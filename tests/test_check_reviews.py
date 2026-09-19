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

REVIEW_ID = "2026-09-20-example-review"
LATER_ID = "2026-09-21-second-review"


def verdict_text(review_id: str, verdict: str, *, findings: str, fix_section: bool) -> str:
    sections = [
        f"# Review verdict {review_id}",
        "",
        "## Subject",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Verdict | {verdict} |",
        "",
        "## 1. Headline verdict",
        "",
        f"{verdict}. The encoding matches the scope.",
        "",
        "## 2. Citation audit",
        "",
        "| Claimed | Correct | Correction |",
        "| --- | --- | --- |",
        "| ITAA 1997 s 328-180 | yes | |",
        "",
        "## 3. Findings",
        "",
        findings,
        "",
        "## 4. Open questions",
        "",
        "- Which export column is the receipt date?",
        "  Why it matters: a sent date can read as on time.",
        "  Resolution path: a real header row.",
        "",
    ]
    if fix_section:
        sections += [
            "## 5. Required changes",
            "",
            "- Defect: no year label. Change: add one. Re-review: no.",
            "",
        ]
    sections += [
        "## 6. Method",
        "",
        "Read the brief. Two hours. No tools.",
        "",
        "## 7. Attestation",
        "",
        "Name: Example Reviewer",
        "Date: 2026-09-20",
        "",
    ]
    return "\n".join(sections)


KNOWN_UNKNOWNS = """# Known unknowns

Preamble prose that the parser ignores.

## KU-001: Does the export label the receipt date?

- Raised: 2026-09-20
- Affects: the due-date comparison
- Why it matters: a payment on the date sent can read as on time
  when the fund received it later.
- Resolution path: a header row from a real export
- Status: open

## KU-002: Which May figure fixes the rate?

- Raised: 2026-09-20
- Affects: the rate table
- Why it matters: the wrong month changes every repayment.
- Resolution path: the source series for the year
- Status: resolved 2026-09-20 by RBA F5 series, May 2024 figure
"""


class CheckReviewsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self._git("init", "--quiet")
        self._git("config", "user.email", "tests@example.invalid")
        self._git("config", "user.name", "Review tests")
        self._git("config", "core.autocrlf", "false")
        self._write("README.md", b"# consumer\n")
        self._git("add", "README.md")
        self._git("commit", "--quiet", "-m", "seed")
        self.head = self._git("rev-parse", "HEAD").stdout.strip()
        self.verdict = "FIX"
        self.findings = "- WARNING. A defensible reading is undisclosed.\n  Why it matters: x.\n  Remedy: disclaim."
        self.fix_section = True
        self.verdict_override: str | None = None
        self.entry_overrides: dict[str, object] = {}
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

    def _entry(self, review_id: str, text: str, **overrides: object) -> dict[str, object]:
        entry: dict[str, object] = {
            "confidence": "MEDIUM",
            "date": "2026-09-20",
            "id": review_id,
            "path": f"reviews/{review_id}.md",
            "reviewer": {
                "credential": "Registered Tax Agent",
                "name": "Example Reviewer",
                "relationship": "independent",
            },
            "scope": ["ITAA_1997_s_328-180", "ITAA_1997_Subdiv_328-D"],
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "subject": {
                "commit": self.head,
                "release": "v1.2.3",
                "repository": "ryanduguid/example",
            },
            "supersedes": None,
            "verdict": self.verdict,
        }
        entry.update(overrides)
        return entry

    def _install(self, *, index_bytes: bytes | None = None) -> None:
        text = self.verdict_override
        if text is None:
            text = verdict_text(
                REVIEW_ID, self.verdict, findings=self.findings, fix_section=self.fix_section
            )
        self._write(f"reviews/{REVIEW_ID}.md", text.encode("utf-8"))
        entries = [self._entry(REVIEW_ID, text, **self.entry_overrides), *self.extra_entries]
        document = {"reviews": entries, "schema": 1}
        if index_bytes is None:
            index_bytes = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
        self._write("reviews/index.json", index_bytes)
        self._write("docs/known-unknowns.md", self.known_unknowns.encode("utf-8"))
        self._git("add", "reviews", "docs")

    def _assert_refused(self, fragment: str) -> None:
        with self.assertRaisesRegex(VerificationError, fragment):
            check_reviews.check_consumer(self.root)

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
            ({"scope": ["ITAA 1997 s 1"]}, r"scope\[0\] must be ACT_YEAR_unit_SECTION"),
            ({"scope": ["ITAA_1997_s_1", "ITAA_1997_s_1"]}, "scope repeats a provision"),
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
            ({"date": "20 September 2026"}, "date must be an ISO 8601 calendar date"),
            ({"date": "20260920"}, "date must be an ISO 8601 calendar date"),
            ({"supersedes": "2026-01-01-unknown"}, "supersedes must name an earlier listed review"),
        ]
        for overrides, fragment in cases:
            with self.subTest(fragment=fragment):
                self.entry_overrides = overrides
                self._install()
                self._assert_refused(fragment)

    def test_release_may_be_null_or_namespaced(self) -> None:
        for release in (None, "review-ready-gate/v0.1.5"):
            with self.subTest(release=release):
                self.entry_overrides = {
                    "subject": {"commit": self.head, "release": release, "repository": "o/n"}
                }
                self._install()
                check_reviews.check_consumer(self.root)

    def test_duplicate_unsorted_and_superseding_entries(self) -> None:
        text = verdict_text(LATER_ID, "ACCEPT", findings="- NOTE. Minor.", fix_section=False)
        self._write(f"reviews/{LATER_ID}.md", text.encode("utf-8"))
        later = self._entry(LATER_ID, text, verdict="ACCEPT", supersedes=REVIEW_ID)

        self.extra_entries = [later]
        self._install()
        self.assertTrue(check_reviews.check_consumer(self.root).startswith("reviews valid: 2 "))

        self.extra_entries = [dict(later, id=REVIEW_ID, path=f"reviews/{REVIEW_ID}.md")]
        self._install()
        self._assert_refused("duplicate review id")

        earlier = dict(later, id="2026-01-01-earlier", path="reviews/2026-01-01-earlier.md")
        earlier["supersedes"] = None
        self._write("reviews/2026-01-01-earlier.md", text.encode("utf-8"))
        self.extra_entries = [earlier]
        self._install()
        self._assert_refused("must be sorted by id")

    def test_verdict_file_structure_is_enforced(self) -> None:
        base = verdict_text(REVIEW_ID, "FIX", findings="- NOTE. Minor.", fix_section=True)
        for text, fragment in (
            (base.replace(f"# Review verdict {REVIEW_ID}", "# Verdict"), "must open with"),
            (base.replace("## 6. Method", "## Method"), "sections must be exactly"),
            (base.replace("## 7. Attestation", "## 7. Attestation\n\n## 8. Extra"), "sections must be exactly"),
            (base.replace("FIX. The encoding", "ACCEPT. The encoding"), "headline must open with FIX"),
            (base.replace("FIX. The encoding matches the scope.", ""), "headline must open with FIX"),
            (base.replace("- NOTE. Minor.", "- Minor."), "finding must open with"),
        ):
            with self.subTest(fragment=fragment):
                self.verdict_override = text
                self._install()
                self._assert_refused(fragment)
        self.verdict_override = None
        self.fix_section = False
        self._install()
        self._assert_refused("sections must be exactly")

    def test_fix_verdict_needs_section_five_and_others_must_not_have_it(self) -> None:
        self.verdict = "ACCEPT"
        self.findings = "- NOTE. Minor."
        self.fix_section = True
        self._install()
        self._assert_refused("sections must be exactly")

    def test_severity_rules_follow_the_verdict(self) -> None:
        critical = "- CRITICAL. Wrong output.\n  Why it matters: money.\n  Remedy: refuse."
        for verdict, findings, fragment in (
            ("REJECT", "- WARNING. Soft.", "needs a CRITICAL finding"),
            ("ACCEPT", critical, "lists a CRITICAL finding"),
            ("REJECT", critical, ""),
            ("ACCEPT", "- NOTE. Minor.", ""),
            ("FIX", critical, ""),
        ):
            with self.subTest(verdict=verdict, findings=findings):
                self.verdict = verdict
                self.findings = findings
                self.fix_section = verdict == "FIX"
                self._install()
                if fragment:
                    self._assert_refused(fragment)
                else:
                    check_reviews.check_consumer(self.root)

    def test_subject_commit_must_be_an_ancestor_of_head(self) -> None:
        orphan = self._git("commit-tree", "HEAD^{tree}", "-m", "orphan").stdout.strip()
        self.entry_overrides = {
            "subject": {"commit": orphan, "release": None, "repository": "o/n"}
        }
        self._install()
        self._assert_refused("not an ancestor of the checked-out commit")

    def test_known_unknowns_register_is_validated(self) -> None:
        entry = KNOWN_UNKNOWNS.split("## KU-002")[0]
        cases = (
            ("## KU-1: Short?\n\n- Raised: 2026-09-20\n", "heading must be"),
            ("## KU-003: No question mark\n", "heading must be"),
            (entry + entry.split("\n\n", 2)[2], "duplicate known-unknown id"),
            (entry + "## KU-003: Q?\n\nstray prose\n", "has an unexpected line"),
            (entry + "## KU-003: Q?\n\n- Raised: 2026-09-20\n", "must have exactly the lines"),
            (entry.replace("- Affects: the due-date comparison", "- Affect: x"), "line must be 'Affects: <text>'"),
            (entry.replace("- Affects: the due-date comparison", "- Affects: "), "line must be 'Affects: <text>'"),
            (entry.replace("- Raised: 2026-09-20", "- Raised: yesterday"), "Raised must be an ISO 8601"),
            (entry.replace("- Status: open", "- Status: closed"), "Status must be 'open' or"),
            (entry.replace("- Status: open", "- Status: resolved 2026-13-01 by x"), "resolution date must be"),
            (
                entry.replace("- Status: open", "- Status: resolved 2026-09-21 by 2026-01-01-missing"),
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
            "- Status: open", f"- Status: resolved 2026-09-21 by {REVIEW_ID}"
        )
        self._install()
        self.assertEqual(
            check_reviews.check_consumer(self.root),
            "reviews valid: 1 verdicts, 2 known unknowns (0 open)",
        )
        self.known_unknowns = "# Known unknowns\n\nNo entries yet.\n"
        self._install()
        self.assertTrue(check_reviews.check_consumer(self.root).endswith("0 known unknowns (0 open)"))
        self._write("docs/known-unknowns.md", b"\xef\xbb\xbf# Known unknowns\n")
        self._assert_refused("register must not contain a UTF-8 BOM")


if __name__ == "__main__":
    unittest.main()
