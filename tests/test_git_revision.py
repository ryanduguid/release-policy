"""Commit identity is established before revisions reach other Git operations."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import git_revision  # noqa: E402


class GitRevisionTests(unittest.TestCase):
    def test_normal_revisions_peel_to_one_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def git(*args: str) -> str:
                return subprocess.run(
                    ["git", *args], cwd=root, check=True, capture_output=True, text=True
                ).stdout.strip()

            git("init", "-q", "-b", "main")
            git("config", "user.name", "Fixture")
            git("config", "user.email", "fixture@example.test")
            git("config", "maintenance.auto", "false")
            git("commit", "--allow-empty", "-qm", "fixture")
            sha = git("rev-parse", "HEAD")
            git("tag", "-am", "fixture", "fixture-tag")
            for revision in ("HEAD", "main", "fixture-tag", sha[:12], sha):
                with self.subTest(revision=revision):
                    self.assertEqual(sha, git_revision.resolve_commit(root, revision))
            for revision in ("", "--help", "--format=%ct", "missing", "HEAD^{tree}"):
                with self.subTest(revision=revision), self.assertRaises(ValueError):
                    git_revision.resolve_commit(root, revision)

    def test_rejects_non_sha_output_even_from_a_successful_process(self) -> None:
        for output in ("", "help text\n", "a" * 40 + "\n" + "b" * 40 + "\n"):
            with self.subTest(output=output), mock.patch.object(
                git_revision.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 0, stdout=output),
            ), self.assertRaises(ValueError):
                git_revision.resolve_commit(ROOT, "HEAD")


if __name__ == "__main__":
    unittest.main()
