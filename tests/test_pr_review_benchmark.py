from __future__ import annotations

import copy
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from test_pr_review import POLICY, POLICY_SHA, ROOT, agent, benchmark, model_report, review


class BenchmarkFailureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.output = self.root / "out"
        self.manifest_path = self.output / "manifest.json"
        self.cases = review.read_json(ROOT / "benchmarks/pr-review-cases.json")["cases"][-3:]
        self.cases_path, self.policy_path = self.root / "cases.json", self.root / "policy.json"
        review.write_json(self.cases_path, {"cases": self.cases})
        review.write_json(self.policy_path, POLICY)
        self.args = ["--cases", str(self.cases_path), "--policy", str(self.policy_path),
                     "--policy-sha", POLICY_SHA, "--output", str(self.output), "--limit", "3"]
        for patch in (mock.patch.object(benchmark, "scan_context"),
                      redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            patch.__enter__()
            self.addCleanup(patch.__exit__, None, None, None)

    def test_first_preparation_failure_preserves_selected_unstarted_population(self):
        def fail(*args):
            persisted = review.read_json(self.manifest_path)
            self.assertEqual([row["state"] for row in persisted["records"]],
                             ["preparing", "unstarted", "unstarted"])
            raise review.ReviewError("fabricated_preparation_failure")
        with mock.patch.object(benchmark, "prepare_case", side_effect=fail), mock.patch.object(agent, "review_snapshot") as paid:
            self.assertEqual(benchmark.main([*self.args, "--run"]), 1)
            paid.assert_not_called()
        data = review.read_json(self.manifest_path)
        self.assertEqual([row["state"] for row in data["records"]],
                         ["preparation_failed", "unstarted", "unstarted"])
        self.assertFalse(data["finished"])
        self.assertEqual(data["stop_stage"], "preparation")
        self.assertEqual(data["cases"], [])
        before = self.manifest_path.read_bytes()
        self.assertEqual(benchmark.main([*self.args, "--run"]), 1)
        self.assertEqual(self.manifest_path.read_bytes(), before)

    def test_later_model_failure_retains_completed_case_and_stops_batch(self):
        with mock.patch.object(agent, "review_snapshot", side_effect=[model_report(), review.ReviewError("fixture"), model_report()]) as paid:
            self.assertEqual(benchmark.main([*self.args, "--run"]), 1)
            self.assertEqual(paid.call_count, 2)
        data = review.read_json(self.manifest_path)
        self.assertEqual([row["state"] for row in data["records"]],
                         ["complete_report", "model_failed", "unstarted"])
        self.assertEqual(data["cases"], [self.cases[0]["id"]])
        self.assertEqual(data["stop_stage"], "model")
        self.assertEqual(data["records"][0]["report_sha256"], review.digest(model_report()))

    def test_report_storage_failure_keeps_prior_receipt_and_no_later_calls(self):
        def paid(snapshot, policy, *, receipt_path):
            review.write_json(receipt_path, {"fixture_known_bill_usd": 0.01})
            return model_report()
        with mock.patch.object(agent, "review_snapshot", side_effect=paid) as calls, mock.patch.object(benchmark, "write_report", side_effect=OSError("fixture")):
            self.assertEqual(benchmark.main([*self.args, "--run"]), 1)
            self.assertEqual(calls.call_count, 1)
        self.assertEqual(review.read_json(self.output / f"{self.cases[0]['id']}.review.receipts.json"),
                         {"fixture_known_bill_usd": 0.01})
        self.assertEqual([row["state"] for row in review.read_json(self.manifest_path)["records"]],
                         ["report_failed", "unstarted", "unstarted"])

    def test_prepared_cache_requires_same_configuration_and_preserves_old_evidence(self):
        self.assertEqual(benchmark.main(self.args), 0)
        before = self.manifest_path.read_bytes()
        changed = copy.deepcopy(POLICY)
        changed["output_tokens"] = 12288
        review.write_json(self.policy_path, changed)
        self.assertEqual(benchmark.main([*self.args, "--run"]), 1)
        self.assertEqual(self.manifest_path.read_bytes(), before)
        review.write_json(self.policy_path, POLICY)
        self.assertEqual(benchmark.main([*self.args, "--offset", "1", "--run"]), 1)
        self.assertEqual(self.manifest_path.read_bytes(), before)
        path = self.output / f"{self.cases[0]['id']}.snapshot.json"
        snapshot = review.read_json(path)
        snapshot["head"] = "f" * 40
        review.write_json(path, snapshot)
        with mock.patch.object(agent, "review_snapshot") as paid:
            self.assertEqual(benchmark.main([*self.args, "--run"]), 1)
            paid.assert_not_called()

    def test_atomic_write_failure_preserves_prior_projection_and_removes_temporary_file(self):
        self.output.mkdir()
        self.manifest_path.write_bytes(b"prior\n")
        with mock.patch.object(benchmark.os, "replace", side_effect=OSError("fixture")), self.assertRaises(OSError):
            benchmark.save_manifest(self.manifest_path, {"state": "new"})
        self.assertEqual(self.manifest_path.read_bytes(), b"prior\n")
        self.assertEqual(list(self.output.iterdir()), [self.manifest_path])

    def test_checkpoint_failure_stops_before_preparation_or_payment(self):
        save = benchmark.save_manifest
        calls = 0

        def fail_after_selection(path, data):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise OSError("fixture")
            save(path, data)

        with mock.patch.object(benchmark, "save_manifest", side_effect=fail_after_selection), mock.patch.object(benchmark, "prepare_case") as prepare, mock.patch.object(agent, "review_snapshot") as paid:
            self.assertEqual(benchmark.main([*self.args, "--run"]), 1)
            prepare.assert_not_called()
            paid.assert_not_called()
        data = review.read_json(self.manifest_path)
        self.assertEqual([row["state"] for row in data["records"]], ["unstarted"] * 3)
        self.assertFalse(data["finished"])

    def test_invalid_selection_has_fixed_failure_state_and_no_paid_admission(self):
        for cases in ([self.cases[0], self.cases[0]], [{**self.cases[0], "id": "../outside"}],
                      [{**self.cases[0], "head": "bad"}]):
            review.write_json(self.cases_path, {"cases": cases})
            with mock.patch.object(agent, "review_snapshot") as paid:
                self.assertEqual(benchmark.main([*self.args, "--run"]), 1)
                paid.assert_not_called()
            data = review.read_json(self.manifest_path)
            self.assertEqual(data["stop_stage"], "selection")
            self.assertFalse(data["finished"])
            self.manifest_path.unlink()


if __name__ == "__main__":
    unittest.main()
