"""Command entry points and short-circuit branches the module tests do not reach."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_release_archives as release_archives  # noqa: E402
import test_pr_review as review_fixtures  # noqa: E402
import verify_skills  # noqa: E402


class EntryPointTests(unittest.TestCase):
    def test_archive_builder_command_passes_its_arguments_through(self) -> None:
        with mock.patch.object(release_archives, "build_release_archives") as build:
            code = release_archives.main([
                "--commit", "HEAD", "--prefix", "example/", "--source-directory", "pkg",
                "--output-base", "dist/example",
            ])
        self.assertEqual(code, 0)
        build.assert_called_once_with(
            commit="HEAD", prefix="example/", source_directory="pkg",
            output_base=Path("dist/example"),
        )

    def test_skill_release_guard_command_accepts_a_supported_mode(self) -> None:
        self.assertEqual(
            verify_skills.main(["guard-release", "--mode", "subcontractor-accounting-v1",
                                "--tag", "v1.2.3"]),
            0,
        )

    def test_a_failed_job_or_missing_report_is_a_failed_review(self) -> None:
        snapshot = review_fixtures.snapshot()
        for report, jobs_ok in ((None, True), ({"schema": 2}, False)):
            with self.subTest(jobs_ok=jobs_ok):
                self.assertEqual(
                    review_fixtures.review.assess_report(
                        snapshot, report, review_fixtures.POLICY, review_fixtures.POLICY_SHA,
                        review_fixtures.fake_fetch(), jobs_ok,
                    ),
                    "failure",
                )


if __name__ == "__main__":
    unittest.main()
