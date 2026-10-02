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
from unittest import mock

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
                mock.patch.dict(os.environ, {"GH_TOKEN": "synthetic-token-must-not-be-used"}):
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
        report = model_report(snapshot())
        report["reviews"][1]["generations"][0]["id"] = report["reviews"][0]["generations"][0]["id"]
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
        self.assertEqual(receipts.generation_id("generation-123"), "generation-123")
        data = receipts.metadata(completion(model, model="hostile-model", provider="hostile-provider",
                                            error="private-body", id="generation-123"), model, provider)
        self.assertFalse(data["queryable"])
        self.assertFalse(data["model_matches"])
        self.assertFalse(data["provider_matches"])
        self.assertNotIn("hostile", json.dumps(data))
        self.assertNotIn("private-body", json.dumps(data))

    def test_partial_failure_retains_each_known_bill_without_complete_report(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "synthetic"}):
            path = Path(directory) / "receipts.json"
            prompts = {model: [("system", "identical private context")] for model in review.MODELS}
            paid = []

            def send(url, key, body=None):
                if url.endswith("/key"):
                    return {"data": {"limit": None, "limit_remaining": None}}
                paid.append(body["model"])
                response = completion(body["model"])
                if len(paid) == 2:
                    response["choices"][0]["message"]["content"] = "private malformed response"
                return response

            with self.assertRaisesRegex(review.ReviewError, "invalid_model_output"):
                agent.review_snapshot(snapshot(), POLICY, send, lambda *args: prompts,
                                      lambda text: json.loads(text)["review"], receipt_path=path)
            data = review.read_json(path)
            self.assertFalse(data["finished"])
            self.assertEqual([call["output_state"] for call in data["calls"]], ["valid", "invalid"])
            self.assertEqual([call["usage"]["cost"] for call in data["calls"]], [0.0001, 0.0001])
            self.assertEqual(len(paid), 2)
            self.assertNotIn("private", path.read_text())
            self.assertEqual(list(Path(directory).iterdir()), [path])
            report = agent.review_snapshot(snapshot(), POLICY,
                                           lambda url, key, body=None: {"data": {"limit": None, "limit_remaining": None}}
                                           if body is None else completion(body["model"]),
                                           lambda *args: prompts, lambda text: clean_result(), receipt_path=path)
            self.assertTrue(report["complete"])
            self.assertTrue(review.read_json(path)["finished"])

    def test_unknown_transport_duplicate_ids_and_disk_failure_stop_later_calls(self):
        prompts = {model: [("system", "context")] for model in review.MODELS}
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "synthetic"}):
            path = Path(directory) / "receipts.json"
            send = mock.Mock(side_effect=[{"data": {"limit": None, "limit_remaining": None}}, OSError("private")])
            with self.assertRaises(OSError):
                agent.review_snapshot(snapshot(), POLICY, send, lambda *args: prompts, receipt_path=path)
            data = review.read_json(path)
            self.assertEqual(data["calls"][0]["transport_state"], "request_intended")
            self.assertIsNone(data["calls"][0]["generation_id"])
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
