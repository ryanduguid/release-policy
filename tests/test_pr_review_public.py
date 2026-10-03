from __future__ import annotations

import copy
import importlib
import io
import json
import os
import runpy
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_pr_review import (
    BASE,
    HEAD,
    POLICY,
    POLICY_SHA,
    PR,
    REPO,
    ROOT,
    SchemaRejection,
    agent,
    clean_result,
    completion,
    fake_fetch,
    finding,
    metadata,
    model_report,
    review,
    snapshot,
)

public = importlib.import_module("pr_review_public")
receipts = importlib.import_module("pr_review_receipts")
URL = f"https://github.com/{REPO}/pull/{PR}"
CALLER = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch",
          "GITHUB_REPOSITORY": public.POLICY_REPOSITORY,
          "GITHUB_REPOSITORY_ID": str(public.POLICY_REPOSITORY_ID),
          "GITHUB_REPOSITORY_OWNER_ID": str(public.OWNER_ID), "GITHUB_ACTOR_ID": str(public.OWNER_ID),
          "GITHUB_REF": "refs/heads/main", "GITHUB_RUN_ATTEMPT": "1",
          "GITHUB_WORKFLOW_REF": public.WORKFLOW_REF, "GITHUB_WORKFLOW_SHA": POLICY_SHA,
          "GITHUB_RUN_ID": "123"}


def public_fetch(repository=None, pr=None):
    repository = {"id": 123, "full_name": REPO, "private": False} if repository is None else repository
    pr = metadata(head={"sha": HEAD, "repo": {"id": 124, "private": False}},
                  base={"sha": BASE, "repo": {**repository}},
                  user={"id": public.OWNER_ID, "type": "User"}) if pr is None else pr
    base_fetch = fake_fetch(pr)
    return lambda path: repository if path == f"repos/{REPO}" else base_fetch(path)


def central_snapshot():
    with mock.patch.dict(os.environ, CALLER), mock.patch.object(public, "scan_context"):
        return public.capture(URL, POLICY, POLICY_SHA, "scanner", public_fetch())


class PublicReviewTests(unittest.TestCase):
    def test_url_accepts_only_bounded_ascii_github_prs(self):
        self.assertEqual(public.parse_url(URL), (REPO, PR))
        self.assertEqual(public.parse_url("https://github.com/a/r.e-p_o/pull/2147483647"),
                         ("a/r.e-p_o", 2147483647))
        for url in (None, "x" * 513, URL + "/", URL + "?x=1", URL + "#x", URL.replace("github.com", "github.com:443"),
                    URL.replace("github.com", "x@github.com"), URL.replace("github.com", "api.github.com"),
                    "https://github.com/a/../pull/1", "https://github.com/a/./pull/1",
                    "https://github.com/a/r/pull/0", "https://github.com/a/r/pull/01",
                    "https://github.com/a/r/pull/2147483648", "https://github.com/a/r/pull/+1",
                    "https://github.com/-a/r/pull/1", "https://github.com/a-/r/pull/1",
                    "https://github.com/a/r%2f/pull/1", "https://github.com/a/r\\z/pull/1",
                    "https://github.com/ä/r/pull/1", URL + "\n"):
            with self.subTest(url=url), self.assertRaisesRegex(review.ReviewError, "invalid_public_pr_url"):
                public.parse_url(url)

    def test_dispatch_identity_is_checked_before_source_or_spend(self):
        with mock.patch.dict(os.environ, CALLER):
            self.assertEqual(public.caller_context()["workflow_sha"], POLICY_SHA)
            for field in CALLER:
                with mock.patch.dict(os.environ, {field: "invalid"}), self.subTest(field=field), \
                        self.assertRaises(review.ReviewError):
                    public.caller_context()
            fetch = mock.Mock()
            with mock.patch.dict(os.environ, {"GITHUB_REF": "refs/heads/feature"}), \
                    self.assertRaisesRegex(review.ReviewError, "untrusted_central_dispatch"):
                public.capture(URL, POLICY, POLICY_SHA, "scanner", fetch)
            fetch.assert_not_called()
        with mock.patch.object(public, "request_json", return_value={}) as send, \
                mock.patch.dict(os.environ, {"GH_TOKEN": "synthetic-token-must-not-be-used"}):  # nosec B105: fabricated sentinel
            public.anonymous("repos/example/repository")
            send.assert_called_once_with("https://api.github.com/repos/example/repository", "")

    def test_private_wrong_author_and_missing_repositories_stop_before_blobs(self):
        valid = public_fetch()(f"repos/{REPO}/pulls/{PR}")
        cases = []
        for field, value in (("private", True), ("id", public.POLICY_REPOSITORY_ID), ("id", True),
                             ("id", 0), ("full_name", "other/repository")):
            repo = {"id": 123, "full_name": REPO, "private": False, field: value}
            cases.append(public_fetch(repo))
        for field, value in (("state", "closed"), ("draft", True), ("number", PR + 1),
                             ("changed_files", 3001), ("changed_files", 0), ("changed_files", True),
                             ("user", {"id": 1, "type": "User"}),
                             ("user", {"id": True, "type": "User"}),
                             ("user", {"id": public.OWNER_ID, "type": "Bot"})):
            cases.append(public_fetch(pr={**valid, field: value}))
        for side in ("head", "base"):
            for repository in (None, {"private": True, "id": 124}, {"private": False, "id": True},
                               {"private": False, "id": 0}, {"private": False, "id": 456, "full_name": REPO}):
                # A different head repository is legitimate; a different base is not.
                if side == "head" and repository and repository["id"] == 456:
                    continue
                candidate = copy.deepcopy(valid)
                candidate[side]["repo"] = repository
                cases.append(public_fetch(pr=candidate))
        with mock.patch.dict(os.environ, CALLER), mock.patch.object(public, "scan_context") as scan:
            for implementation in cases:
                fetch = mock.Mock(side_effect=implementation)
                with self.assertRaises((review.ReviewError, TypeError, KeyError)):
                    public.capture(URL, POLICY, POLICY_SHA, "scanner", fetch)
                self.assertFalse(any("/contents/" in call.args[0] for call in fetch.call_args_list))
            scan.assert_not_called()

    def test_capture_freezes_identity_and_checks_it_before_inference_and_summary(self):
        with mock.patch.dict(os.environ, CALLER), mock.patch.object(public, "scan_context") as scan:
            snap = public.capture(URL, POLICY, POLICY_SHA, "scanner", public_fetch())
            scan.assert_called_once()
            public.verify_live(snap, POLICY, POLICY_SHA, public_fetch())
            changed = public_fetch()(f"repos/{REPO}/pulls/{PR}")
            changed["head"]["repo"]["id"] += 1
            with self.assertRaisesRegex(review.ReviewError, "identity_changed"):
                public.verify_live(snap, POLICY, POLICY_SHA, public_fetch(pr=changed))
            with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "124"}), \
                    self.assertRaisesRegex(review.ReviewError, "caller_changed"):
                public.verify_live(snap, POLICY, POLICY_SHA, public_fetch())
            original = public_fetch()
            reads = 0

            def moving(path):
                nonlocal reads
                if path == f"repos/{REPO}":
                    reads += 1
                    return {"id": 123 if reads == 1 else 456, "full_name": REPO, "private": False}
                return original(path)

            with self.assertRaisesRegex(review.ReviewError, "target_repository_mismatch"):
                public.capture(URL, POLICY, POLICY_SHA, "scanner", moving)
            with mock.patch.object(public, "public_identity", side_effect=[snap["target_identity"],
                                                                          {**snap["target_identity"], "author_id": 1}]), \
                    self.assertRaisesRegex(review.ReviewError, "identity_changed"):
                public.capture(URL, POLICY, POLICY_SHA, "scanner", public_fetch())
            with mock.patch.object(public, "verify_live", side_effect=review.ReviewError("stale")) as live, \
                    mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "synthetic"}):
                send = mock.Mock(return_value={"data": {"limit": None, "limit_remaining": None}})
                with self.assertRaisesRegex(review.ReviewError, "stale"):
                    agent.review_snapshot(snap, POLICY, send,
                                          lambda *args: {model: [("system", "context")] for model in review.MODELS})
                live.assert_called_once()
                self.assertEqual(send.call_count, 1)
                self.assertTrue(send.call_args.args[0].endswith("/key"))

    def test_central_and_historical_evidence_cannot_publish_status(self):
        snap = central_snapshot()
        post = mock.Mock()
        for kind, capability, caller in ((public.MODE, "report_only", public.POLICY_REPOSITORY),
                                         ("unknown", "status", REPO), (None, "status", REPO),
                                         ("installed_pr_review_v1", "none", REPO),
                                         ("installed_pr_review_v1", "status", "wrong/repo")):
            candidate = {**snap, "snapshot_kind": kind, "publication_capability": capability,
                         "caller_repository": caller}
            with self.assertRaisesRegex(review.ReviewError, "status_capability_required"):
                review.publish(candidate, model_report(snap), POLICY, POLICY_SHA, public_fetch(), post, True)
            with self.assertRaisesRegex(review.ReviewError, "invalid_failure_identity"):
                review.failed_status(candidate, REPO, post)
            with self.assertRaisesRegex(review.ReviewError, "central_report_capability_required"):
                public.verify_live({**candidate, "snapshot_kind": "unknown"}, POLICY, POLICY_SHA, public_fetch())
        post.assert_not_called()
        for field, value in (("repository_id", None), ("repository_id", -1),
                             ("caller_repository_id", True), ("caller_repository_id", 456)):
            candidate = {**snapshot(), field: value}
            with self.subTest(field=field, value=value), self.assertRaisesRegex(review.ReviewError, "status_capability_required"):
                review.publish(candidate, model_report(candidate), POLICY, POLICY_SHA, fake_fetch(), post, True)
            with self.assertRaisesRegex(review.ReviewError, "invalid_failure_identity"):
                review.failed_status(candidate, REPO, post)
        report = model_report(snapshot())
        report["reviews"][1]["generations"][0]["generation_id_sha256"] = report["reviews"][0]["generations"][0]["generation_id_sha256"]
        with self.assertRaisesRegex(review.ReviewError, "duplicate_generation_id"):
            review.publish(snapshot(), report, POLICY, POLICY_SHA, fake_fetch(), post, True)
        post.assert_not_called()

    def test_summary_distinguishes_incomplete_from_findings_and_escapes_output(self):
        snap = central_snapshot()
        with mock.patch.dict(os.environ, CALLER):
            self.assertIn("Review incomplete. No verdict", public.summary(snap, None, POLICY, POLICY_SHA, public_fetch()))
            report = model_report(snap)
            self.assertIn("no findings", public.summary(snap, report, POLICY, POLICY_SHA, public_fetch()))
            report["reviews"][0]["results"][0]["key_issues_to_review"] = [{**finding(), "issue_content": "<script>"}]
            text = public.summary(snap, report, POLICY, POLICY_SHA, public_fetch())
            self.assertIn("Inspect findings", text)
            self.assertIn("&lt;script&gt;", text)
            self.assertNotIn("<script>", text)

    def test_cli_gate_capture_summary_and_sanitised_failures(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, CALLER), \
                redirect_stderr(io.StringIO()) as errors:
            root = Path(directory)
            policy, snap, report, summary = (root / name for name in ("policy.json", "snapshot.json", "review.json", "summary.md"))
            review.write_json(policy, POLICY)
            args = ["--policy", str(policy), "--policy-sha", POLICY_SHA, "--snapshot", str(snap),
                    "--report", str(report)]
            self.assertEqual(public.main(["gate", *args, "--url", URL]), 0)
            self.assertEqual(public.main(["gate", *args, "--mode", "unknown", "--url", URL]), 1)
            self.assertEqual(public.main(["gate", *args, "--policy-sha", "invalid", "--url", URL]), 1)
            with mock.patch.object(public, "request_json", side_effect=lambda url, key: public_fetch()(url.removeprefix("https://api.github.com/"))), \
                    mock.patch.object(public, "scan_context"), \
                    mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}):
                self.assertEqual(public.main(["capture", *args, "--url", URL]), 0)
                self.assertEqual(public.main(["summary", *args]), 0)
                self.assertIn("incomplete", summary.read_text())
                review.write_json(report, model_report(review.read_json(snap)))
                self.assertEqual(public.main(["summary", *args]), 0)
            with mock.patch.object(public, "read_json", side_effect=ValueError("private-raw-body")):
                self.assertEqual(public.main(["gate", *args, "--url", URL]), 1)
            self.assertNotIn("private-raw-body", errors.getvalue())
            with mock.patch.object(sys, "argv", ["pr_review_public.py", "gate", *args]), \
                    self.assertRaises(SystemExit) as caught:
                runpy.run_path(str(ROOT / "scripts/pr_review_public.py"), run_name="__main__")
            self.assertEqual(caught.exception.code, 1)


class ReceiptTests(unittest.TestCase):
    def test_public_serialisation_rejects_raw_ids_and_invalid_digest_or_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            report = model_report()
            review.write_report(path, report)
            prior = path.read_bytes()
            cases = [{**report, "schema": 1}, {**report, "generation_id": "gen-private-canary"}]
            candidate = copy.deepcopy(report)
            candidate["reviews"][0]["generation_id"] = "gen-private-canary"
            cases.append(candidate)
            for field, value in (("id", "gen-private-canary"), ("generation_id_sha256", None),
                                 ("generation_id_sha256", "gen-private-canary")):
                candidate = copy.deepcopy(report)
                candidate["reviews"][0]["generations"][0][field] = value
                cases.append(candidate)
            for changes in ({"raw_id": "gen-private-canary"}, {"prompt_tokens": True},
                            {"completion_tokens": 0}, {"cost": "0"}, {"cost": float("inf")}, {"cost": -1}):
                candidate = copy.deepcopy(report)
                candidate["reviews"][0]["generations"][0]["usage"].update(changes)
                cases.append(candidate)
            for candidate in cases:
                with self.assertRaises(review.ReviewError):
                    review.write_report(path, candidate)
                self.assertEqual(path.read_bytes(), prior)
            journal_path = Path(directory) / "receipts.json"
            snap = snapshot()
            journal = receipts.ReceiptJournal(journal_path, snap, review.chunks(snap, POLICY), POLICY, 1)
            with self.assertRaisesRegex(review.ReviewError, "invalid_receipt_fields"):
                journal.update(0, generation_id="gen-private-canary")
            self.assertNotIn("gen-private-canary", journal_path.read_text())

    def test_digest_is_exact_ascii_and_raw_response_id_cannot_escape_report(self):
        identifier = "gen-AsCiICanary09"
        data = receipts.metadata(completion(review.MODELS[0], id=identifier), review.MODELS[0], "Parasail")
        self.assertEqual(data["generation_id_sha256"], review.hashlib.sha256(identifier.encode("ascii")).hexdigest())
        self.assertTrue(data["generation_id_valid"])
        self.assertNotIn(identifier, json.dumps(data))
        self.assertIsNone(receipts.metadata(completion(review.MODELS[0], id=identifier + " "),
                                           review.MODELS[0], "Parasail")["generation_id_sha256"])
        prompts = {model: [("system", "context")] for model in review.MODELS}

        def send(url, key, body=None, **kwargs):
            if body is None:
                return {"data": {"limit": None, "limit_remaining": None}}
            result = clean_result()
            result["key_issues_to_review"] = [{**finding(), "issue_content": identifier}]
            return completion(body["model"], id=identifier if body["model"] == review.MODELS[0] else "gen-other",
                              choices=[{"finish_reason": "stop", "message": {"content": json.dumps({"review": result})}}])

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "synthetic"}):
            path = Path(directory) / "receipts.json"
            with self.assertRaisesRegex(review.ReviewError, "raw_generation_id_in_report"):
                agent.review_snapshot(snapshot(), POLICY, send, lambda *args: prompts,
                                      lambda text: json.loads(text)["review"], receipt_path=path)
            self.assertNotIn(identifier, path.read_text())
            self.assertFalse(review.read_json(path)["finished"])

    def test_malformed_envelope_and_http_errors_retain_observed_unknown_receipt(self):
        prompts = {model: [("system", "context")] for model in review.MODELS}
        from urllib.error import HTTPError

        for envelope in (b"malformed private body", b'{"cost":0,"cost":1}',
                         b'{"cost":NaN}', b"x" * (review.MAX_RESPONSE_BYTES + 1),
                         HTTPError("url", 500, "private error", {}, None)):
            key_response = mock.MagicMock()
            key_response.__enter__.return_value.read.return_value = b'{"data":{"limit":null,"limit_remaining":null}}'
            paid_response = mock.MagicMock()
            paid_response.__enter__.return_value.read.return_value = envelope
            opener = mock.Mock()
            opener.open.side_effect = [key_response, envelope if isinstance(envelope, HTTPError) else paid_response]
            with tempfile.TemporaryDirectory() as directory, \
                    mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "synthetic"}), \
                    mock.patch.object(review, "build_opener", return_value=opener):
                path = Path(directory) / "receipts.json"
                with self.assertRaises(review.ReviewError):
                    agent.review_snapshot(snapshot(), POLICY, prompt_builder=lambda *args: prompts, receipt_path=path)
                data = review.read_json(path)
                self.assertEqual(data["calls"][0]["transport_state"], "response_observed")
                self.assertIsNone(data["calls"][0]["generation_id_sha256"])
                self.assertIsNone(data["calls"][0]["usage"]["cost"])
                self.assertEqual(data["calls"][1]["transport_state"], "not_started")
                self.assertFalse(data["finished"])
                self.assertNotIn("private", path.read_text())
                self.assertEqual(opener.open.call_count, 2)

    def test_final_journal_write_failure_vetoes_cli_report(self):
        prompts = {model: [("system", "context")] for model in review.MODELS}
        original_save = receipts.ReceiptJournal.save
        original_review = agent.review_snapshot

        def failing_save(journal):
            if journal.data["finished"]:
                raise OSError("private disk error")
            original_save(journal)

        def send(url, key, body=None, **kwargs):
            return {"data": {"limit": None, "limit_remaining": None}} if body is None else completion(body["model"])

        def run(snap, policy, *, receipt_path=None, metadata_lookup=None):
            return original_review(snap, policy, send, lambda *args: prompts,
                                   lambda text: clean_result(), receipt_path=receipt_path)

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "synthetic", "GITHUB_RUN_ATTEMPT": "1"}), \
                mock.patch.object(receipts.ReceiptJournal, "save", failing_save), \
                mock.patch.object(agent, "review_snapshot", side_effect=run), redirect_stderr(io.StringIO()) as errors:
            root = Path(directory)
            policy, snap, report = (root / name for name in ("policy.json", "snapshot.json", "review.json"))
            review.write_json(policy, POLICY)
            review.write_json(snap, snapshot())
            self.assertEqual(review.main(["review", "--policy", str(policy), "--policy-sha", POLICY_SHA,
                                          "--snapshot", str(snap), "--report", str(report)]), 1)
            self.assertFalse(report.exists())
            data = review.read_json(report.with_suffix(".receipts.json"))
            self.assertFalse(data["finished"])
            self.assertEqual([call["output_state"] for call in data["calls"]], ["valid", "valid"])
            self.assertNotIn("private disk", errors.getvalue())

    def test_metadata_whitelists_bounded_numbers_ids_and_enum_values(self):
        model = review.MODELS[0]
        provider = POLICY["routes"][model]["name"]
        self.assertEqual(receipts.metadata(completion(model), model, provider)["usage"]["cost"], 0.0001)
        for value in (None, [], {"choices": []}, {"choices": [None]}, {"choices": [{"finish_reason": []}]}):
            self.assertIsNone(receipts.metadata(value, model, provider)["finish_reason"])
        for cost in (True, "0.1", -1, float("nan"), float("inf"), 10**24):
            response = completion(model, usage={"cost": cost, "prompt_tokens": True, "total_tokens": 1_000_000_001})
            self.assertIsNone(receipts.metadata(response, model, provider)["usage"]["cost"])
        for cost in (0, 99, 1e100, 0.0):
            self.assertEqual(receipts.metadata(completion(model, usage={"cost": cost}), model, provider)["usage"]["cost"], cost)
        self.assertIsNone(receipts.metadata(completion(model, usage=[]), model, provider)["usage"]["cost"])
        for identifier in (None, "raw body", "gen-" + "a" * 129, "gen-sk-secret", "gen-ü", "gen-line\nbreak"):
            self.assertIsNone(receipts.generation_id(identifier))
        self.assertIsNone(receipts.generation_id("generation-123"))
        data = receipts.metadata(completion(model, model="hostile-model", provider="hostile-provider",
                                            error="private-body", id="generation-123"), model, provider)
        self.assertFalse(data["generation_id_valid"])
        self.assertIsNone(data["generation_id_sha256"])
        self.assertFalse(data["model_matches"])
        self.assertFalse(data["provider_matches"])
        self.assertNotIn("hostile", json.dumps(data))
        self.assertNotIn("private-body", json.dumps(data))

    def test_reasoning_usage_is_bounded_numeric_metadata_from_one_path(self):
        model = review.MODELS[0]
        provider = POLICY["routes"][model]["name"]
        for value in (0, 37, 100):
            response = completion(model, usage={"completion_tokens": 100,
                                               "completion_tokens_details": {"reasoning_tokens": value,
                                                                             "text": "private-canary"}})
            data = receipts.metadata(response, model, provider, output_tokens=100)
            self.assertEqual(data["usage"]["reasoning_tokens"], value)
            self.assertNotIn("private-canary", json.dumps(data))
        for details in (None, [], "private-canary", {}, {"reasoning_tokens": None},
                        *({"reasoning_tokens": value} for value in
                          (True, False, -1, 0.5, "37", 101, 10**100, float("nan"), float("inf")))):
            response = completion(model, usage={"completion_tokens": 100,
                                               "completion_tokens_details": details,
                                               "reasoning_tokens": 37})
            with self.subTest(details=details):
                self.assertIsNone(receipts.metadata(response, model, provider,
                                                   output_tokens=100)["usage"]["reasoning_tokens"])
        response = completion(model, usage={"completion_tokens": 100,
                                           "completion_tokens_details": {"reasoning_tokens": 38}})
        self.assertIsNone(receipts.metadata(response, model, provider,
                                           output_tokens=37)["usage"]["reasoning_tokens"])
        self.assertIsNone(receipts.metadata(response, model, provider)["usage"]["reasoning_tokens"])
        response["usage"]["completion_tokens"] = None
        self.assertIsNone(receipts.metadata(response, model, provider,
                                           output_tokens=100)["usage"]["reasoning_tokens"])

    def test_reasoning_metadata_never_changes_reservation_acceptance_or_report(self):
        prompts = {model: [("system", "context")] for model in review.MODELS}
        reports = []
        for value in (None, 0, 100, "private-canary"):
            with tempfile.TemporaryDirectory() as directory, \
                    mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "synthetic"}):
                path = Path(directory) / "receipts.json"

                def send(url, key, body=None, **kwargs):
                    if body is None:
                        return {"data": {"limit": None, "limit_remaining": None}}
                    response = completion(body["model"])
                    response["usage"]["completion_tokens_details"] = {"reasoning_tokens": value}
                    return response

                reports.append(agent.review_snapshot(snapshot(), POLICY, send, lambda *args: prompts,
                                                      lambda text: clean_result(), receipt_path=path))
                data = review.read_json(path)
                self.assertEqual(data["schema"], "receipt_journal_v3")
                self.assertTrue(data["finished"])
                self.assertNotIn("private-canary", path.read_text())
                self.assertEqual([call["usage"]["reasoning_tokens"] for call in data["calls"]],
                                 [value, value] if isinstance(value, int) else [None, None])
        self.assertTrue(all(report == reports[0] for report in reports))

    def test_length_failure_retains_reasoning_count_and_stops_later_calls(self):
        model = review.MODELS[0]
        response = completion(model, choices=[{"finish_reason": "length",
                                              "message": {"content": "private-canary"}}],
                              usage={"prompt_tokens": 100, "completion_tokens": POLICY["output_tokens"],
                                     "completion_tokens_details": {"reasoning_tokens": POLICY["output_tokens"]},
                                     "cost": 0.01})
        send = mock.Mock(side_effect=[{"data": {"limit": None, "limit_remaining": None}}, response])
        prompts = {selected: [("system", "context")] for selected in review.MODELS}
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "synthetic"}):
            path = Path(directory) / "receipts.json"
            with self.assertRaisesRegex(review.ReviewError, "incomplete_completion"):
                agent.review_snapshot(snapshot(), POLICY, send, lambda *args: prompts,
                                      lambda text: clean_result(), receipt_path=path)
            data = review.read_json(path)
            self.assertFalse(data["finished"])
            self.assertEqual(data["calls"][0]["usage"]["reasoning_tokens"], POLICY["output_tokens"])
            self.assertEqual(data["calls"][0]["usage"]["cost"], 0.01)
            self.assertEqual(data["calls"][1]["transport_state"], "not_started")
            self.assertNotIn("private-canary", path.read_text())
            self.assertEqual(send.call_count, 2)

    def test_partial_failure_retains_each_known_bill_without_complete_report(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "synthetic"}):
            path = Path(directory) / "receipts.json"
            prompts = {model: [("system", "identical private context")] for model in review.MODELS}
            paid = []

            def send(url, key, body=None, **kwargs):
                if url.endswith("/key"):
                    return {"data": {"limit": None, "limit_remaining": None}}
                paid.append(body["model"])
                response = completion(body["model"])
                if len(paid) == 2:
                    response["choices"][0]["message"]["content"] = "private malformed response"
                return response

            native = SimpleNamespace(PRReview=SimpleNamespace(model_validate=mock.Mock()))
            with mock.patch.dict(sys.modules, {"pr_agent.algo.output_models": native,
                                               "pydantic": SimpleNamespace(ValidationError=SchemaRejection)}), \
                    self.assertRaisesRegex(review.ReviewError, "invalid_review_json"):
                agent.review_snapshot(snapshot(), POLICY, send, lambda *args: prompts,
                                      receipt_path=path)
            data = review.read_json(path)
            self.assertEqual(data["schema"], "receipt_journal_v3")
            self.assertFalse(data["finished"])
            self.assertEqual([call["output_state"] for call in data["calls"]], ["valid", "invalid"])
            self.assertEqual([call["diagnostic_category"] for call in data["calls"]], [None, "json_syntax"])
            self.assertEqual([call["usage"]["cost"] for call in data["calls"]], [0.0001, 0.0001])
            self.assertEqual(len(paid), 2)
            self.assertNotIn("private", path.read_text())
            self.assertEqual(list(Path(directory).iterdir()), [path])
            report = agent.review_snapshot(snapshot(), POLICY,
                                           lambda url, key, body=None, **kwargs: {"data": {"limit": None, "limit_remaining": None}}
                                           if body is None else completion(body["model"]),
                                           lambda *args: prompts, lambda text: clean_result(), receipt_path=path)
            self.assertTrue(report["complete"])
            self.assertTrue(review.read_json(path)["finished"])

    def test_diagnostic_receipts_require_closed_categories_and_matching_states(self):
        snap = snapshot()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipts.json"
            journal = receipts.ReceiptJournal(path, snap, review.chunks(snap, POLICY), POLICY, 1)
            initial = copy.deepcopy(journal.data)
            allowed = {"model", "provider", "chunk_hash", "transport_state", "output_state",
                       "diagnostic_category", "generation_id_sha256", "generation_id_valid",
                       "model_matches", "provider_matches", "finish_reason", "usage"}
            self.assertEqual(set(initial["calls"][0]), allowed)
            for category in ("completion_contract", "json_syntax", "duplicate_keys", "root_or_nesting", "strict_schema"):
                journal.update(0, transport_state="response_received", output_state="invalid",
                               diagnostic_category=category)
                self.assertEqual(review.read_json(path)["calls"][0]["diagnostic_category"], category)
            journal.update(0, output_state="indeterminate", diagnostic_category="unexpected_runtime")
            prior = path.read_bytes()
            for changes in ({"diagnostic_category": None}, {"diagnostic_category": True},
                            {"diagnostic_category": 1}, {"diagnostic_category": []},
                            {"diagnostic_category": {}}, {"diagnostic_category": "private-canary"},
                            {"output_state": "invalid", "diagnostic_category": "unexpected_runtime"},
                            {"output_state": "valid", "diagnostic_category": "strict_schema"},
                            {"output_state": "not_checked", "diagnostic_category": "strict_schema"},
                            {"output_state": "unknown", "diagnostic_category": None},
                            {"transport_state": "request_intended", "output_state": "invalid",
                             "diagnostic_category": "json_syntax"}):
                journal.data = copy.deepcopy(initial)
                journal.data["calls"][0].update(transport_state="response_received", output_state="invalid",
                                               diagnostic_category="json_syntax")
                with self.assertRaisesRegex(review.ReviewError, "invalid_receipt_diagnostic"):
                    journal.update(0, **changes)
                self.assertEqual(path.read_bytes(), prior)
            journal.data = copy.deepcopy(initial)
            with self.assertRaisesRegex(review.ReviewError, "invalid_receipt_fields"):
                journal.update(0, message="private-canary")
            self.assertNotIn("private", path.read_text())

    def test_runtime_failure_is_indeterminate_and_never_exposes_exception_data(self):
        prompts = {model: [("system", "context")] for model in review.MODELS}
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "synthetic"}):
            path = Path(directory) / "receipts.json"
            send = mock.Mock(side_effect=[{"data": {"limit": None, "limit_remaining": None}}, completion(review.MODELS[0])])

            def parser(content):
                call = review.read_json(path)["calls"][0]
                self.assertEqual(call["transport_state"], "response_received")
                self.assertEqual(call["output_state"], "not_checked")
                self.assertIsNone(call["diagnostic_category"])
                raise RuntimeError("private-message private-value private-field-path private-source")

            with self.assertRaisesRegex(review.ReviewError, "^invalid_model_output$"):
                agent.review_snapshot(snapshot(), POLICY, send, lambda *args: prompts, parser, receipt_path=path)
            call = review.read_json(path)["calls"][0]
            self.assertEqual(call["output_state"], "indeterminate")
            self.assertEqual(call["diagnostic_category"], "unexpected_runtime")
            self.assertEqual(call["usage"]["cost"], 0.0001)
            self.assertEqual(send.call_count, 2)
            self.assertNotIn("private", path.read_text())

    def test_native_value_error_is_runtime_and_typed_rejection_is_invalid(self):
        prompts = {model: [("system", "context")] for model in review.MODELS}
        for rejected_model in review.MODELS:
            for error, state, category in ((ValueError("private-value"), "indeterminate", "unexpected_runtime"),
                                           (SchemaRejection("private-field-path"), "invalid", "strict_schema")):
                with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "synthetic"}):
                    path = Path(directory) / "receipts.json"
                    paid = []

                    def send(url, key, body=None, **kwargs):
                        if body is None:
                            return {"data": {"limit": None, "limit_remaining": None}}
                        paid.append(body["model"])
                        return completion(body["model"])

                    def validate(data, strict):
                        self.assertTrue(strict)
                        call = review.read_json(path)["calls"][len(paid) - 1]
                        self.assertEqual(call["transport_state"], "response_received")
                        if paid[-1] == rejected_model:
                            raise error

                    native = SimpleNamespace(PRReview=SimpleNamespace(model_validate=validate))
                    with mock.patch.dict(sys.modules, {"pr_agent.algo.output_models": native,
                                                       "pydantic": SimpleNamespace(ValidationError=SchemaRejection)}), \
                            self.assertRaises(review.ReviewError) as caught:
                        agent.review_snapshot(snapshot(), POLICY, send, lambda *args: prompts, receipt_path=path)
                    self.assertNotIn("private", str(caught.exception))
                    journal = review.read_json(path)
                    self.assertFalse(journal["finished"])
                    call = journal["calls"][len(paid) - 1]
                    self.assertEqual(call["output_state"], state)
                    self.assertEqual(call["diagnostic_category"], category)
                    self.assertEqual(call["usage"]["cost"], 0.0001)
                    self.assertEqual(paid, list(review.MODELS[:review.MODELS.index(rejected_model) + 1]))
                    self.assertNotIn("private", path.read_text())

    def test_primary_and_fallback_cannot_write_with_corrupt_capabilities(self):
        for changes in ({"repository": "other/repo"}, {"head": "invalid"},
                        {"snapshot_kind": "central_public_report_v1"},
                        {"publication_capability": "none"}, {"caller_repository": "other/repo"},
                        {"repository_id": 0}, {"caller_repository_id": True},
                        {"caller_repository_id": 999}):
            candidate = {**snapshot(), **changes}
            post = mock.Mock()
            with self.assertRaises(review.ReviewError):
                review.publish(candidate, model_report(candidate), POLICY, POLICY_SHA, fake_fetch(), post, True)
            with self.assertRaises(review.ReviewError):
                review.failed_status(candidate, REPO, post)
            post.assert_not_called()

    def test_unknown_transport_duplicate_ids_and_disk_failure_stop_later_calls(self):
        prompts = {model: [("system", "context")] for model in review.MODELS}
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "synthetic"}):
            path = Path(directory) / "receipts.json"
            send = mock.Mock(side_effect=[{"data": {"limit": None, "limit_remaining": None}}, OSError("private")])
            with self.assertRaises(OSError):
                agent.review_snapshot(snapshot(), POLICY, send, lambda *args: prompts, receipt_path=path)
            data = review.read_json(path)
            self.assertEqual(data["calls"][0]["transport_state"], "request_intended")
            self.assertIsNone(data["calls"][0]["generation_id_sha256"])
            self.assertIsNone(data["calls"][0]["usage"]["cost"])
            self.assertEqual(send.call_count, 2)
            for identifier in ("generation-123", "gen-duplicate"):
                send = mock.Mock(side_effect=[{"data": {"limit": None, "limit_remaining": None}},
                                              completion(review.MODELS[0], id=identifier),
                                              completion(review.MODELS[1], id=identifier)])
                with self.assertRaisesRegex(review.ReviewError, "unusable_or_duplicate_generation_id"):
                    agent.review_snapshot(snapshot(), POLICY, send, lambda *args: prompts,
                                          lambda text: clean_result(), receipt_path=path)
            send = mock.Mock(return_value={"data": {"limit": None, "limit_remaining": None}})
            with mock.patch.object(receipts.os, "replace", side_effect=OSError("disk failure")), self.assertRaises(OSError):
                agent.review_snapshot(snapshot(), POLICY, send, lambda *args: prompts, receipt_path=path)
            self.assertEqual(send.call_count, 1)
            self.assertFalse(any(item.name.startswith(".receipt-") for item in Path(directory).iterdir()))

    def test_journal_atomic_replacement_size_bounds_and_linux_directory_sync(self):
        snap = snapshot()
        groups = review.chunks(snap, POLICY)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipts.json"
            journal = receipts.ReceiptJournal(path, snap, groups, POLICY, 1)
            journal.update(0, transport_state="request_intended")
            windows_os = SimpleNamespace(fdopen=os.fdopen, fsync=os.fsync, replace=os.replace)
            with mock.patch.object(receipts, "os", windows_os):
                journal.save()
            self.assertEqual(review.read_json(path), journal.data)
            prior = path.read_bytes()
            with mock.patch.object(receipts.os, "replace", side_effect=OSError), self.assertRaises(OSError):
                journal.update(0, transport_state="response_received")
            self.assertEqual(path.read_bytes(), prior)
            with mock.patch.object(receipts, "MAX_JOURNAL_BYTES", 1), self.assertRaisesRegex(review.ReviewError, "plan_too_large"):
                receipts.ReceiptJournal(path, snap, groups, POLICY, 1)
            with mock.patch.object(receipts, "MAX_JOURNAL_BYTES", 1), self.assertRaisesRegex(review.ReviewError, "journal_too_large"):
                journal.save()
            descriptor, name = tempfile.mkstemp(dir=directory)
            with mock.patch.object(receipts.tempfile, "mkstemp", return_value=(descriptor, name)), \
                    mock.patch.object(receipts.os, "O_DIRECTORY", 0, create=True), \
                    mock.patch.object(receipts.os, "open", return_value=9999), \
                    mock.patch.object(receipts.os, "fsync", side_effect=[None, OSError("directory sync")]), \
                    mock.patch.object(receipts.os, "close") as close, self.assertRaises(OSError):
                journal.save()
            close.assert_called_once_with(9999)
            self.assertEqual(review.read_json(path)["calls"][0]["transport_state"], "response_received")


if __name__ == "__main__":
    unittest.main()
