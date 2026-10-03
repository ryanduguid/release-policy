from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from test_pr_review import POLICY, POLICY_SHA, ROOT, agent, benchmark, clean_result, review

CONTROLS = ROOT / "benchmarks/pr-review-controls.json"


class QualificationControlsTests(unittest.TestCase):
    def setUp(self):
        self.cases = review.read_json(CONTROLS)["cases"]

    def test_fixed_set_reuses_the_four_clean_controls_and_adds_two_deletion_cases(self):
        original = review.read_json(ROOT / "benchmarks/pr-review-cases.json")["cases"]
        self.assertEqual(self.cases[:4], original[-4:])
        self.assertEqual([case["id"] for case in self.cases],
                         ["clean1", "clean2", "clean3", "clean4", "deletedguard", "removedfile"])
        self.assertTrue(all(case["synthetic"] and case["adjudication"] == "required"
                            for case in self.cases))

    def test_complete_before_and_after_source_is_prepared_without_github(self):
        with mock.patch.object(benchmark, "github", side_effect=AssertionError("network forbidden")):
            for case in self.cases:
                with self.subTest(case=case["id"]):
                    snapshot = benchmark.prepare_case(case, POLICY, POLICY_SHA)
                    review.verify_snapshot(snapshot, POLICY, POLICY_SHA)
                    self.assertEqual(snapshot["publication_capability"], "none")
                    self.assertEqual(snapshot["snapshot_kind"], "benchmark_v1")
                    self.assertEqual(snapshot["files"][0]["before"], case["source"]["before"])
                    self.assertEqual(snapshot["files"][0]["after"], case["source"]["after"])

    def test_deleted_guard_uses_current_file_bounds_and_does_not_reanchor_old_coordinates(self):
        snapshot = benchmark.prepare_case(self.cases[4], POLICY, POLICY_SHA)
        issue = {"relevant_file": "control.py", "issue_header": "[P2] Synthetic location control",
                 "issue_content": "Fabricated location check; human assessment is pending.",
                 "start_line": 4, "end_line": 4}
        result = {**clean_result(), "key_issues_to_review": [issue]}
        with self.assertRaisesRegex(review.ReviewError, "invalid_finding_location"):
            review.validate_review(result, snapshot["files"])
        self.assertEqual(issue["start_line"], 4)
        issue.update(start_line=3, end_line=3)
        review.validate_review(result, snapshot["files"])

    def test_removed_file_uses_before_bounds(self):
        snapshot = benchmark.prepare_case(self.cases[5], POLICY, POLICY_SHA)
        issue = {"relevant_file": "control.py", "issue_header": "[P2] Synthetic location control",
                 "issue_content": "Fabricated location check; human assessment is pending.",
                 "start_line": 4, "end_line": 4}
        result = {**clean_result(), "key_issues_to_review": [issue]}
        review.validate_review(result, snapshot["files"])
        issue.update(start_line=6, end_line=6)
        with self.assertRaisesRegex(review.ReviewError, "invalid_finding_location"):
            review.validate_review(result, snapshot["files"])

    def test_assessment_reference_metadata_is_not_in_the_snapshot(self):
        case = copy.deepcopy(self.cases[4])
        case["reference_findings"] = ["CONTROL_REFERENCE_SENTINEL"]
        case["human_assessment"] = {"outcome": "CONTROL_REFERENCE_SENTINEL"}
        snapshot = benchmark.prepare_case(case, POLICY, POLICY_SHA)
        self.assertNotIn("CONTROL_REFERENCE_SENTINEL", json.dumps(snapshot))

    def test_cli_prepares_all_six_controls_without_model_or_github_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "controls"
            with mock.patch.object(benchmark, "github", side_effect=AssertionError("network forbidden")), \
                    mock.patch.object(agent, "review_snapshot") as paid, \
                    mock.patch.object(benchmark, "scan_context") as scan, \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = benchmark.main(["--cases", str(CONTROLS), "--policy",
                                         str(ROOT / ".github/pr-review-policy.json"),
                                         "--policy-sha", POLICY_SHA, "--limit", "6",
                                         "--output", str(output)])
            self.assertEqual(result, 0)
            paid.assert_not_called()
            self.assertEqual(scan.call_count, 6)
            manifest = review.read_json(output / "manifest.json")
            self.assertEqual(manifest["cases"], [case["id"] for case in self.cases])
            self.assertEqual([record["state"] for record in manifest["records"]], ["prepared"] * 6)
            self.assertFalse(manifest["paid_calls_requested"])
            self.assertEqual(manifest["qualification"], "local_only")
            self.assertEqual(manifest["adjudication"], "pending_human_review")
            self.assertEqual(list(output.glob("*.review.*")), [])


if __name__ == "__main__":
    unittest.main()
