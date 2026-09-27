"""Consumers re-pin only the families a release-policy change moves.

The 20 September round re-pinned all 30 attribution consumers and every release
caller together, although release-python.yml and its scripts had not changed. These
checks run the family diff on a throwaway repository, so they need no clone history.
"""

from __future__ import annotations

import io
import runpy
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import pin_families  # noqa: E402

FILES = {
    ".github/workflows/release-python.yml": "run: . policy/scripts/gates.sh\n",
    ".github/workflows/release-archive.yml": "run: . policy/scripts/gates.sh\n"
                                             "uses: ./.github/workflows/publish-archives.yml\n",
    ".github/workflows/publish-archives.yml": "run: policy/scripts/publish_archives.sh\n",
    ".github/workflows/release-skills.yml": "uses: ./.github/workflows/publish-archives.yml\n",
    ".github/workflows/attribution-policy.yml": "uses: ./.github/actions/no-ai-attribution\n",
    ".github/actions/no-ai-attribution/action.yml": "runs: scan\n",
    "scripts/gates.sh": '"$PYTHON" "$GATES_DIR/python_release.py" tag\n',
    "scripts/python_release.py": "print('release')\n",
    "scripts/publish_archives.sh": '# see release-python.yml\n. "$(dirname "$0")/publish_common.sh"\n',
    "scripts/publish_common.sh": "true\n",
    "docs/guide.md": "prose\n",
}


class PinFamilyTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.test")
        self.git("config", "user.name", "Fixture")
        self.commits = [self.commit(FILES)]

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True,
                              check=True).stdout.strip()

    def commit(self, files: dict[str, str]) -> str:
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "fixture")
        return self.git("rev-parse", "HEAD")

    def moved(self, files: dict[str, str]) -> set[str]:
        before = self.git("rev-parse", "HEAD")
        after = self.commit(files)
        return {family for family, paths in pin_families.changed(before, after, self.root).items() if paths}

    def test_families_follow_workflows_actions_and_scripts(self) -> None:
        head = self.commits[0]
        self.assertEqual(pin_families.inputs(".github/workflows/release-python.yml", head, self.root), {
            ".github/workflows/release-python.yml", "scripts/gates.sh", "scripts/python_release.py"})
        # The comment naming release-python.yml does not tie the archive family to it.
        self.assertEqual(pin_families.inputs(".github/workflows/release-archive.yml", head, self.root), {
            ".github/workflows/release-archive.yml", ".github/workflows/publish-archives.yml",
            "scripts/gates.sh", "scripts/python_release.py", "scripts/publish_archives.sh",
            "scripts/publish_common.sh"})

    def test_only_the_moved_families_are_named(self) -> None:
        self.assertEqual(self.moved({".github/actions/no-ai-attribution/action.yml": "runs: scan2\n"}),
                         {"attribution"})
        # A script two steps from the workflow still moves its family.
        self.assertEqual(self.moved({"scripts/python_release.py": "print('changed')\n"}),
                         {"release-python", "release-archive"})
        self.assertEqual(self.moved({"scripts/publish_common.sh": "false\n"}),
                         {"release-archive", "release-skills"})
        self.assertEqual(self.moved({"docs/guide.md": "more prose\n"}), set())

    def test_the_real_release_python_family_is_the_audited_set(self) -> None:
        found = pin_families.inputs(".github/workflows/release-python.yml", "HEAD")
        self.assertTrue({".github/workflows/release-python.yml", "scripts/gates.sh",
                         "scripts/required_checks.py", "scripts/python_release.py",
                         "scripts/publish_python.sh", "scripts/publish_common.sh"} <= found, found)
        self.assertNotIn(".github/workflows/attribution-policy.yml", found)

    def test_prints_one_verdict_per_family(self) -> None:
        moved = {"release-python": [], "attribution": [".github/actions/no-ai-attribution/action.yml"]}
        with mock.patch.object(pin_families, "changed", return_value=moved), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(pin_families.main(["old", "new"]), 0)
        self.assertEqual(out.getvalue().splitlines(), [
            "release-python: keep the pin",
            "attribution: re-pin (.github/actions/no-ai-attribution/action.yml)"])
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(pin_families.main(["old"]), 2)
        self.assertEqual(err.getvalue().strip(), "Usage: python scripts/pin_families.py OLD_COMMIT NEW_COMMIT")

    def test_runs_as_a_script(self) -> None:
        argv = ["pin_families.py", "HEAD", "HEAD"]
        with mock.patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path(str(ROOT / "scripts" / "pin_families.py"), run_name="__main__")
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(out.getvalue().splitlines(), [f"{family}: keep the pin" for family in pin_families.FAMILIES])


if __name__ == "__main__":
    unittest.main()
