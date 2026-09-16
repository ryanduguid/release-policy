from __future__ import annotations

import contextlib
import io
import runpy
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from scripts import check_egress


def write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@contextlib.contextmanager
def candidate(files: dict[str, str]):
    """A publication candidate, with the working directory inside it.

    The checker matches an --exclude against the path as given, so tests that
    pass relative paths exercise the same matching a caller gets from a
    repository checkout.
    """
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        for name, text in files.items():
            write(root, name, text)
        with contextlib.chdir(root):
            yield root


def run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = check_egress.main(argv)
    return code, out.getvalue(), err.getvalue()


class LayerATests(unittest.TestCase):
    """Configured terms: the words that must never reach a public artefact."""

    def test_a_configured_term_is_a_finding_and_the_term_is_not_printed(self) -> None:
        # The finding names where to look, never what was found: this output
        # goes to CI logs, read by more people than the artefact would reach.
        with candidate({"README.md": "Prepared for Northwind Holdings in June.\n"}) as root:
            terms = write(root.parent, "terms.txt", "# policy\nNorthwind Holdings\n")
            code, out, _ = run([str(root / "README.md"), "--terms", str(terms)])
            self.assertEqual(code, 1)
            self.assertIn("[A] configured term at ", out)
            self.assertIn("terms.txt:2 (match redacted)", out)
            self.assertIn("README.md:1", out)
            self.assertNotIn("Northwind", out)
            self.assertNotIn("Prepared for", out)
            self.assertIn("do not publish", out)

    def test_a_term_is_found_whatever_its_case(self) -> None:
        with candidate({"notes.md": "NORTHWIND holdings pty ltd\n"}) as root:
            terms = write(root.parent, "terms.txt", "Northwind Holdings\n")
            code, out, _ = run([str(root), "--terms", str(terms)])
            self.assertEqual(code, 1)
            self.assertIn("[A]", out)

    def test_a_near_miss_is_not_a_finding(self) -> None:
        # The near-miss negative: a term must not fire on a word that merely
        # shares a prefix, or the gate trains its operator to ignore it.
        with candidate({"notes.md": "Northwinds are common on this coast.\n"}) as root:
            terms = write(root.parent, "terms.txt", "Northwind Holdings\n")
            code, out, _ = run([str(root), "--terms", str(terms)])
            self.assertEqual(code, 0)
            self.assertIn("RESULT: clean", out)

    def test_comments_and_blank_lines_in_the_terms_file_are_not_terms(self) -> None:
        with candidate({"notes.md": "nothing to see\n"}) as root:
            terms = write(root.parent, "terms.txt", "# a comment\n\n  \nAcme\n")
            loaded = check_egress.load_terms(terms)
            self.assertEqual([(term.line, term.text) for term in loaded], [(4, "Acme")])
            code, _, _ = run([str(root), "--terms", str(terms)])
            self.assertEqual(code, 0)

    def test_no_part_of_the_matched_line_reaches_the_output(self) -> None:
        line = "%s Northwind Holdings %s" % ("filler " * 40, "trailing " * 40)
        with candidate({"notes.md": line + "\n"}) as root:
            terms = write(root.parent, "terms.txt", "Northwind Holdings\n")
            _, out, _ = run([str(root), "--terms", str(terms)])
            printed = [row for row in out.splitlines() if "[A]" in row][0]
            self.assertLess(len(printed), len(line))
            for fragment in ("filler", "trailing", "Northwind"):
                self.assertNotIn(fragment, printed)


class LayerBTests(unittest.TestCase):
    """Structural shapes: a soft fail a person waves through, not silence."""

    def test_structural_shapes_are_findings_by_default(self) -> None:
        for name, text in (
            ("windows path", r"see C:\Users\ryan\Documents\notes.md"),
            ("unix path", "see /home/ryan/notes.md for the working copy"),
            ("private ipv4", "the runner answered on 10.1.2.3 overnight"),
            ("rfc1918", "the printer sits at 192.168.0.14"),
            ("localhost", "open http://localhost:8080 to see it"),
            ("internal host", "resolved through reports.internal all week"),
            ("bucket", "staged into s3://client-exports-2026 first"),
            ("dotenv", "copy the key out of .env before running"),
            ("unc", r"mapped from \\fileserver\payroll last quarter"),
        ):
            with self.subTest(name), candidate({"notes.md": text + "\n"}) as root:
                code, out, _ = run([str(root), "--no-terms"])
                self.assertEqual(code, 1, out)
                self.assertIn("[B]", out)

    def test_ordinary_prose_does_not_trip_the_structural_layer(self) -> None:
        # Near-miss negatives for the patterns above: a public address, a
        # version number shaped like an address, and a sentence about an
        # environment file that is not a path to one.
        text = (
            "The service is published at https://example.com/reports.\n"
            "Version 10.1.2 shipped on Tuesday and 192.168 is not in it.\n"
            "The environment is documented in the deployment guide.\n"
            "Local development happens on the developer's own machine.\n"
        )
        with candidate({"notes.md": text}) as root:
            code, out, _ = run([str(root), "--no-terms"])
            self.assertEqual(code, 0, out)

    def test_allow_structural_reports_without_failing(self) -> None:
        with candidate({"notes.md": "open http://localhost:8080 to see it\n"}) as root:
            code, out, _ = run([str(root), "--no-terms", "--allow-structural"])
            self.assertEqual(code, 0)
            self.assertIn("[B]", out)
            self.assertIn("waved through", out)

    def test_allow_structural_does_not_wave_through_the_other_layers(self) -> None:
        with candidate({"notes.md": "x-internal-ledger: 4\n"}) as root:
            code, out, _ = run([str(root), "--no-terms", "--allow-structural"])
            self.assertEqual(code, 1)
            self.assertIn("[C]", out)


class LayerCTests(unittest.TestCase):
    """The reserved namespace is decisive on its own."""

    def test_the_reserved_namespace_is_a_finding(self) -> None:
        with candidate({"meta.yaml": "x-internal-cost-centre: 1182\n"}) as root:
            code, out, _ = run([str(root), "--no-terms"])
            self.assertEqual(code, 1)
            self.assertIn("[C] reserved namespace", out)

    def test_a_similar_key_that_is_not_the_namespace_is_not_a_finding(self) -> None:
        with candidate({"meta.yaml": "x_internal_note: fine\nexternal-id: 4\n"}) as root:
            code, out, _ = run([str(root), "--no-terms"])
            self.assertEqual(code, 0, out)


class ExitStatusTests(unittest.TestCase):
    """0 clean, 1 finding, 2 the check did not run. Never 1 for a broken gate."""

    def test_a_clean_candidate_exits_0(self) -> None:
        with candidate({"README.md": "Nothing private here.\n"}) as root:
            terms = write(root.parent, "terms.txt", "Northwind Holdings\n")
            code, out, _ = run([str(root), "--terms", str(terms)])
            self.assertEqual(code, 0)
            self.assertIn("RESULT: clean", out)

    def test_a_missing_terms_file_exits_2_not_1(self) -> None:
        with candidate({"README.md": "clean\n"}) as root:
            code, out, err = run([str(root), "--terms", str(root / "absent.txt")])
            self.assertEqual(code, 2)
            self.assertIn("EGRESS CHECK ERROR", err)
            self.assertIn("unchecked, not clean", out)

    def test_an_empty_terms_file_exits_2(self) -> None:
        # An emptied policy file is the likeliest way this gate would go
        # quiet, so it is an error rather than an empty policy.
        with candidate({"README.md": "clean\n"}) as root:
            terms = write(root.parent, "terms.txt", "# everything was deleted\n")
            code, _, err = run([str(root), "--terms", str(terms)])
            self.assertEqual(code, 2)
            self.assertIn("lists no terms", err)

    def test_a_missing_candidate_exits_2(self) -> None:
        with candidate({"README.md": "clean\n"}) as root:
            code, _, err = run([str(root / "absent"), "--no-terms"])
            self.assertEqual(code, 2)
            self.assertIn("does not exist", err)

    def test_running_without_a_terms_decision_exits_2(self) -> None:
        # Fail closed: no terms file and no acknowledgement means layer A did
        # not run, and a clean result would be a lie by omission.
        with candidate({"README.md": "clean\n"}) as root:
            code, _, err = run([str(root)])
            self.assertEqual(code, 2)
            self.assertIn("layer A needs a terms file", err)

    def test_contradictory_terms_arguments_exit_2(self) -> None:
        with candidate({"README.md": "clean\n"}) as root:
            terms = write(root.parent, "terms.txt", "Acme\n")
            code, _, err = run([str(root), "--terms", str(terms), "--no-terms"])
            self.assertEqual(code, 2)
            self.assertIn("contradict", err)

    def test_a_pattern_that_does_not_compile_exits_2(self) -> None:
        with self.assertRaises(check_egress.EgressError):
            check_egress.scan_text(
                "text", label_path="x.md", terms=(), patterns=(("broken", "("),),
            )

    def test_the_three_statuses_are_distinct(self) -> None:
        self.assertEqual(
            sorted({
                check_egress.EXIT_OK,
                check_egress.EXIT_FINDINGS,
                check_egress.EXIT_COULD_NOT_RUN,
            }),
            [0, 1, 2],
        )


class CandidateTests(unittest.TestCase):
    def test_a_directory_is_walked_and_a_file_is_taken_as_given(self) -> None:
        with candidate({"a.md": "one\n", "nested/b.md": "two\n"}) as root:
            walked = check_egress.candidate_files([root])
            self.assertEqual([p.name for p in walked], ["a.md", "b.md"])
            self.assertEqual(check_egress.candidate_files([root / "a.md"]), (root / "a.md",))

    def test_a_file_that_cannot_be_read_as_text_is_not_cleared(self) -> None:
        # "Clean" would be a claim about bytes the gate never decoded.
        with candidate({"README.md": "clean\n"}) as root:
            (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n binary")
            code, out, _ = run([str(root), "--no-terms"])
            self.assertEqual(code, 1)
            self.assertIn("not scanned as text: ", out)
            self.assertIn("logo.png", out)
            self.assertIn("unchecked, so this candidate is not cleared", out)

    def test_undecodable_text_is_reported_rather_than_crashing(self) -> None:
        with candidate({"README.md": "clean\n"}) as root:
            (root / "broken.md").write_bytes(b"\xff\xfe not utf-8")
            code, out, _ = run([str(root), "--no-terms"])
            self.assertEqual(code, 1)
            self.assertIn("broken.md", out)

    def test_allow_unscanned_clears_them_once_a_person_has_looked(self) -> None:
        with candidate({"README.md": "clean\n"}) as root:
            (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n binary")
            code, out, _ = run([str(root), "--no-terms", "--allow-unscanned"])
            self.assertEqual(code, 0)
            self.assertIn("not scanned as text: ", out)
            self.assertIn("waved through by --allow-unscanned", out)

    def test_allow_unscanned_does_not_wave_through_a_finding(self) -> None:
        with candidate({"notes.md": "x-internal-ledger: 4\n"}) as root:
            (root / "logo.png").write_bytes(b"\x89PNG binary")
            code, out, _ = run([str(root), "--no-terms", "--allow-unscanned"])
            self.assertEqual(code, 1)
            self.assertIn("[C]", out)


class EntryPointTests(unittest.TestCase):
    def test_an_unreadable_candidate_exits_2_rather_than_reporting_it_clean(self) -> None:
        with candidate({"README.md": "clean\n"}) as root:
            with unittest.mock.patch.object(
                Path, "read_text", side_effect=OSError("device not ready")
            ):
                code, out, err = run([str(root), "--no-terms"])
        self.assertEqual(code, 2)
        self.assertIn("cannot read the publication candidate", err)
        self.assertIn("unchecked, not clean", out)

    def test_the_script_runs_as_a_command(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "check_egress.py"
        with candidate({"meta.yaml": "x-internal-cost-centre: 1182\n"}) as root:
            argv = ["check_egress.py", str(root), "--no-terms"]
            with unittest.mock.patch.object(sys, "argv", argv):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    with self.assertRaises(SystemExit) as raised:
                        runpy.run_path(str(script), run_name="__main__")
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("[C] reserved namespace", out.getvalue())


class ExclusionTests(unittest.TestCase):
    """An exclusion is for a file that must carry the shapes, and it is loud."""

    def test_an_excluded_file_is_printed_and_not_scanned(self) -> None:
        with candidate({"docs/guide.md": "the reserved prefix is x-internal-\n"}):
            code, out, _ = run(["docs", "--no-terms", "--exclude", "docs/guide.md"])
            self.assertEqual(code, 0, out)
            self.assertIn("excluded by --exclude, not checked: docs/guide.md", out)
            self.assertNotIn("[C]", out)

    def test_an_exclusion_does_not_cover_its_neighbours(self) -> None:
        files = {
            "docs/guide.md": "the reserved prefix is x-internal-\n",
            "docs/other.md": "x-internal-ledger: 4\n",
        }
        with candidate(files):
            code, out, _ = run(["docs", "--no-terms", "--exclude", "docs/guide.md"])
            self.assertEqual(code, 1)
            self.assertIn("docs/other.md", out)
            self.assertIn("[C]", out)

    def test_an_exclusion_is_the_path_not_the_file_name(self) -> None:
        # A bare basename would exclude every file of that name at any depth,
        # so adding docs/nested/guide.md would stop it being scanned without
        # anyone changing the exclusion. The pattern matches the whole path.
        files = {
            "docs/guide.md": "the reserved prefix is x-internal-\n",
            "docs/nested/guide.md": "x-internal-ledger: 4\n",
        }
        with candidate(files):
            code, out, _ = run(["docs", "--no-terms", "--exclude", "docs/guide.md"])
            self.assertEqual(code, 1)
            self.assertIn("docs/nested/guide.md", out)
            self.assertIn("[C]", out)

    def test_a_pattern_that_matches_nothing_exits_2(self) -> None:
        # A stale exclusion protects nothing and hides the next file that
        # needs looking at, so it fails rather than passing quietly.
        with candidate({"README.md": "clean\n"}):
            code, out, err = run([".", "--no-terms", "--exclude", "gone.md"])
            self.assertEqual(code, 2)
            self.assertIn("matched no file in the candidate: gone.md", err)
            self.assertIn("unchecked, not clean", out)

    def test_a_glob_excludes_a_family_of_files(self) -> None:
        files = {"docs/a.md": "x-internal-one\n", "docs/b.md": "x-internal-two\n"}
        with candidate(files):
            code, out, _ = run(["docs", "--no-terms", "--exclude", "docs/*.md"])
            self.assertEqual(code, 0, out)
            self.assertEqual(out.count("excluded by --exclude"), 2)

    def test_this_repository_can_check_its_own_published_prose(self) -> None:
        # The invocation CI runs, from the repository root. The gate's own
        # documentation names the reserved namespace, so it is excluded by its
        # path; the rest has to keep passing, and the exclusion has to keep
        # being needed, because one that stops matching is an error.
        root = Path(__file__).resolve().parents[1]
        with contextlib.chdir(root):
            code, out, err = run([
                "README.md", "RELEASE_NOTES.md", "CONTRIBUTING.md",
                "SECURITY.md", "AGENTS.md", "docs",
                "--no-terms", "--allow-structural",
                "--exclude", "docs/egress-check.md",
            ])
        self.assertEqual(code, 0, out + err)
        self.assertIn("excluded by --exclude, not checked: docs/egress-check.md", out)


class PrivateAddressTests(unittest.TestCase):
    """Every RFC 1918 range, not just the two that came to mind first."""

    def test_each_private_range_is_a_finding(self):
        for label, address in (
            ("10/8", "10.1.2.3"),
            ("172.16/12 low", "172.16.0.1"),
            ("172.16/12 mid", "172.24.8.9"),
            ("172.16/12 high", "172.31.255.254"),
            ("192.168/16", "192.168.0.14"),
            ("loopback", "127.0.0.1"),
        ):
            with self.subTest(label), candidate({"notes.md": f"host {address} here\n"}):
                code, out, _ = run([".", "--no-terms"])
                self.assertEqual(code, 1, out)
                self.assertIn("[B] private ipv4", out)

    def test_public_addresses_either_side_of_the_range_are_not_findings(self):
        # The near-miss negatives: 172.15 and 172.32 are public.
        text = "edge hosts 172.15.0.1 and 172.32.0.1 are public\n"
        with candidate({"notes.md": text}):
            code, out, _ = run([".", "--no-terms"])
            self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
