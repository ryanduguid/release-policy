# Copyright 2026 Ryan Duguid
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy
import importlib
import io
import json
import runpy
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
metrics = importlib.import_module("pr_review_measurements")
receipts = importlib.import_module("pr_review_receipts")
review = importlib.import_module("pr_review")
POLICY = json.loads((ROOT / ".github/pr-review-policy.json").read_bytes())


def journal():
    return receipts.ReceiptJournal(None, {"context_hash": "a" * 64}, [[{"path": "file.py"}]], 1).data


def received(data, index, *, valid=True, bill=0.1):
    """Record a response; bill replaces the computed cost to exercise the summed decimals."""
    model = review.MODELS[index]
    response = {"id": f"msg_control-{index}", "type": "message", "role": "assistant", "model": model,
                "stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 20}}
    metadata = receipts.metadata(response, model, POLICY["prices"][model], output_tokens=POLICY["output_tokens"])
    if bill != "computed":
        metadata["usage"]["cost"] = bill
    data["calls"][index].update(metadata,
                                transport_state="response_received", output_state="valid" if valid else "invalid",
                                diagnostic_category=None if valid else "strict_schema")


class MeasurementsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def save(self, data, name="receipts.json"):
        path = self.root / name
        review.write_json(path, data)
        return path

    def test_known_costs_use_recorded_decimals_and_unstarted_is_not_zero_cost(self):
        data = journal()
        received(data, 0)
        result = metrics.summarise([("attempt1", self.save(data))])
        self.assertEqual(result["known_billed_subtotal_usd"], "0.1")
        self.assertTrue(result["accounting_complete"])
        self.assertEqual(result["fully_completed_pairs"], 0)
        self.assertEqual(result["models"][review.MODELS[1]]["counts"]["unstarted"], 1)
        self.assertEqual(result["models"][review.MODELS[1]]["accepted_per_received_call"],
                         {"numerator": 0, "denominator": 0, "fraction": None})
        received(data, 1, bill=0.2)
        data["finished"] = True
        result = metrics.summarise([("attempt1", self.save(data))])
        self.assertEqual(result["known_billed_subtotal_usd"], "0.3")
        self.assertEqual(result["fully_completed_pairs"], 1)
        self.assertEqual(result["human_accuracy"], "not_measured")
        self.assertEqual(result["models"][review.MODELS[0]]["accepted_per_received_call"]["numerator"], 1)

    def test_recorded_cost_is_the_computed_upper_bound(self):
        data = journal()
        received(data, 0, bill="computed")
        received(data, 1, bill="computed")
        data["finished"] = True
        total = Decimal(metrics.summarise([("attempt1", self.save(data))])["known_billed_subtotal_usd"])
        # 10 input and 20 output tokens: US$0.00044 at Opus prices and US$0.00022 at Sonnet prices.
        self.assertGreaterEqual(total, Decimal("0.00066"))
        self.assertLess(total, Decimal("0.00066") + Decimal("1e-18"))

    def test_pending_transport_and_rejection_keep_unknown_bills_unknown(self):
        for state in ("request_intended", "response_observed", "response_received"):
            data = journal()
            data["calls"][0]["transport_state"] = state
            received(data, 1, valid=False, bill=None)
            result = metrics.summarise([("attempt1", self.save(data))])
            self.assertEqual(result["unknown_bills"], 2)
            self.assertFalse(result["accounting_complete"])
            self.assertEqual(result["models"][review.MODELS[1]]["rejections"], {"strict_schema": 1})
        data["calls"][1].update(output_state="indeterminate", diagnostic_category="unexpected_runtime")
        result = metrics.summarise([("attempt1", self.save(data))])
        self.assertEqual(result["models"][review.MODELS[1]]["counts"]["indeterminate"], 1)

    def test_replayed_artifacts_deduplicate_by_attempt_but_separate_attempts_are_billed(self):
        data = journal()
        received(data, 0)
        path = self.save(data)
        result = metrics.summarise([("attempt1", path), ("attempt1", path), ("attempt2", path)])
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["duplicate_evidence_copies"], 1)
        self.assertEqual(result["known_billed_subtotal_usd"], "0.2")
        self.assertEqual(result["repeated_generation_occurrences"], 1)
        changed = copy.deepcopy(data)
        changed["calls"][0]["usage"]["cost"] = 0.11
        with self.assertRaisesRegex(review.ReviewError, "conflicting_measurement_attempt"):
            metrics.summarise([("attempt1", path), ("attempt1", self.save(changed, "changed.json"))])
        with self.assertRaisesRegex(review.ReviewError, "invalid_measurement_attempt"):
            metrics.summarise([("../private", path)])
        with self.assertRaisesRegex(review.ReviewError, "empty_measurement_input"):
            metrics.summarise([])

    def test_numeric_only_usage_and_extreme_precision(self):
        data = journal()
        data["updated_at"] = "2026-10-03T01:00:00Z"
        received(data, 0)
        data["calls"][0]["usage"]["thinking_tokens"] = 15
        path = self.save(data)
        raw = path.read_text().replace('"cost":0.1', '"cost":5e-324')
        path.write_text(raw)
        total = metrics.summarise([("first", path), ("second", path)])["known_billed_subtotal_usd"]
        self.assertEqual(Decimal(total), Decimal("1e-323"))
        data["calls"][0]["usage"]["thinking_tokens"] = 21
        with self.assertRaises(review.ReviewError):
            metrics.read_receipt(self.save(data))
        for value in (True, -1, float("nan"), float("inf"), "0.1", Decimal("1e-325"), Decimal("1e309"), 10**24):
            self.assertFalse(metrics.money(value))
        data = journal()
        received(data, 0, valid=False, bill=1e100)
        received(data, 1, bill=0.1)
        total = metrics.summarise([("overspend", self.save(data))])["known_billed_subtotal_usd"]
        self.assertEqual(Decimal(total), Decimal("1" + "0" * 100 + ".1"))

    def test_untrusted_fields_and_contradictory_completion_are_rejected(self):
        mutations = [lambda data: data.update(source="private source"),
                     lambda data: data.update(finished=True),
                     lambda data: data.update(calls=data["calls"][:1]),
                     lambda data: data.update(schema="receipt_journal_v4"),
                     lambda data: data["calls"][0].update(provider="other"),
                     lambda data: data["calls"][0].update(stop_reason="private-value"),
                     lambda data: data["calls"][0].update(model_matches="yes"),
                     lambda data: data["calls"][0]["usage"].update(reasoning_tokens=None),
                     lambda data: data["calls"][0]["usage"].update(cost=0),
                     lambda data: data["calls"][0].update(output_state="valid"),
                     lambda data: data["calls"][1].update(chunk_hash="b" * 64),
                     lambda data: data["calls"][0].update(generation_id_sha256="msg_raw"),
                     lambda data: data["calls"][0]["usage"].update(output_tokens=True)]
        for mutate in mutations:
            data = journal()
            mutate(data)
            with self.subTest(mutate=mutate), self.assertRaises(review.ReviewError):
                metrics.read_receipt(self.save(data))
        for raw in ('{"schema":1,"schema":2}', '{"usage":NaN}', '"source text"',
                    " " * (receipts.MAX_JOURNAL_BYTES + 1)):
            path = self.root / "malformed.json"
            path.write_text(raw)
            with self.subTest(raw=raw[:20]), self.assertRaises(review.ReviewError):
                metrics.read_receipt(path)

    def test_cli_writes_only_summary_and_has_fixed_failure_text(self):
        path = self.save(journal())
        output = self.root / "summary.json"
        args = ["--journal", f"attempt1={path}", "--output", str(output)]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(metrics.main(args), 0)
            self.assertEqual(review.read_json(output)["attempts"], 1)
            before = path.read_bytes()
            self.assertEqual(metrics.main(["--journal", f"attempt1={path}", "--output", str(path)]), 1)
            self.assertEqual(path.read_bytes(), before)
            alias = self.root / "linked-output.json"
            alias.hardlink_to(path)
            self.assertEqual(metrics.main(["--journal", f"attempt1={path}", "--output", str(alias)]), 1)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(metrics.main(["--journal", "bad", "--output", str(output)]), 1)
            self.assertEqual(metrics.main(["--journal", "attempt1=private-missing.json", "--output", str(output)]), 1)
            self.assertNotIn("private", errors.getvalue())
            with mock.patch.object(sys, "argv", ["pr_review_measurements.py", *args]), self.assertRaises(SystemExit) as caught:
                runpy.run_path(str(ROOT / "scripts/pr_review_measurements.py"), run_name="__main__")
            self.assertEqual(caught.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
