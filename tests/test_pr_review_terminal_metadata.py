# Copyright 2026 Ryan Duguid
# SPDX-License-Identifier: LicenseRef-Duguid-Public-Attribution-1.0

from __future__ import annotations

import importlib
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
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
    counted,
    fake_fetch,
    metadata,
    review,
    snapshot,
)

metrics = importlib.import_module("pr_review_measurements")


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

    def test_missing_usage_leaves_cost_unknown_and_accounting_incomplete(self):
        prompts = {model: [("system", "context")] for model in review.MODELS}
        send = counted(lambda url, key, body=None, **kwargs: completion(body["model"], usage={"input_tokens": 100}))
        path = self.root / "receipts.json"
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "synthetic"}),                 self.assertRaisesRegex(review.ReviewError, "missing_usage"):
            agent.review_snapshot(snapshot(), POLICY, send, lambda *args: prompts,
                                  lambda text: clean_result(), receipt_path=path)
        data = review.read_json(path)
        self.assertEqual(data["schema"], "receipt_journal_v5")
        self.assertEqual(data["calls"][0]["output_state"], "invalid")
        self.assertEqual(data["calls"][0]["diagnostic_category"], "completion_contract")
        self.assertIsNone(data["calls"][0]["usage"]["cost"])
        self.assertEqual(data["calls"][1]["transport_state"], "not_started")
        self.assertNotIn("msg_1-0", path.read_text())
        projection = metrics.summarise([("attempt1", path)])
        self.assertEqual(projection["known_billed_subtotal_usd"], "0")
        self.assertEqual(projection["unknown_bills"], 1)
        self.assertFalse(projection["accounting_complete"])
        self.assertEqual(projection["fully_completed_pairs"], 0)


if __name__ == "__main__":
    unittest.main()
