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
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        for name, text in files.items():
            write(root, name, text)
        yield root


def run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = check_egress.main(argv)
    return code, out.getvalue(), err.getvalue()


class LayerATests(unittest.TestCase):
    """Configured terms: the words that must never reach a public artefact."""

    def test_a_configured_term_is_a_finding(self) -> None:
        with candidate({"README.md": "Prepared for Northwind Holdings in June.\n"}) as root:
            terms = write(root.parent, "terms.txt", "Northwind Holdings\n")
            code, out, _ = run([str(root / "README.md"), "--terms", str(terms)])
            self.assertEqual(code, 1)
            self.assertIn("[A] configured term 'Northwind Holdings'", out)
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
            self.assertEqual(check_egress.load_terms(terms), ("Acme",))
            code, _, _ = run([str(root), "--terms", str(terms)])
            self.assertEqual(code, 0)

    def test_the_excerpt_is_bounded_and_does_not_reprint_the_paragraph(self) -> None:
        line = "%s Northwind Holdings %s" % ("filler " * 40, "trailing " * 40)
        with candidate({"notes.md": line + "\n"}) as root:
            terms = write(root.parent, "terms.txt", "Northwind Holdings\n")
            _, out, _ = run([str(root), "--terms", str(terms)])
            printed = [row for row in out.splitlines() if "[A]" in row][0]
            self.assertLess(len(printed), len(line))
            self.assertIn("...", printed)


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

    def test_a_file_that_is_not_scanned_as_text_is_reported_not_passed(self) -> None:
        with candidate({"README.md": "clean\n"}) as root:
            (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n binary")
            code, out, _ = run([str(root), "--no-terms"])
            self.assertEqual(code, 0)
            self.assertIn("not scanned as text: ", out)
            self.assertIn("logo.png", out)

    def test_undecodable_text_is_reported_rather_than_crashing(self) -> None:
        with candidate({"README.md": "clean\n"}) as root:
            (root / "broken.md").write_bytes(b"\xff\xfe not utf-8")
            code, out, _ = run([str(root), "--no-terms"])
            self.assertEqual(code, 0)
            self.assertIn("broken.md", out)


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


if __name__ == "__main__":
    unittest.main()


class ExclusionTests(unittest.TestCase):
    """An exclusion is for a file that must carry the shapes, and it is loud."""

    def test_an_excluded_file_is_printed_and_not_scanned(self) -> None:
        with candidate({"guide.md": "the reserved prefix is x-internal-\n"}) as root:
            code, out, _ = run([str(root), "--no-terms", "--exclude", "guide.md"])
            self.assertEqual(code, 0, out)
            self.assertIn("excluded by --exclude, not checked: ", out)
            self.assertIn("guide.md", out)
            self.assertNotIn("[C]", out)

    def test_an_exclusion_does_not_cover_its_neighbours(self) -> None:
        files = {
            "guide.md": "the reserved prefix is x-internal-\n",
            "other.md": "x-internal-ledger: 4\n",
        }
        with candidate(files) as root:
            code, out, _ = run([str(root), "--no-terms", "--exclude", "guide.md"])
            self.assertEqual(code, 1)
            self.assertIn("other.md", out)
            self.assertIn("[C]", out)

    def test_a_pattern_that_matches_nothing_exits_2(self) -> None:
        # A stale exclusion protects nothing and hides the next file that
        # needs looking at, so it fails rather than passing quietly.
        with candidate({"README.md": "clean\n"}) as root:
            code, out, err = run([str(root), "--no-terms", "--exclude", "gone.md"])
            self.assertEqual(code, 2)
            self.assertIn("matched no file in the candidate: gone.md", err)
            self.assertIn("unchecked, not clean", out)

    def test_a_glob_excludes_a_family_of_files(self) -> None:
        files = {"docs/a.md": "x-internal-one\n", "docs/b.md": "x-internal-two\n"}
        with candidate(files) as root:
            code, out, _ = run([str(root), "--no-terms", "--exclude", "*.md"])
            self.assertEqual(code, 0, out)
            self.assertEqual(out.count("excluded by --exclude"), 2)

    def test_this_repository_can_check_its_own_published_prose(self) -> None:
        # The gate's own documentation names the reserved namespace, so the
        # CI invocation excludes it. This pins that the rest still passes and
        # that the exclusion is still needed, because an exclusion that stops
        # matching is an error.
        root = Path(__file__).resolve().parents[1]
        code, out, err = run([
            str(root / "README.md"), str(root / "SECURITY.md"), str(root / "docs"),
            "--no-terms", "--allow-structural", "--exclude", "egress-check.md",
        ])
        self.assertEqual(code, 0, out + err)
        self.assertIn("egress-check.md", out)
