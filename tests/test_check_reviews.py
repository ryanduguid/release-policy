from __future__ import annotations

import hashlib
import json
import runpy
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_reviews  # noqa: E402
from verify_skills import VerificationError  # noqa: E402

REVIEW_ID = "2026-01-15-example-review"
LATER_ID = "2026-01-16-second-review"
SIGNED = "2026-01-15"
NOTE = "- NOTE. Minor wording.\n  Why it matters: readers may misread it.\n  Remedy: disclaim in the documentation."
WARNING = "- WARNING. A defensible reading is undisclosed.\n  Why it matters: another reading exists.\n  Remedy: disclaim in the documentation."
CRITICAL = "- CRITICAL. Wrong output for a lawful input.\n  Why it matters: money.\n  Remedy: refuse with an error."
QUESTION = "- Does the export label the receipt date?\n  Why it matters: a sent date can read as on time.\n  Resolution path: a real header row."
CHANGE = "- Defect: no year label on the output.\n  Change: add an income_year field.\n  Re-review: no"


def render_verdict(
    entry: dict[str, object],
    *,
    findings: str,
    questions: str = QUESTION,
    changes: str = CHANGE,
    headline: str | None = None,
) -> str:
    subject = entry["subject"]
    reviewer = entry["reviewer"]
    assert isinstance(subject, dict) and isinstance(reviewer, dict)
    scope = entry["scope"]
    assert isinstance(scope, list)
    verdict = entry["verdict"]
    rows = (
        ("Repository", subject["repository"]),
        ("Commit", subject["commit"]),
        ("Release", subject["release"] or "none"),
        ("Scope", ", ".join(scope)),
        ("Reviewer", f"{reviewer['name']}, {reviewer['credential']}"),
        ("Relationship", reviewer["relationship"]),
        ("Verdict", verdict),
        ("Confidence", entry["confidence"]),
        ("Date", entry["date"]),
    )
    if headline is None:
        headline = f"{verdict}. The encoding matches the scope."
        if reviewer["relationship"] == "author":
            headline += " This is a self-review by the maintainer."
    lines = [
        f"# Review verdict {entry['id']}",
        "",
        "## Subject",
        "",
        "| Field | Value |",
        "| --- | --- |",
        *(f"| {field} | {value} |" for field, value in rows),
        "",
        "## 1. Headline verdict",
        "",
        headline,
        "",
        "## 2. Citation audit",
        "",
        "| Claimed | Correct | Correction |",
        "| --- | --- | --- |",
        "| ITAA 1997 s 328-180 | yes | |",
        "| ITAA 1997 s 328-180(2) | no | The rule is in s 328-180(3). |",
        "",
        "Provisions the subject should cite and does not:",
        "",
        "- none",
        "",
        "## 3. Findings",
        "",
        findings,
        "",
        "## 4. Open questions",
        "",
        questions,
        "",
    ]
    if verdict == "FIX":
        lines += ["## 5. Required changes", "", changes, ""]
    lines += [
        "## 6. Method",
        "",
        "Read the brief and the source at the commit above. Two hours. No tools.",
        "",
        "## 7. Attestation",
        "",
        "I read the subject at the commit above and the materials named in",
        "section 6. The verdict is my own professional judgement.",
        "",
        f"Name: {reviewer['name']}",
        f"Date: {entry['date']}",
        "",
    ]
    return "\n".join(lines)


KNOWN_UNKNOWNS = """# Known unknowns

Preamble prose that the parser ignores.

## KU-001: Does the export label the receipt date?

- Raised: 2026-01-10
- Affects: the due-date comparison
- Why it matters: a payment on the date sent can read as on time
  when the fund received it later.
- Resolution path: a header row from a real export
- Status: open

## KU-002: Which May figure fixes the rate?

- Raised: 2026-01-10
- Affects: the rate table
- Why it matters: the wrong month changes every repayment.
- Resolution path: the source series for the year
- Status: resolved 2026-01-12 by RBA F5 series, May 2024 figure
"""


class CheckReviewsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self._git("init", "--quiet")
        self._git("config", "user.email", "tests@example.invalid")
        self._git("config", "user.name", "Review tests")
        self._git("config", "core.autocrlf", "false")
        self._git("config", "tag.gpgSign", "false")
        self._write("README.md", b"# consumer\n")
        self._git("add", "README.md")
        self._git("commit", "--quiet", "-m", "seed")
        self.head = self._git("rev-parse", "HEAD").stdout.strip()
        self._git("tag", "v1.2.3")
        self.verdict = "FIX"
        self.findings = WARNING
        self.entry_overrides: dict[str, object] = {}
        self.render_overrides: dict[str, object] = {}
        self.text_edits: list[tuple[str, str]] = []
        self.known_unknowns = KNOWN_UNKNOWNS
        self.extra_entries: list[dict[str, object]] = []

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=self.root,
            check=True,
            shell=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _write(self, supplied: str, contents: bytes) -> Path:
        path = self.root / supplied
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        return path

    def _entry(self, review_id: str, verdict: str, **overrides: object) -> dict[str, object]:
        entry: dict[str, object] = {
            "confidence": "MEDIUM",
            "date": SIGNED,
            "id": review_id,
            "path": f"reviews/{review_id}.md",
            "reviewer": {
                "credential": "Registered Tax Agent",
                "name": "Example Reviewer",
                "relationship": "independent",
            },
            "scope": ["ITAA_1997_s_328-180", "ITAA_1997_Subdiv_328-D"],
            "sha256": "0" * 64,
            "subject": {
                "commit": self.head,
                "release": "v1.2.3",
                "repository": "ryanduguid/example",
            },
            "supersedes": None,
            "verdict": verdict,
        }
        entry.update(overrides)
        return entry

    def _listed(self, entry: dict[str, object], text: str) -> dict[str, object]:
        for old, new in self.text_edits:
            text = text.replace(old, new)
        self._write(str(entry["path"]), text.encode("utf-8"))
        return dict(entry, sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())

    def _install(self, *, index_bytes: bytes | None = None) -> None:
        entry = self._entry(REVIEW_ID, self.verdict)
        listed = {**entry, **self.entry_overrides}
        try:
            text = render_verdict(listed, findings=self.findings, **self.render_overrides)  # type: ignore[arg-type]
        except (KeyError, AssertionError):
            # A malformed override must reach the checker, so render from the valid entry.
            text = render_verdict(entry, findings=self.findings, **self.render_overrides)  # type: ignore[arg-type]
        listed = self._listed(listed, text)
        if "sha256" in self.entry_overrides:
            listed["sha256"] = self.entry_overrides["sha256"]
        entries = [listed, *self.extra_entries]
        document = {"reviews": entries, "schema": 1}
        if index_bytes is None:
            index_bytes = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
        self._write("reviews/index.json", index_bytes)
        self._write("docs/known-unknowns.md", self.known_unknowns.encode("utf-8"))
        self._git("add", "reviews", "docs")

    def _assert_refused(self, fragment: str, **arguments: object) -> None:
        with self.assertRaisesRegex(VerificationError, fragment):
            check_reviews.check_consumer(self.root, **arguments)  # type: ignore[arg-type]

    def test_accepts_a_complete_consumer_and_reports_counts(self) -> None:
        self._install()
        self.assertEqual(
            check_reviews.check_consumer(self.root),
            "reviews valid: 1 verdicts, 2 known unknowns (1 open)",
        )
        out = StringIO()
        with redirect_stdout(out):
            self.assertEqual(check_reviews.main(["--root", str(self.root)]), 0)
        self.assertIn("reviews valid", out.getvalue())

    def test_main_reports_the_first_failure_and_exits_one(self) -> None:
        self._install(index_bytes=b"{}\n")
        err = StringIO()
        with redirect_stderr(err):
            self.assertEqual(check_reviews.main(["--root", str(self.root)]), 1)
        self.assertIn("check_reviews: review index must contain exactly", err.getvalue())

    def test_the_script_runs_as_a_command(self) -> None:
        self._install()
        script = ROOT / "scripts" / "check_reviews.py"
        argv = ["check_reviews.py", "--root", str(self.root)]
        out = StringIO()
        with mock.patch.object(sys, "argv", argv), redirect_stdout(out):
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path(str(script), run_name="__main__")
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("reviews valid: 1 verdicts", out.getvalue())

    def test_a_checkout_without_commits_cannot_date_the_ledgers(self) -> None:
        with TemporaryDirectory() as empty:
            subprocess.run(["git", "init", "--quiet"], cwd=empty, check=True, shell=False)
            with self.assertRaisesRegex(VerificationError, "no commit to date"):
                check_reviews.check_consumer(Path(empty))

    def test_crlf_verdicts_digest_as_lf_and_other_encodings_are_refused(self) -> None:
        self._install()
        path = self.root / f"reviews/{REVIEW_ID}.md"
        lf = path.read_bytes()
        path.write_bytes(lf.replace(b"\n", b"\r\n"))
        check_reviews.check_consumer(self.root)
        for raw, fragment in (
            (b"\xef\xbb\xbf" + lf, "must not contain a UTF-8 BOM"),
            (lf + b"\xff", "must be valid UTF-8"),
            (lf.replace(b"Subject", b"Sub\rject"), "lone carriage return"),
            (lf + b"\n", "does not match its listed sha256"),
        ):
            with self.subTest(fragment=fragment):
                path.write_bytes(raw)
                self._assert_refused(fragment)

    def test_index_must_be_tracked_canonical_json(self) -> None:
        for raw, fragment in (
            (b"\xef\xbb\xbf{}\n", "must not contain a UTF-8 BOM"),
            (b"{\n", "must be valid UTF-8 JSON"),
            (b'{"schema": 1, "reviews": []}', "canonical sorted JSON"),
            (b'{\n  "reviews": [],\n  "schema": 2\n}\n', "schema must be 1"),
            (b'{\n  "reviews": {},\n  "schema": 1\n}\n', "reviews must be a list"),
            (b'{\n  "reviews": [\n    1\n  ],\n  "schema": 1\n}\n', "must contain exactly"),
        ):
            with self.subTest(fragment=fragment):
                self._install(index_bytes=raw)
                self._assert_refused(fragment)
        self._install()
        self._git("rm", "--cached", "--quiet", "reviews/index.json")
        self._assert_refused("not one tracked Git entry")

    def test_entry_fields_are_validated(self) -> None:
        cases: list[tuple[dict[str, object], str]] = [
            ({"id": "Example"}, "id must be YYYY-MM-DD-hyphenated-words"),
            ({"id": ""}, "id must be a non-empty string"),
            ({"id": "2026-02-30-example"}, "id date prefix must be an ISO 8601 calendar date"),
            ({"path": "reviews/other.md"}, f"path must be reviews/{REVIEW_ID}.md"),
            ({"sha256": "0" * 63}, "sha256 must be 64 lower-case hex digits"),
            ({"subject": {"commit": self.head}}, "subject must contain exactly"),
            (
                {"subject": {"commit": self.head, "release": None, "repository": "nested/a/b"}},
                "subject.repository must be owner/name",
            ),
            (
                {"subject": {"commit": "abc", "release": None, "repository": "o/n"}},
                "subject.commit must be a full lower-case commit SHA",
            ),
            (
                {"subject": {"commit": self.head, "release": "1.2.3", "repository": "o/n"}},
                "subject.release must be a release tag",
            ),
            ({"scope": []}, "scope must be a non-empty list"),
            ({"scope": "ITAA_1997_s_1"}, "scope must be a non-empty list"),
            ({"scope": ["ITAA 1997 s 1"]}, r"scope\[0\] must be an identifier"),
            ({"scope": ["ITAA_1997_s_1", "ITAA_1997_s_1"]}, "scope repeats an identifier"),
            ({"reviewer": {"name": "A"}}, "reviewer must contain exactly"),
            (
                {"reviewer": {"credential": "CPA", "name": "", "relationship": "author"}},
                "reviewer.name must be a non-empty string",
            ),
            (
                {"reviewer": {"credential": "", "name": "A", "relationship": "author"}},
                "reviewer.credential must be a non-empty string",
            ),
            (
                {"reviewer": {"credential": "CPA", "name": "A", "relationship": "tool"}},
                "reviewer.relationship must be one of",
            ),
            ({"verdict": "MAYBE"}, "verdict must be one of"),
            ({"confidence": "SURE"}, "confidence must be one of"),
            ({"date": "15 January 2026"}, "date must be an ISO 8601 calendar date"),
            ({"date": "20260115"}, "date must be an ISO 8601 calendar date"),
            ({"date": "2999-01-01"}, "date 2999-01-01 is later than the checked commit date"),
            ({"supersedes": "2026-01-01-unknown"}, "supersedes must name an earlier listed review"),
        ]
        for overrides, fragment in cases:
            with self.subTest(fragment=fragment):
                self.entry_overrides = overrides
                self._install()
                self._assert_refused(fragment)

    def test_scope_grammar_admits_statutes_standards_and_nested_provisions(self) -> None:
        self.entry_overrides = {
            "scope": [
                "SGAA_1992_s_18C(1)(c)(i)",
                "APES_110_para_R112.1",
                "TPB_PG_01-2023",
                "TAA_1953_s_284-75",
            ]
        }
        self._install()
        check_reviews.check_consumer(self.root)

    def test_subject_commit_and_release_are_resolved_in_git(self) -> None:
        orphan = self._git("commit-tree", "HEAD^{tree}", "-m", "orphan").stdout.strip()
        self._git("tag", "elsewhere/v2.0.0", orphan)
        for subject, fragment in (
            ({"commit": "1" * 40, "release": None, "repository": "o/n"}, "not present in this checkout"),
            ({"commit": orphan, "release": None, "repository": "o/n"}, "not an ancestor of the checked-out"),
            ({"commit": self.head, "release": "v9.9.9", "repository": "o/n"}, "is not present in this checkout; fetch tags"),
            ({"commit": self.head, "release": "elsewhere/v2.0.0", "repository": "o/n"}, "points at another commit"),
        ):
            with self.subTest(fragment=fragment):
                self.entry_overrides = {"subject": subject}
                self._install()
                self._assert_refused(fragment)
        self.entry_overrides = {"subject": {"commit": self.head, "release": None, "repository": "o/n"}}
        self._install()
        check_reviews.check_consumer(self.root)

    def test_duplicate_unsorted_and_superseding_entries(self) -> None:
        later = self._entry(LATER_ID, "ACCEPT", supersedes=REVIEW_ID)
        later = self._listed(later, render_verdict(later, findings=NOTE))

        self.extra_entries = [later]
        self._install()
        self.assertTrue(check_reviews.check_consumer(self.root).startswith("reviews valid: 2 "))

        self.extra_entries = [dict(later, id=REVIEW_ID, path=f"reviews/{REVIEW_ID}.md")]
        self._install()
        self._assert_refused("duplicate review id")

        earlier = dict(later, id="2026-01-01-earlier", path="reviews/2026-01-01-earlier.md", supersedes=None)
        self.extra_entries = [earlier]
        self._install()
        self._assert_refused("must be sorted by id")

    def test_verdict_file_structure_is_enforced(self) -> None:
        cases = (
            ((f"# Review verdict {REVIEW_ID}", "# Verdict"), "must open with"),
            (("## 6. Method", "## Method"), "sections must be exactly"),
            (("## 7. Attestation", "## 7. Attestation\n\n## 8. Extra"), "sections must be exactly"),
            (("| Field | Value |", "| Name | Value |"), "Subject must be a Field"),
            (("| Verdict | FIX |", "| Verdict | FIX | extra |"), "Subject rows must have two cells"),
            (("| Confidence | MEDIUM |", "| Confidence | HIGH |"), r"Subject rows differ from the index: \['Confidence'\]"),
            ((f"| Date | {SIGNED} |", f"| Date | 2026-01-01 |\n| Date | {SIGNED} |"), "Subject rows must be exactly"),
            (("FIX. The encoding", "ACCEPT. The encoding"), "headline must open with FIX"),
            (("FIX. The encoding matches the scope.", ""), "headline must open with FIX"),
            (("| Claimed | Correct | Correction |", "| Section | Correct | Note |"), "must open with the Claimed table"),
            (("| yes | |", "| maybe | |"), "citation row must be claimed"),
            (("| no | The rule is in s 328-180(3). |", "| no | |"), "marked no needs a correction"),
            (("- WARNING. A defensible", "- Soft. A defensible"), "finding must open with"),
            (("  Remedy: disclaim in the documentation.", "  Remedy:"), "needs a non-empty 'Remedy:' line"),
            (("  Why it matters: another reading exists.", "  stray sentence"), "text before its first named line"),
            (("- Does the export label the receipt date?", "- Does the export label the receipt date"), "must end with a question mark"),
            (("  Resolution path: a real header row.", ""), "needs a non-empty 'Resolution path:' line"),
            (("- Does the export label the receipt date?", "- Is the rate ever rounded?"), "open question is not in docs/known-unknowns.md: Is the rate ever rounded"),
            (("- Defect: no year label on the output.", "- No year label on the output."), "required change must open with 'Defect: '"),
            (("- Defect: no year label on the output.", "- Defect: no year label on the output.\nstray"), "line outside any list item"),
            (("  Re-review: no", "  Re-review: later"), "Re-review must be yes or no"),
            (("Read the brief and the source at the commit above. Two hours. No tools.", ""), "Method must describe"),
            (("I read the subject at the commit above", "I skimmed the subject at the commit above"), "must contain the fixed sentence"),
            (("Name: Example Reviewer", "Name: Someone Else"), "signed with the index name and date"),
        )
        for edit, fragment in cases:
            with self.subTest(fragment=fragment):
                self.text_edits = [edit]
                self._install()
                self._assert_refused(fragment)

    def test_citation_audit_needs_rows(self) -> None:
        self.text_edits = [
            ("| ITAA 1997 s 328-180 | yes | |\n", ""),
            ("| ITAA 1997 s 328-180(2) | no | The rule is in s 328-180(3). |\n", ""),
        ]
        self._install()
        self._assert_refused("citation audit has no rows")

    def test_list_sections_take_items_or_none(self) -> None:
        self.verdict = "ACCEPT"
        self.findings = "none"
        self.render_overrides = {"questions": "none"}
        self._install()
        check_reviews.check_consumer(self.root)
        self.render_overrides = {"questions": ""}
        self._install()
        self._assert_refused("Open questions must list items or say none")
        self.verdict = "FIX"
        self.findings = WARNING
        self.render_overrides = {"changes": "none"}
        self._install()
        self._assert_refused("is FIX and must list at least one required change")

    def test_fix_verdict_needs_section_five_and_others_must_not_have_it(self) -> None:
        self.verdict = "ACCEPT"
        self.findings = NOTE
        self.text_edits = [("## 6. Method", "## 5. Required changes\n\n- x\n\n## 6. Method")]
        self._install()
        self._assert_refused("sections must be exactly")
        self.verdict = "FIX"
        self.findings = WARNING
        self.text_edits = [("## 5. Required changes", "## 5. Changes")]
        self._install()
        self._assert_refused("sections must be exactly")

    def test_severity_rules_follow_the_verdict(self) -> None:
        for verdict, findings, fragment in (
            ("REJECT", WARNING, "needs a CRITICAL finding"),
            ("ACCEPT", CRITICAL, "lists a CRITICAL finding"),
            ("REJECT", CRITICAL, ""),
            ("ACCEPT", NOTE, ""),
            ("FIX", CRITICAL, ""),
        ):
            with self.subTest(verdict=verdict, findings=findings):
                self.verdict = verdict
                self.findings = findings
                self._install()
                if fragment:
                    self._assert_refused(fragment)
                else:
                    check_reviews.check_consumer(self.root)

    def test_author_verdicts_must_say_self_review(self) -> None:
        reviewer = {"credential": "CPA", "name": "Example Reviewer", "relationship": "author"}
        self.entry_overrides = {"reviewer": reviewer}
        self._install()
        check_reviews.check_consumer(self.root)
        self.render_overrides = {"headline": "FIX. The encoding matches the scope."}
        self._install()
        self._assert_refused("author verdict and its headline must say self-review")

    def test_known_unknowns_register_is_validated(self) -> None:
        entry = KNOWN_UNKNOWNS.split("## KU-002")[0]
        cases = (
            ("## KU-1: Short?\n\n- Raised: 2026-01-10\n", "heading must be"),
            ("## KU-003: No question mark\n", "heading must be"),
            (entry + entry.split("\n\n", 2)[2], "duplicate known-unknown id"),
            (entry + "## KU-003: Q?\n\nstray prose\n", "has an unexpected line"),
            (entry + "## KU-003: Q?\n\n- Raised: 2026-01-10\n", "must have exactly the lines"),
            (entry.replace("- Affects: the due-date comparison", "- Affect: x"), "line must be 'Affects: <text>'"),
            (entry.replace("- Affects: the due-date comparison", "- Affects: "), "line must be 'Affects: <text>'"),
            (entry.replace("- Raised: 2026-01-10", "- Raised: yesterday"), "Raised must be an ISO 8601"),
            (entry.replace("- Raised: 2026-01-10", "- Raised: 2999-01-01"), "Raised 2999-01-01 is later than"),
            (entry.replace("- Status: open", "- Status: closed"), "Status must be 'open' or"),
            (entry.replace("- Status: open", "- Status: resolved 2026-13-01 by x"), "resolution date must be"),
            (entry.replace("- Status: open", "- Status: resolved 2999-01-01 by x"), "resolution date 2999-01-01 is later"),
            (
                entry.replace("- Status: open", "- Status: resolved 2026-01-12 by 2026-01-01-missing"),
                "resolved by an unlisted verdict",
            ),
        )
        for text, fragment in cases:
            with self.subTest(fragment=fragment):
                self.known_unknowns = text
                self._install()
                self._assert_refused(fragment)

    def test_known_unknowns_may_resolve_by_a_listed_verdict_or_a_citation(self) -> None:
        self.known_unknowns = KNOWN_UNKNOWNS.replace(
            "- Status: open", f"- Status: resolved 2026-01-15 by {REVIEW_ID}"
        )
        self._install()
        self.assertEqual(
            check_reviews.check_consumer(self.root),
            "reviews valid: 1 verdicts, 2 known unknowns (0 open)",
        )
        self.known_unknowns = "# Known unknowns\n\nNo entries yet.\n"
        self.render_overrides = {"questions": "none"}
        self._install()
        self.assertTrue(check_reviews.check_consumer(self.root).endswith("0 known unknowns (0 open)"))
        self._write("docs/known-unknowns.md", b"\xef\xbb\xbf# Known unknowns\n")
        self._assert_refused("register must not contain a UTF-8 BOM")

    def test_base_revision_holds_the_ledgers_append_only(self) -> None:
        later = self._entry(LATER_ID, "ACCEPT", supersedes=REVIEW_ID)
        later = self._listed(later, render_verdict(later, findings=NOTE))
        self.extra_entries = [later]
        self._install()
        self._git("commit", "--quiet", "-m", "ledgers")

        self._assert_refused("base revision is not a commit", base="no-such-ref")
        check_reviews.check_consumer(self.root, base=self.head)
        check_reviews.check_consumer(self.root, base="HEAD")

        self.extra_entries = []
        self._install()
        self._assert_refused(f"review {LATER_ID} was listed at HEAD and must not change or disappear", base="HEAD")

        self.extra_entries = [later]
        self.entry_overrides = {"confidence": "HIGH"}
        self._install()
        self._assert_refused(f"review {REVIEW_ID} was listed at HEAD", base="HEAD")
        self.entry_overrides = {}

        for text, fragment in (
            (KNOWN_UNKNOWNS.split("## KU-002")[0], "KU-002 was registered at HEAD and must not disappear"),
            (KNOWN_UNKNOWNS.replace("- Affects: the rate table", "- Affects: the rates"), "KU-002 may change only its Status line"),
            (KNOWN_UNKNOWNS.replace("- Status: resolved 2026-01-12 by RBA F5 series, May 2024 figure", "- Status: open"), "KU-002 Status may only move from open to resolved"),
            (KNOWN_UNKNOWNS.replace("- Status: open", "- Status: resolved 2026-01-14 by ATO guidance"), ""),
        ):
            with self.subTest(fragment=fragment or "open to resolved"):
                self.known_unknowns = text
                self._install()
                if fragment:
                    self._assert_refused(fragment, base="HEAD")
                else:
                    check_reviews.check_consumer(self.root, base="HEAD")

    def test_base_revision_without_a_register_checks_only_the_index(self) -> None:
        self._install()
        self._git("rm", "--cached", "--quiet", "docs/known-unknowns.md")
        self._git("commit", "--quiet", "-m", "index only")
        self._install()
        check_reviews.check_consumer(self.root, base="HEAD")


if __name__ == "__main__":
    unittest.main()
