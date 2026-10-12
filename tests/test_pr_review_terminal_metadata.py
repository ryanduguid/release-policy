from __future__ import annotations

import copy
import importlib
import io
import json
import os
import runpy
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

from test_pr_review import (
    BASE,
    HEAD,
    POLICY,
    POLICY_SHA,
    PR,
    REPO,
    ROOT,
    agent,
    clean_result,
    completion,
    fake_fetch,
    metadata,
    review,
    snapshot,
)

receipts = importlib.import_module("pr_review_receipts")
metrics = importlib.import_module("pr_review_measurements")


def lookup_response(selected_model=review.MODELS[0], **changes):
    return {"data": {"id": completion(selected_model)["id"], "model": selected_model,
                     "provider_name": POLICY["routes"][selected_model]["name"],
                     "total_cost": Decimal("0.0001"), "latency": Decimal("1200.5"),
                     "generation_time": 900, **changes}}


class TerminalMetadataTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.policy = self.root / "policy.json"
        review.write_json(self.policy, POLICY)
        self.args = ["--policy", str(self.policy), "--policy-sha", POLICY_SHA,
                     "--snapshot", str(self.root / "snapshot.json"), "--repo", REPO]
        for context in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()),
                        mock.patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "1", "GITHUB_ACTIONS": "false",
                            "GITHUB_OUTPUT": str(self.root / "output"),
                            "GITHUB_STEP_SUMMARY": str(self.root / "summary")})):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)

    def test_closed_capture_has_fixed_disposition_without_source_or_status(self):
        api = mock.Mock(wraps=fake_fetch(pr=metadata(state="closed")))
        with mock.patch.object(review, "github", api), mock.patch.object(review, "collect") as source:
            self.assertEqual(review.main(["capture", *self.args, "--pr", str(PR), "--publish-pending"]), 0)
        source.assert_not_called()
        self.assertEqual(len(api.call_args_list), 1)
        self.assertFalse((self.root / "snapshot.json").exists())
        self.assertFalse((self.root / "snapshot.identity.json").exists())
        self.assertEqual((self.root / "output").read_text(), "capture_disposition=closed_before_admission\n")
        self.assertIn("No verdict", (self.root / "summary").read_text())
        with mock.patch.dict(os.environ, {}, clear=True):
            review.capture_disposition("closed_before_admission")
        with self.assertRaisesRegex(review.ReviewError, "invalid_capture_disposition"):
            review.capture_disposition("closed\npass=true")

    def test_drafts_private_invalid_identity_and_state_cannot_be_closed_skips(self):
        cases = [metadata(state="closed", draft=True), metadata(state="unknown")]
        for change in ("private", "sha", "id", "repository"):
            pr = metadata(state="closed")
            if change == "private":
                pr["base"]["repo"]["private"] = True
            elif change == "sha":
                pr["head"]["sha"] = "bad"
            elif change == "id":
                pr["base"]["repo"]["id"] = 0
            else:
                pr["base"]["repo"]["full_name"] = "other/repo"
            cases.append(pr)
        for pr in cases:
            with self.subTest(pr=pr), mock.patch.object(review, "github", fake_fetch(pr=pr)):
                self.assertEqual(review.main(["capture", *self.args, "--pr", str(PR)]), 1)
        self.assertFalse((self.root / "output").exists())
        with self.assertRaisesRegex(review.ReviewError, "pr_not_ready"):
            review.pr_metadata(fake_fetch(pr=metadata(state="closed")), REPO, PR)
        with mock.patch.object(review, "github") as api:
            self.assertEqual(review.main(["capture", *self.args, "--policy-sha", "bad", "--pr", str(PR)]), 1)
            api.assert_not_called()

    def test_closed_event_still_requires_author_and_bridge_provenance(self):
        closed = metadata(state="closed", user={"login": "outside"})
        for name in ("outside", "dependabot[bot]"):
            closed["user"]["login"] = name
            with self.assertRaises(review.ReviewError):
                review.resolve_event({"number": PR}, "pull_request_target", REPO,
                                     fake_fetch(pr=closed), allow_closed=True)
        closed["user"]["login"] = "example"
        self.assertEqual(review.resolve_event({"number": PR}, "pull_request_target", REPO,
                         fake_fetch(pr=closed), allow_closed=True), PR)
        self.assertEqual(review.resolve_event({"inputs": {"pr-number": str(PR)}}, "workflow_dispatch",
                         REPO, fake_fetch(pr=closed), allow_closed=True), PR)
        run = {"run_attempt": 1, "path": ".github/workflows/pr-review-trigger.yml",
               "event": "pull_request", "conclusion": "success", "head_sha": HEAD,
               "pull_requests": [{"number": PR}]}
        closed["user"]["login"] = "dependabot[bot]"
        def fetch(path):
            return run if "/actions/runs/" in path else closed
        event = {"workflow_run": {"id": 123}}
        self.assertEqual(review.resolve_event(event, "workflow_run", REPO, fetch, allow_closed=True), PR)
        run["head_sha"] = BASE
        with self.assertRaisesRegex(review.ReviewError, "stale_or_non_dependabot"):
            review.resolve_event(event, "workflow_run", REPO, fetch, allow_closed=True)

    def test_closure_after_pending_remains_failure_without_closed_disposition(self):
        api = mock.Mock(side_effect=[metadata(), {}, metadata(state="closed")])
        with mock.patch.object(review, "github", api):
            self.assertEqual(review.main(["capture", *self.args, "--pr", str(PR), "--publish-pending"]), 1)
        self.assertEqual(api.call_args_list[1].args[1]["state"], "pending")
        self.assertTrue((self.root / "snapshot.identity.json").exists())
        self.assertFalse((self.root / "output").exists())

    def test_open_capture_sets_disposition_only_after_snapshot_storage(self):
        with mock.patch.object(review, "github", fake_fetch()), mock.patch.object(review, "scan_context"):
            self.assertEqual(review.main(["capture", *self.args, "--pr", str(PR)]), 0)
        self.assertTrue((self.root / "snapshot.json").exists())
        self.assertEqual((self.root / "output").read_text(), "capture_disposition=captured\n")

    def test_workflow_skips_before_artifact_download_and_requires_exact_capture_output(self):
        text = (ROOT / ".github/workflows/pr-review.yml").read_text()
        self.assertIn("disposition: ${{ steps.capture_context.outputs.capture_disposition }}", text)
        self.assertIn("needs.capture.outputs.disposition == 'captured'", text)
        self.assertIn("!(needs.capture.result == 'success' && needs.capture.outputs.disposition == 'closed_before_admission')", text)
        self.assertIn('run: test "$CAPTURE_DISPOSITION" = captured', text)
        self.assertIn("steps.capture_context.outputs.capture_disposition != 'closed_before_admission'", text)

    def lookup(self, response=None, send=None):
        model = review.MODELS[0]
        return receipts.generation_metadata(completion(model) if response is None else response,
                    model, POLICY["routes"][model]["name"], "synthetic", send=send)

    def test_lookup_is_one_bounded_get_and_never_retains_extra_fields(self):
        response = lookup_response(prompt="private-canary", completion="private-canary",
                                   error="private-canary", publish=True, reservation=0)
        send = mock.Mock(return_value=response)
        result = self.lookup(send=send)
        self.assertEqual(result, {"state": "verified", "total_cost_usd": "0.0001",
                                 "provider_latency_ms": 1200.5, "provider_generation_time_ms": 900.0})
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args, ("https://openrouter.ai/api/v1/generation?id=gen-1-0", "synthetic"))
        self.assertEqual(send.call_args.kwargs, {"timeout": 10, "response_limit": 65536, "decimal_numbers": True})
        self.assertNotIn("private-canary", json.dumps(result))
        self.assertNotIn("gen-", json.dumps(result))

    def test_missing_invalid_or_unexpected_route_metadata_is_not_retained(self):
        send = mock.Mock()
        for value in (None, "gen-bad?query=x", "gen-line\nbreak", "gen-" + "x" * 130):
            self.assertEqual(self.lookup({"id": value}, send)["state"], "not_eligible")
        send.assert_not_called()
        for change in ({"id": "gen-other"}, {"model": "other"}, {"provider_name": "parasail"},
                       {"total_cost": True}, {"total_cost": "0"}, {"total_cost": Decimal("NaN")},
                       {"latency": -1}, {"generation_time": receipts.MAX_TIMING_MS + 1}):
            result = self.lookup(send=mock.Mock(return_value=lookup_response(**change)))
            self.assertEqual(result, receipts.empty_lookup("attempted_rejected"))
        result = self.lookup(completion(review.MODELS[0], model="other"),
                             mock.Mock(return_value=lookup_response()))
        self.assertEqual(result["state"], "attempted_rejected")
        for invalid in (None, [], {"data": []}):
            self.assertEqual(self.lookup(send=mock.Mock(return_value=invalid))["state"], "attempted_rejected")
        self.assertEqual(self.lookup(send=mock.Mock(return_value=lookup_response(latency=None, generation_time=None))),
                         {**receipts.empty_lookup("verified"), "total_cost_usd": "0.0001"})

    def test_lookup_failures_are_sanitised_and_never_retry(self):
        for error, state in ((review.ReviewError("http_404"), "attempted_unavailable"),
                             (review.ReviewError("oversized_api_response"), "attempted_rejected"),
                             (ValueError("private-gen-1-0"), "attempted_unavailable")):
            send = mock.Mock(side_effect=error)
            self.assertEqual(self.lookup(send=send), receipts.empty_lookup(state))
            self.assertEqual(send.call_count, 1)

    def test_worker_deadline_cancels_a_blocking_lookup_and_keeps_secrets_out_of_arguments(self):
        original = subprocess.run
        entered = []
        def stalled(arguments, **kwargs):
            entered.append((arguments, kwargs))
            return original([sys.executable, "-c", "import time; time.sleep(5)"], **kwargs)
        start = time.monotonic()
        with mock.patch.object(receipts.subprocess, "run", stalled), \
                mock.patch.object(receipts, "LOOKUP_TIMEOUT_SECONDS", 0.1):
            self.assertEqual(self.lookup(), receipts.empty_lookup("attempted_unavailable"))
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(len(entered), 1)
        arguments, kwargs = entered[0]
        self.assertNotIn("synthetic", json.dumps(arguments))
        self.assertNotIn("gen-1-0", json.dumps(arguments))
        self.assertEqual(arguments[-1], "--lookup")
        self.assertEqual(json.loads(kwargs["input"])["key"], "synthetic")
        self.assertNotIn("env", kwargs)

    def test_isolated_worker_output_is_strict_and_round_trips_without_network(self):
        valid = {**receipts.empty_lookup("verified"), "total_cost_usd": "0.0001"}
        for returncode, output, expected in ((0, json.dumps(valid).encode(), valid),
                (1, b"private-canary", receipts.empty_lookup("attempted_unavailable")),
                (0, b"x" * 4097, receipts.empty_lookup("attempted_unavailable")),
                (0, b'{"state":"private-canary"}', receipts.empty_lookup("attempted_unavailable"))):
            with mock.patch.object(receipts.subprocess, "run",
                    return_value=subprocess.CompletedProcess([], returncode, output, b"private-error")):
                result = self.lookup()
            self.assertEqual(result, expected)
            self.assertNotIn("private", json.dumps(result))
        for model in review.MODELS:
            payload = {"id": completion(model)["id"], "model": model,
                       "provider": POLICY["routes"][model]["name"], "key": "synthetic"}
            out = io.BytesIO()
            with mock.patch.object(receipts.sys, "stdin", mock.Mock(buffer=io.BytesIO(review.canonical(payload)))), \
                    mock.patch.object(receipts.sys, "stdout", mock.Mock(buffer=out)), \
                    mock.patch.object(receipts, "request_json", return_value=lookup_response(model)) as get:
                receipts.lookup_worker()
            self.assertEqual(json.loads(out.getvalue())["state"], "verified")
            self.assertEqual(get.call_count, 1)
            self.assertNotIn(b"gen-", out.getvalue())
        out = io.BytesIO()
        with mock.patch.object(sys, "stdin", mock.Mock(buffer=io.BytesIO(b"{}"))), \
                mock.patch.object(sys, "stdout", mock.Mock(buffer=out)), \
                mock.patch.object(review, "request_json") as get:
            runpy.run_path(str(ROOT / "scripts/pr_review_receipts.py"), run_name="__main__")
        get.assert_not_called()
        self.assertEqual(json.loads(out.getvalue())["state"], "attempted_unavailable")

    def test_required_nullable_timings_cannot_be_silently_omitted(self):
        for field in ("latency", "generation_time"):
            incomplete = lookup_response()
            del incomplete["data"][field]
            self.assertEqual(self.lookup(send=mock.Mock(return_value=incomplete)),
                             receipts.empty_lookup("attempted_rejected"))
        zero = self.lookup(send=mock.Mock(return_value=lookup_response(latency=0, generation_time=0)))
        self.assertEqual(zero["state"], "verified")
        self.assertEqual(zero["provider_latency_ms"], 0)
        self.assertEqual(zero["provider_generation_time_ms"], 0)

    def test_transport_uses_exact_decimal_bounds_and_refuses_redirects(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"data":{"total_cost":0.123456789012345678901}}'
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(review, "build_opener", return_value=opener):
            data = review.request_json("https://openrouter.ai/api/v1/generation?id=gen-test", "synthetic",
                                       timeout=10, response_limit=65536, decimal_numbers=True)
            self.assertEqual(data["data"]["total_cost"], Decimal("0.123456789012345678901"))
            self.assertEqual(opener.open.call_args.kwargs["timeout"], 10)
            response.__enter__.return_value.read.assert_called_with(65537)
            for code in (301, 302, 307, 308):
                opener.open.side_effect = HTTPError("private-gen-test", code, "private", {}, None)
                self.assertEqual(self.lookup(send=review.request_json)["state"], "attempted_unavailable")
        with self.assertRaisesRegex(review.ReviewError, "redirect"):
            review.NoRedirect().redirect_request(None, None, 302, "", {}, "https://openrouter.ai/api/v1/other")

    def test_metadata_cannot_heal_missing_completion_bill_or_invalid_output(self):
        prompts = {model: [("system", "context")] for model in review.MODELS}
        def send(url, key, body=None, **kwargs):
            return {"data": {"limit": None, "limit_remaining": None}} if body is None else completion(
                body["model"], usage={"prompt_tokens": 100, "completion_tokens": 100})
        lookup = mock.Mock(side_effect=lambda response, model, provider, key:
                           receipts.generation_metadata(response, model, provider, key,
                               send=mock.Mock(return_value=lookup_response(model))))
        path = self.root / "receipts.json"
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "synthetic"}):
            with self.assertRaises(review.ReviewError):
                agent.review_snapshot(snapshot(), POLICY, send, lambda *args: prompts,
                                      lambda text: clean_result(), receipt_path=path, metadata_lookup=lookup)
        data = review.read_json(path)
        self.assertEqual(data["schema"], "receipt_journal_v4")
        self.assertEqual(data["calls"][0]["generation_metadata"]["state"], "verified")
        self.assertEqual(data["calls"][0]["output_state"], "invalid")
        self.assertEqual(data["calls"][1]["transport_state"], "not_started")
        self.assertEqual(lookup.call_count, 1)
        self.assertNotIn("gen-1-0", path.read_text())
        projection = metrics.summarise([("attempt1", path)])
        self.assertEqual(projection["known_billed_subtotal_usd"], "0.0001")
        self.assertTrue(projection["accounting_complete"])
        self.assertEqual(projection["fully_completed_pairs"], 0)

    def test_valid_metadata_or_lookup_failure_cannot_change_a_valid_report(self):
        prompts = {model: [("system", "context")] for model in review.MODELS}
        def send(url, key, body=None, **kwargs):
            return {"data": {"limit": None, "limit_remaining": None}} if body is None else completion(body["model"])
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "synthetic"}):
            original = agent.review_snapshot(snapshot(), POLICY, send, lambda *args: prompts,
                                             lambda text: clean_result())
            for lookup_state in ("verified", "attempted_unavailable", "attempted_rejected"):
                def lookup(response, model, provider, key):
                    return ({**receipts.empty_lookup("verified"), "total_cost_usd": "0.0001"}
                            if lookup_state == "verified" else receipts.empty_lookup(lookup_state))
                path = self.root / "complete.json"
                result = agent.review_snapshot(snapshot(), POLICY, send, lambda *args: prompts,
                                               lambda text: clean_result(), receipt_path=path, metadata_lookup=lookup)
                self.assertEqual(result, original)
                self.assertTrue(metrics.read_receipt(path)["finished"])

    def test_original_receipt_write_failure_prevents_lookup(self):
        prompts = {model: [("system", "context")] for model in review.MODELS}
        original = receipts.ReceiptJournal.save
        def save(journal):
            if journal.data["calls"][0]["transport_state"] == "response_received":
                raise OSError("private")
            original(journal)
        def send(url, key, body=None, **kwargs):
            return {"data": {"limit": None, "limit_remaining": None}} if body is None else completion(body["model"])
        lookup = mock.Mock()
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "synthetic"}), \
                mock.patch.object(receipts.ReceiptJournal, "save", save), self.assertRaises(OSError):
            agent.review_snapshot(snapshot(), POLICY, send, lambda *args: prompts,
                                  receipt_path=self.root / "receipts.json", metadata_lookup=lookup)
        lookup.assert_not_called()

    def test_no_id_and_inconsistent_lookup_states_fail_closed_in_v4(self):
        journal = receipts.ReceiptJournal(self.root / "no-id.json", {"context_hash": "a" * 64},
                                         [[{"path": "file.py"}]], POLICY, 1, lookup_enabled=True)
        journal.update(0, transport_state="response_received",
                       generation_metadata=receipts.empty_lookup("not_eligible"))
        self.assertEqual(metrics.read_receipt(journal.path)["calls"][0]["generation_metadata"]["state"],
                         "not_eligible")
        for change in ({"generation_id_valid": True}, {"generation_id_sha256": "b" * 64},
                       {"transport_state": "request_intended"}):
            call = copy.deepcopy(journal.data["calls"][0])
            call.update(change)
            with self.assertRaises(review.ReviewError):
                receipts.validate_lookup(call)
        call = copy.deepcopy(journal.data["calls"][0])
        call["generation_metadata"] = receipts.empty_lookup("lookup_intended")
        with self.assertRaises(review.ReviewError):
            receipts.validate_lookup(call)

    def test_lookup_rewrite_failure_retains_original_atomic_receipt_and_stops_later_inference(self):
        prompts = {model: [("system", "context")] for model in review.MODELS}
        path = self.root / "atomic.json"
        original = receipts.ReceiptJournal.save
        paid = []
        def save(journal):
            if journal.data["calls"][0]["generation_metadata"]["state"] == "verified":
                with mock.patch.object(receipts.os, "replace", side_effect=OSError("private")):
                    original(journal)
            else:
                original(journal)
        def send(url, key, body=None, **kwargs):
            if body is None:
                return {"data": {"limit": None, "limit_remaining": None}}
            paid.append(body["model"])
            return completion(body["model"])
        def lookup(response, model, provider, key):
            return receipts.generation_metadata(response, model, provider, key,
                                                send=mock.Mock(return_value=lookup_response(model)))
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "synthetic"}), \
                mock.patch.object(receipts.ReceiptJournal, "save", save), self.assertRaises(OSError):
            agent.review_snapshot(snapshot(), POLICY, send, lambda *args: prompts,
                                  receipt_path=path, metadata_lookup=lookup)
        data = metrics.read_receipt(path)
        self.assertEqual(data["calls"][0]["generation_metadata"]["state"], "lookup_intended")
        self.assertEqual(len(paid), 1)
        self.assertEqual(data["calls"][0]["usage"]["cost"], Decimal("0.0001"))
        self.assertFalse(data["finished"])
        self.assertEqual(list(self.root.glob(".receipt-*")), [])

    def test_v4_cost_matrix_and_rejected_namespace_do_not_double_count(self):
        for response_cost, observed, source, amount in ((None, None, "unknown", "0"),
                (0.1, None, "response", "0.1"), (None, "0.1", "lookup", "0.1"),
                (0.1, "0.100", "corroborated", "0.1"), (0.1, "0.100000000000000000001", "conflict", "0"),
                (0, "0", "corroborated", "0")):
            data = receipts.ReceiptJournal(None, {"context_hash": "a" * 64}, [[{"path": "file.py"}]],
                                          POLICY, 1, lookup_enabled=True).data
            call = data["calls"][0]
            call.update(receipts.metadata(completion(review.MODELS[0], usage={"cost": response_cost}),
                        review.MODELS[0], "Parasail"), transport_state="response_received")
            call["generation_metadata"] = {**receipts.empty_lookup("verified"), "total_cost_usd": observed} \
                if observed is not None else receipts.empty_lookup("attempted_unavailable")
            path = self.root / "costs.json"
            review.write_json(path, data)
            result = metrics.summarise([("attempt1", path)])
            self.assertEqual(result["known_billed_subtotal_usd"], amount)
            self.assertEqual(result["models"][review.MODELS[0]]["billing_sources"][source], 1)
            self.assertEqual(result["accounting_complete"], source not in ("unknown", "conflict"))
        for change in ({"state": []}, {"total_cost_usd": "private"}, {"total_cost_usd": "-1"},
                       {"total_cost_usd": "1e99999"}, {"provider_latency_ms": True},
                       {"provider_latency_ms": "1200"}, {"unexpected": "private"}):
            bad = copy.deepcopy(data)
            bad["calls"][0]["generation_metadata"].update(change)
            review.write_json(path, bad)
            with self.assertRaisesRegex(review.ReviewError, "invalid_measurement_input"):
                metrics.read_receipt(path)


if __name__ == "__main__":
    unittest.main()
