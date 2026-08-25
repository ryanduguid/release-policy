from __future__ import annotations

from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import verify_skills  # noqa: E402


class SkillVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self._git("init", "--quiet")
        self._git("config", "user.email", "tests@example.invalid")
        self._git("config", "user.name", "Verifier tests")
        self._write("VERSION", b"1.2.3\n")
        self._write("requirements-test.txt", b"")
        self._write("scripts/validate_validation.py", b"pass\n")
        self._write("tests/verify_skills_cli.py", b"pass\n")
        self._write("tests/test_example.py", b"pass\n")
        self._git("add", "VERSION", "requirements-test.txt", "scripts/validate_validation.py", "tests/verify_skills_cli.py", "tests/test_example.py")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments], cwd=self.root, check=True, shell=False,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def _write(self, supplied: str, contents: bytes) -> Path:
        path = self.root / supplied
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        return path

    def _set_mode(self, supplied: str, mode: str) -> None:
        self._git("update-index", "--add", "--cacheinfo", f"{mode}," + "1" * 40 + f",{supplied}")

    def test_guard_accepts_supported_mode_and_rejects_empty_unknown_and_frozen_v010(self) -> None:
        verify_skills.guard_skill_release("subcontractor-accounting-v1", "v1.2.3")
        for mode, tag in (("", "v1.2.3"), ("unknown", "v1.2.3"), ("subcontractor-accounting-v1", "v0.1.0")):
            with self.subTest(mode=mode, tag=tag), self.assertRaises(verify_skills.VerificationError):
                verify_skills.guard_skill_release(mode, tag)

    def test_valid_fixture_runs_exact_argument_arrays_in_order_without_shell(self) -> None:
        calls: list[tuple[tuple[str, ...], Path, bool, bool]] = []

        def runner(command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[object]:
            calls.append((command, kwargs["cwd"], kwargs["check"], kwargs["shell"]))  # type: ignore[arg-type]
            return subprocess.CompletedProcess(command, 0)

        verify_skills.verify_skill_pack(self.root, "subcontractor-accounting-v1", "VERSION", runner=runner)

        self.assertEqual(list(verify_skills.commands_for_mode("subcontractor-accounting-v1")), [call[0] for call in calls])
        self.assertEqual(4, len(calls))
        for _, cwd, check, shell in calls:
            self.assertEqual(self.root, cwd)
            self.assertTrue(check)
            self.assertFalse(shell)

    def test_each_of_four_command_failures_stops_immediately(self) -> None:
        for failing_index in range(4):
            calls: list[tuple[str, ...]] = []

            def runner(command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[object]:
                calls.append(command)
                if len(calls) - 1 == failing_index:
                    raise subprocess.CalledProcessError(1, command)
                return subprocess.CompletedProcess(command, 0)

            with self.subTest(failing_index=failing_index), self.assertRaises(subprocess.CalledProcessError):
                verify_skills.verify_skill_pack(self.root, "subcontractor-accounting-v1", "VERSION", runner=runner)
            self.assertEqual(failing_index + 1, len(calls))

    def test_required_files_reject_missing_untracked_symlink_gitlink_and_directory(self) -> None:
        with self.assertRaises(verify_skills.VerificationError):
            verify_skills.require_tracked_regular_file(self.root, "missing", label="required")
        self._write("untracked", b"x")
        with self.assertRaises(verify_skills.VerificationError):
            verify_skills.require_tracked_regular_file(self.root, "untracked", label="required")
        directory = self.root / "directory"
        directory.mkdir()
        self._git("add", "directory")
        with self.assertRaises(verify_skills.VerificationError):
            verify_skills.require_tracked_regular_file(self.root, "directory", label="required")
        link = self.root / "link"
        try:
            link.symlink_to(self.root / "VERSION")
        except OSError as error:
            self.skipTest(f"symlink fixtures are unavailable: {error}")
        self._git("add", "link")
        with self.assertRaises(verify_skills.VerificationError):
            verify_skills.require_tracked_regular_file(self.root, "link", label="required")
        self._set_mode("gitlink", "160000")
        with self.assertRaises(verify_skills.VerificationError):
            verify_skills.require_tracked_regular_file(self.root, "gitlink", label="required")
        self._set_mode("executable", "100755")
        self._write("executable", b"x")
        self.assertEqual(self.root / "executable", verify_skills.require_tracked_regular_file(self.root, "executable", label="required"))
        self._set_mode("ordinary", "100644")
        self._write("ordinary", b"x")
        self.assertEqual(self.root / "ordinary", verify_skills.require_tracked_regular_file(self.root, "ordinary", label="required"))

    def test_path_rejects_absolute_backslash_traversal_parent_symlink_and_outside_root(self) -> None:
        for supplied in ("C:/outside", "/outside", "bad\\path", "../outside", "tests/../VERSION"):
            with self.subTest(supplied=supplied), self.assertRaises(verify_skills.VerificationError):
                verify_skills.resolve_safe_relative(self.root, supplied, label="path")
        outside_temporary = TemporaryDirectory()
        self.addCleanup(outside_temporary.cleanup)
        outside = Path(outside_temporary.name)
        parent = self.root / "parent"
        try:
            parent.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"symlink fixtures are unavailable: {error}")
        (outside / "file").write_text("x", encoding="utf-8")
        with self.assertRaises(verify_skills.VerificationError):
            verify_skills.resolve_safe_relative(self.root, "parent/file", label="path")
        outside_file = outside / "outside"
        outside_file.write_text("x", encoding="utf-8")
        link = self.root / "outside-link"
        link.symlink_to(outside_file)
        with self.assertRaises(verify_skills.VerificationError):
            verify_skills.resolve_safe_relative(self.root, "outside-link", label="path")

    def test_tests_directory_requires_real_directory_and_tracked_regular_test(self) -> None:
        self.assertEqual((self.root / "tests" / "test_example.py",), verify_skills.require_tracked_test_files(self.root))
        self._git("rm", "--cached", "tests/test_example.py")
        with self.assertRaises(verify_skills.VerificationError):
            verify_skills.require_tracked_test_files(self.root)
        self._git("add", "tests/test_example.py")
        self._git("rm", "--cached", "tests/test_example.py")
        (self.root / "tests" / "test_example.py").unlink()
        self._git("rm", "--cached", "tests/verify_skills_cli.py")
        (self.root / "tests" / "verify_skills_cli.py").unlink()
        (self.root / "tests").rmdir()
        self._write("tests", b"not a directory")
        self._git("add", "tests")
        with self.assertRaises(verify_skills.VerificationError):
            verify_skills.require_tracked_test_files(self.root)

    def test_version_accepts_one_canonical_line_and_rejects_bom_encoding_and_bad_semver(self) -> None:
        version = self.root / "VERSION"
        for contents, expected in ((b"1.2.3", "1.2.3"), (b"1.2.3\n", "1.2.3"), (b"1.2.3\r\n", "1.2.3")):
            version.write_bytes(contents)
            with self.subTest(contents=contents):
                self.assertEqual(expected, verify_skills.read_canonical_version(version))
        for contents in (b"\xef\xbb\xbf1.2.3\n", b"\xff", b"", b"1.2.3\n2.3.4\n", b"v1.2.3\n", b"01.2.3\n", b"1.2.3.4\n"):
            version.write_bytes(contents)
            with self.subTest(contents=contents), self.assertRaises(verify_skills.VerificationError):
                verify_skills.read_canonical_version(version)

    def test_cli_returns_nonzero_for_guard_and_verification_errors(self) -> None:
        stderr = StringIO()
        with redirect_stderr(stderr):
            self.assertNotEqual(0, verify_skills.main(["guard-release", "--mode", "unknown", "--tag", "v1.2.3"]))
        self.assertIn("verify_skills:", stderr.getvalue())
        with mock.patch.object(verify_skills, "verify_skill_pack", side_effect=verify_skills.VerificationError("bad fixture")):
            with redirect_stderr(StringIO()):
                self.assertNotEqual(0, verify_skills.main(["verify", "--mode", "subcontractor-accounting-v1"]))


if __name__ == "__main__":
    unittest.main()
