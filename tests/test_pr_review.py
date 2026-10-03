from __future__ import annotations

import base64
import copy
import importlib
import io
import json
import os
import runpy
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
# The policy scripts also import one another when invoked by their file path.
sys.path.insert(0, str(ROOT / "scripts"))
review = importlib.import_module("pr_review")
agent = importlib.import_module("pr_review_agent")
benchmark = importlib.import_module("pr_review_benchmark")
POLICY = json.loads((ROOT / ".github/pr-review-policy.json").read_bytes())


class SchemaRejection(ValueError):
    """Stand in for the installed native validator's dedicated rejection type."""

HEAD, BASE, POLICY_SHA, MERGE_BASE = (char * 40 for char in "abcd")
REPO = "example/repository"
PR = 7


def metadata(**changes):
    pr = {"number": PR, "state": "open", "draft": False, "changed_files": 1, "author_association": "OWNER",
          "head": {"sha": HEAD}, "base": {"sha": BASE, "repo": {"full_name": REPO, "private": False, "id": 123}},
          "user": {"login": "example"}}
    pr.update(changes)
    return pr


def source(text):
    raw = text.encode()
    return {"type": "file", "encoding": "base64", "size": len(raw),
            "content": base64.b64encode(raw).decode()}


def file_change(**changes):
    f = {"filename": "code.py", "status": "modified", "additions": 1, "deletions": 1,
         "patch": "@@ -1 +1 @@\n-return False\n+return True"}
    f.update(changes)
    return f


def snapshot():
    return review.collect(fake_fetch(), REPO, PR, POLICY_SHA, copy.deepcopy(POLICY))


def fake_fetch(pr=None, files=None, old="return False\n", new="return True\n"):
    pr = metadata() if pr is None else pr
    files = [file_change()] if files is None else files

    def fetch(path):
        if "/files?" in path:
            return files if path.endswith("page=1") else []
        if "/contents/" in path:
            return source(old if path.endswith(MERGE_BASE) else new)
        if "/compare/" in path:
            return {"merge_base_commit": {"sha": MERGE_BASE}}
        return pr
    return fetch


def clean_result():
    return {"key_issues_to_review": [], "security_concerns": "No",
            "merge_recommendation": "safe_to_merge", "risk_level": "low"}


def finding():
    return {"relevant_file": "code.py", "issue_header": "[P1] Wrong result",
            "issue_content": "The change returns True for a case requiring False.",
            "start_line": 1, "end_line": 1}


def completion(selected_model, **changes):
    obj = {"id": f"gen-1-{review.MODELS.index(selected_model)}", "model": selected_model, "provider": POLICY["routes"][selected_model]["name"],
           "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"review": clean_result()})}}],
           "usage": {"prompt_tokens": 100, "completion_tokens": 100, "cost": 0.0001}}
    obj.update(changes)
    return obj


def model_report(snap=None):
    snap = snapshot() if snap is None else snap
    groups = review.chunks(snap, POLICY)
    return {"schema": 2, "context_hash": snap["context_hash"], "complete": True, "engine_sha": review.PR_AGENT_SHA,
            "remaining_files": [], "failed_chunks": 0, "reviews": [
                {"model": model, "provider": POLICY["routes"][model]["name"],
                 "chunk_hashes": [review.digest(group) for group in groups],
                 "results": [clean_result() for _ in groups],
                 "generations": [{"finish_reason": "stop",
                                  "generation_id_sha256": review.hashlib.sha256(f"gen-{review.MODELS.index(model)}-{i}".encode()).hexdigest(),
                                  "usage": {"prompt_tokens": 100, "completion_tokens": 100, "cost": 0.0001}}
                                 for i, _ in enumerate(groups)]} for model in review.MODELS]}


class ReviewBoundaryTests(unittest.TestCase):
    def test_policy_allows_exactly_two_distinct_models(self):
        review.validate_policy(POLICY)
        for field, value in (("models", ["claude"]), ("mode", "enforce"), ("schema", 2),
                             ("spending_mode", "unknown"), ("spending_mode", None),
                             ("private_repositories", True), ("max_review_usd", float("nan")),
                             ("max_review_usd", 4), ("max_pilot_limit_usd", 101),
                             ("max_chunks", 1.5), ("instructions", "x" * 8001)):
            policy = copy.deepcopy(POLICY)
            policy[field] = value
            with self.subTest(field=field), self.assertRaises(review.ReviewError):
                review.validate_policy(policy)
        for field, value in (("slug", "unknown"), ("name", "other"), ("prompt", 0), ("completion", "5")):
            policy = copy.deepcopy(POLICY)
            policy["routes"][review.MODELS[0]][field] = value
            with self.subTest(field=field), self.assertRaises(review.ReviewError):
                review.validate_policy(policy)

    def test_file_paths_are_checked_before_content_fetch(self):
        for path in ("/absolute", "../outside", "line\nbreak", "a\\b", "credentials", "secrets/key.txt",
                     ".env.production", "private_key.txt", "cert.pem", "a.token"):
            with self.subTest(path=path), self.assertRaises(review.ReviewError):
                review.safe_path(path)
        review.safe_path("src/normal.py")
        fetch = mock.Mock(wraps=fake_fetch(files=[file_change(filename=".env")]))
        with self.assertRaisesRegex(review.ReviewError, "secret_path"):
            review.collect(fetch, REPO, PR, POLICY_SHA, POLICY)
        self.assertFalse(any("/contents/" in call.args[0] for call in fetch.call_args_list))

    def test_capture_uses_merge_base_and_rechecks_revision(self):
        fetch = mock.Mock(wraps=fake_fetch())
        snap = review.collect(fetch, REPO, PR, POLICY_SHA, POLICY)
        self.assertEqual(snap["merge_base"], MERGE_BASE)
        self.assertEqual(snap["files"][0]["before"], "return False\n")
        self.assertIn(mock.call(f"repos/{REPO}/contents/code.py?ref={MERGE_BASE}"), fetch.call_args_list)
        review.verify_snapshot(snap, POLICY, POLICY_SHA)
        calls = 0

        def moving(path):
            nonlocal calls
            if path == f"repos/{REPO}/pulls/{PR}":
                calls += 1
                return metadata(head={"sha": "e" * 40}) if calls > 1 else metadata()
            return fake_fetch()(path)
        with self.assertRaisesRegex(review.ReviewError, "revision_changed"):
            review.collect(moving, REPO, PR, POLICY_SHA, POLICY)
        reads = 0

        def changing_count(path):
            nonlocal reads
            if path == f"repos/{REPO}/pulls/{PR}":
                reads += 1
                return metadata(changed_files=1 if reads == 1 else 2)
            return fake_fetch()(path)
        with self.assertRaisesRegex(review.ReviewError, "revision_changed"):
            review.collect(changing_count, REPO, PR, POLICY_SHA, POLICY)

    def test_incomplete_listing_pagination_and_changed_line_counts(self):
        for pr, files, expected in ((metadata(changed_files=3001), [], "file_count"),
                                    (metadata(changed_files=2), [file_change()], "files_listing"),
                                    (metadata(changed_files=2), [file_change(), file_change()], "files_listing"),
                                    (metadata(), [file_change(patch="")], "truncated_patch"),
                                    (metadata(), [file_change(status="copied")], "file_change"),
                                    (metadata(), [file_change(patch=None)], "invalid_patch"),
                                    (metadata(), [file_change(patch="", additions=0, deletions=0)], "non_text")):
            with self.subTest(expected=expected), self.assertRaisesRegex(review.ReviewError, expected):
                review.collect(fake_fetch(pr, files), REPO, PR, POLICY_SHA, POLICY)
        files = [file_change(filename=f"f{i}.py") for i in range(100)]
        snap = review.collect(fake_fetch(metadata(changed_files=100), files), REPO, PR, POLICY_SHA, POLICY)
        self.assertEqual(len(snap["files"]), 100)
        all_files = [file_change(filename=f"f{i}.py") for i in range(3000)]

        def maximum(path):
            if "/files?" in path:
                page = int(path.rsplit("=", 1)[1])
                return all_files[(page - 1) * 100:page * 100]
            return fake_fetch(metadata(changed_files=3000))(path)
        self.assertEqual(len(review.collect(maximum, REPO, PR, POLICY_SHA, POLICY)["files"]), 3000)
        with self.assertRaises(review.ReviewError):
            review.collect(lambda path: {} if "/files?" in path else metadata(), REPO, PR, POLICY_SHA, POLICY)
        # A changed line beginning with '+' is still counted once.
        review.collect(fake_fetch(files=[file_change(patch="@@ -1 +1 @@\n---old\n+++new")], old="--old\n", new="++new\n"),
                       REPO, PR, POLICY_SHA, POLICY)

    def test_renames_additions_and_deletions_are_accounted_for(self):
        for f in (file_change(status="added", deletions=0, patch="@@ -0,0 +1 @@\n+return True"),
                  file_change(status="removed", additions=0, patch="@@ -1 +0,0 @@\n-return False"),
                  file_change(status="renamed", previous_filename="old.py", additions=0, deletions=0, patch="")):
            snap = review.collect(fake_fetch(files=[f], new="return False\n" if f["status"] == "renamed" else "return True\n"), REPO, PR, POLICY_SHA, POLICY)
            self.assertEqual(snap["files"][0]["status"], f["status"])
        with self.assertRaises(review.ReviewError):
            review.collect(fake_fetch(files=[file_change(previous_filename="../bad")]), REPO, PR, POLICY_SHA, POLICY)
        with self.assertRaisesRegex(review.ReviewError, "merge_base"):
            review.collect(lambda path: {"merge_base_commit": {"sha": "bad"}} if "/compare/" in path
                           else fake_fetch()(path), REPO, PR, POLICY_SHA, POLICY)

    def test_unready_private_and_invalid_metadata_fail(self):
        for pr in (metadata(draft=True), metadata(state="closed"), metadata(head={"sha": "bad"}),
                   metadata(base={"sha": BASE, "repo": {"full_name": "other/repo", "private": False}}),
                   metadata(base={"sha": BASE, "repo": {"full_name": REPO, "private": True}})):
            with self.assertRaises(review.ReviewError):
                review.pr_metadata(lambda path: pr, REPO, PR)
        for repo, number in (("../bad", PR), (REPO, 0), (REPO, True)):
            with self.assertRaises(review.ReviewError):
                review.pr_metadata(fake_fetch(), repo, number)
        with self.assertRaises(review.ReviewError):
            review.collect(fake_fetch(), REPO, PR, "short", POLICY)

    def test_blob_shape_size_encoding_and_binary_errors(self):
        self.assertEqual(review.file_content(lambda path: source("hello"), REPO, "normal.py", HEAD), "hello")
        for obj in ({**source("x"), "type": "symlink"}, {**source("x"), "size": 2},
                    {**source("x"), "size": 1000001}, {**source("x"), "encoding": "none"}, source("\x00")):
            with self.assertRaises(review.ReviewError):
                review.file_content(lambda path: obj, REPO, "normal.py", HEAD)

    def test_snapshot_integrity_and_required_receipt(self):
        snap = snapshot()
        for field, value, expected in (("policy_sha", HEAD, "policy_mismatch"),
                                        ("context_hash", "bad", "hash_mismatch"),
                                        ("head", "bad", "invalid_commit"),
                                        ("repository", "bad", "invalid_pr"),
                                        ("files", [], "snapshot_files"),
                                        ("scanner_version", "none", "missing_secret_scan")):
            bad = copy.deepcopy(snap)
            bad[field] = value
            if field != "context_hash":
                bad["context_hash"] = review.digest({k: v for k, v in bad.items() if k != "context_hash"})
            with self.subTest(field=field), self.assertRaisesRegex(review.ReviewError, expected):
                review.verify_snapshot(bad, POLICY, POLICY_SHA)

    def test_chunks_never_drop_or_split_a_file(self):
        snap = snapshot()
        snap["files"] *= 3
        policy = {**POLICY, "chunk_bytes": len(review.canonical([snap["files"][0]])) + 1}
        self.assertEqual(len(review.chunks(snap, policy)), 3)
        with self.assertRaisesRegex(review.ReviewError, "chunk_budget"):
            review.chunks(snap, {**policy, "max_chunks": 2})
        with self.assertRaisesRegex(review.ReviewError, "file_exceeds"):
            review.chunks(snap, {**policy, "chunk_bytes": 1})

    def test_scan_errors_prevent_sending_source(self):
        with mock.patch.object(review.subprocess, "run", return_value=types.SimpleNamespace(returncode=0)) as run:
            review.scan_context(snapshot(), "verified-gitleaks")
            self.assertEqual(run.call_args.kwargs["stdout"], subprocess.DEVNULL)
            self.assertIn("--redact", run.call_args.args[0])
        with mock.patch.object(review.subprocess, "run", return_value=types.SimpleNamespace(returncode=1)):
            with self.assertRaisesRegex(review.ReviewError, "secret_scan"):
                review.scan_context(snapshot(), "verified-gitleaks")


    def test_patch_matches_frozen_source_including_unchanged_gaps(self):
        review.verify_patch("a\nb\nc\n", "a\nx\nc\n", "@@ -1,3 +1,3 @@ function\n a\n-b\n+x\n c\n\\ No newline at end of file")
        review.verify_patch("a\nb\nc\n", "x\nb\ny\n", "@@ -1 +1 @@\n-a\n+x\n@@ -3 +3 @@\n-c\n+y")
        for before, after, patch in (("old\n", "new\n", "@@ -1 +1 @@\n-wrong\n+new"),
                                      ("old\n", "new\n", "@@ invalid"),
                                      ("old\n", "new\n", "not a hunk"),
                                      ("old\n", "new\n", "@@ -1,2 +1 @@\n-old\n+new\n@@ -1 +1 @@"),
                                      ("a\nb\n", "z\nx\n", "@@ -2 +2 @@\n-b\n+x"),
                                      ("a\nb\n", "x\nz\n", "@@ -1 +1 @@\n-a\n+x"),
                                      ("a\n", "b\n", "@@ -1 +1 @@\n-a\n+wrong")):
            with self.assertRaises(review.ReviewError):
                review.verify_patch(before, after, patch)

    def test_provider_output_and_billed_usage_are_required(self):
        model = review.MODELS[0]
        self.assertTrue(review.validate_completion(completion(model), model, POLICY["routes"][model]))
        for changes, expected in (({"model": "gpt"}, "model_or_provider"), ({"provider": "other"}, "model_or_provider"),
                                   ({"choices": []}, "choices"), ({"id": ""}, "generation_id"),
                                   ({"usage": {}}, "missing_usage"),
                                   ({"usage": {"prompt_tokens": 1, "completion_tokens": 1}}, "billed_cost")):
            with self.assertRaisesRegex(review.ReviewError, expected):
                review.validate_completion(completion(model, **changes), model, POLICY["routes"][model])
        for reason in ("length", "error", "content_filter", None):
            obj = completion(model)
            obj["choices"][0]["finish_reason"] = reason
            with self.assertRaisesRegex(review.ReviewError, "incomplete_completion"):
                review.validate_completion(obj, model, POLICY["routes"][model])
        obj = completion(model)
        obj["choices"][0]["message"]["content"] = ""
        with self.assertRaisesRegex(review.ReviewError, "empty_completion"):
            review.validate_completion(obj, model, POLICY["routes"][model])

    def test_review_shapes_and_locations_fail_closed(self):
        group = snapshot()["files"]
        clean = clean_result()
        review.validate_review(clean, group)
        self.assertTrue(review.clear_review(clean))
        for changes in ({"extra": "field"}, {"key_issues_to_review": "none"}, {"security_concerns": ""},
                        {"merge_recommendation": "approved"}, {"risk_level": "unknown"}):
            with self.assertRaises(review.ReviewError):
                review.validate_review({**clean, **changes}, group)
        for index, issue in enumerate((finding(), {**finding(), "relevant_file": "other.py"}, {**finding(), "start_line": True},
                      {**finding(), "end_line": 100}, {**finding(), "issue_header": "Good job"},
                      {**finding(), "issue_content": ""}, {**finding(), "extra": 1})):
            result = {**clean, "key_issues_to_review": [issue]}
            if index == 0:
                review.validate_review(result, group)
                self.assertFalse(review.clear_review(result))
            else:
                with self.assertRaises(review.ReviewError):
                    review.validate_review(result, group)
        for changes in ({"security_concerns": "Concern"}, {"merge_recommendation": "merge_with_caution"},
                        {"risk_level": "medium"}):
            self.assertFalse(review.clear_review({**clean, **changes}))

    def test_only_complete_clear_independent_results_can_pass(self):
        snap = snapshot()
        report = model_report(snap)
        post = mock.Mock()
        self.assertEqual(review.publish(snap, report, POLICY, POLICY_SHA, fake_fetch(), post, True), "success")
        self.assertEqual(post.call_args.args[1]["state"], "success")
        for jobs_ok, candidate in ((False, report), (True, None)):
            post.reset_mock()
            with self.assertRaisesRegex(review.ReviewError, "review_execution_incomplete"):
                review.publish(snap, candidate, POLICY, POLICY_SHA, fake_fetch(), post, jobs_ok)
            post.assert_not_called()
        report["reviews"][0]["results"][0]["key_issues_to_review"] = [finding()]
        self.assertEqual(review.publish(snap, report, POLICY, POLICY_SHA, fake_fetch(), post, True), "failure")
        for changes, expected in (({"complete": False}, "incomplete_review"), ({"reviews": []}, "independent_review"),
                                   ({"remaining_files": ["omitted.py"]}, "incomplete_review"),
                                   ({"failed_chunks": 1}, "incomplete_review")):
            with self.assertRaisesRegex(review.ReviewError, expected):
                review.publish(snap, {**model_report(snap), **changes}, POLICY, POLICY_SHA, fake_fetch(), post, True)
        bad = model_report(snap)
        bad["reviews"][1]["model"] = review.MODELS[0]
        with self.assertRaisesRegex(review.ReviewError, "coverage_mismatch"):
            review.publish(snap, bad, POLICY, POLICY_SHA, fake_fetch(), post, True)
        bad = model_report(snap)
        bad["reviews"][0]["generations"][0]["finish_reason"] = "length"
        with self.assertRaisesRegex(review.ReviewError, "generation_receipt"):
            review.publish(snap, bad, POLICY, POLICY_SHA, fake_fetch(), post, True)
        for side in ("head", "base"):
            moving = metadata()
            moving[side]["sha"] = "f" * 40
            with self.assertRaisesRegex(review.ReviewError, "stale_review"):
                review.publish(snap, model_report(snap), POLICY, POLICY_SHA, lambda path: moving, post, True)
        with self.assertRaisesRegex(review.ReviewError, "stale_review"):
            review.publish(snap, model_report(snap), POLICY, POLICY_SHA, fake_fetch(metadata(changed_files=2)), post, True)

    def test_dependabot_bridge_uses_api_metadata_not_artifacts(self):
        self.assertEqual(review.resolve_event({"number": PR}, "pull_request_target", REPO, fake_fetch()), PR)
        self.assertEqual(review.resolve_event({"inputs": {"pr-number": str(PR)}}, "workflow_dispatch", REPO, fake_fetch()), PR)
        self.assertEqual(review.resolve_event({"number": PR}, "pull_request_target", REPO,
                                              fake_fetch(metadata(user={"login": "EXAMPLE"}))), PR)
        for association in ("OWNER", "MEMBER", "COLLABORATOR", "NONE"):
            outsider = fake_fetch(metadata(user={"login": "outside"}, author_association=association))
            with self.subTest(association=association), self.assertRaisesRegex(review.ReviewError, "manual_dispatch"):
                review.resolve_event({"number": PR}, "pull_request_target", REPO, outsider)
            self.assertEqual(review.resolve_event({"inputs": {"pr-number": str(PR)}}, "workflow_dispatch", REPO, outsider), PR)
        bot = metadata(user={"login": "dependabot[bot]"})
        with self.assertRaisesRegex(review.ReviewError, "requires_bridge"):
            review.resolve_event({"number": PR}, "pull_request_target", REPO, lambda path: bot)
        run = {"event": "pull_request", "conclusion": "success", "head_sha": HEAD,
               "path": ".github/workflows/pr-review-trigger.yml", "run_attempt": 1,
               "pull_requests": [{"number": PR}]}

        def bridge(path):
            if "/actions/runs/" in path:
                return run
            if "/commits/" in path:
                raise AssertionError("A commit association cannot select the trigger PR")
            return bot
        event = {"workflow_run": {"id": 123}}
        self.assertEqual(review.resolve_event(event, "workflow_run", REPO, bridge), PR)
        for path in (".github/workflows/pr-review-trigger.yml@main",
                     ".github/workflows/pr-review-trigger.yml@" + HEAD):
            with self.subTest(path=path):
                self.assertEqual(review.resolve_event(event, "workflow_run", REPO,
                                                      lambda url: {**run, "path": path} if "/actions/" in url else bot), PR)
        for path in (".github/workflows/pr-review-trigger.yml@",
                     ".github/workflows/pr-review-trigger.yml.evil@main",
                     ".github/workflows/pr-review-trigger.ymlbackup"):
            with self.subTest(path=path), self.assertRaisesRegex(review.ReviewError, "untrusted_trigger"):
                review.resolve_event(event, "workflow_run", REPO, lambda url: {**run, "path": path})
        for fetch, expected in ((lambda path: {**run, "path": "evil.yml"}, "untrusted_trigger"),
                                (lambda path: {**run, "head_sha": "bad"}, "trigger_commit"),
                                (lambda path: {**run, "pull_requests": []}, "ambiguous_trigger"),
                                (lambda path: {**run, "pull_requests": [{"number": PR}] * 2}, "ambiguous_trigger"),
                                (lambda path: {**run, "pull_requests": None}, "ambiguous_trigger"),
                                (lambda path: {**run, "run_attempt": 2}, "trigger_rerun"),
                                (lambda path: {**run, "run_attempt": None}, "trigger_rerun"),
                                (lambda path: {**run, "run_attempt": "1"}, "trigger_rerun"),
                                (lambda path: run if "/actions/" in path else metadata(),
                                 "non_dependabot")):
            with self.assertRaisesRegex(review.ReviewError, expected):
                review.resolve_event(event, "workflow_run", REPO, fetch)
        for event_name, data in (("push", event), ("workflow_run", {"workflow_run": {"id": 0}})):
            with self.assertRaises(review.ReviewError):
                review.resolve_event(data, event_name, REPO, bridge)

    def test_trigger_cannot_rebind_a_shared_commit_to_another_pr(self):
        run = {"event": "pull_request", "conclusion": "success", "head_sha": HEAD,
               "path": ".github/workflows/pr-review-trigger.yml", "run_attempt": 1,
               "pull_requests": [{"number": PR + 1}]}
        bot = metadata(user={"login": "dependabot[bot]"})
        for state, expected in (("open", "non_dependabot"), ("closed", "pr_not_ready")):
            outsider = metadata(number=PR + 1, state=state, user={"login": "outside"})

            def fetch(path):
                if "/actions/runs/" in path:
                    return run
                if path == f"repos/{REPO}/pulls/{PR + 1}":
                    return outsider
                if "/commits/" in path:
                    return [bot]
                raise AssertionError("The unrelated Dependabot PR must not be selected")

            api = mock.Mock(side_effect=fetch)
            with self.subTest(state=state), self.assertRaisesRegex(review.ReviewError, expected):
                review.resolve_event({"workflow_run": {"id": 123}}, "workflow_run", REPO, api)
            self.assertFalse(any("/commits/" in call.args[0] for call in api.call_args_list))

    def test_budget_requires_capped_key_and_available_reservation(self):
        policy = {**POLICY, "spending_mode": "capped"}
        valid = {"limit": 100, "limit_remaining": 90, "limit_reset": None, "include_byok_in_limit": True}
        review.reserve_budget({"data": valid}, policy, 1)
        review.reserve_budget({"data": {**valid, "limit": 350, "limit_reset": "monthly"}}, policy, 1)
        for changes, reservation in (({"limit": None}, 1), ({"limit": 101}, 1),
                                     ({"limit": 351, "limit_reset": "monthly"}, 1),
                                     ({"limit_remaining": 0}, 1), ({}, 4)):
            with self.assertRaises(review.ReviewError):
                review.reserve_budget({"data": {**valid, **changes}}, policy, reservation)

    def test_budget_rejects_unknown_or_frequently_reset_limits(self):
        policy = {**POLICY, "spending_mode": "capped"}
        data = {"limit": 100, "limit_remaining": 90, "include_byok_in_limit": True}
        for reset in ("daily", "weekly", "unknown", False):
            with self.subTest(reset=reset), self.assertRaisesRegex(review.ReviewError, "unsupported_key_limit_reset"):
                review.reserve_budget({"data": {**data, "limit_reset": reset}}, policy, 1)
        with self.assertRaisesRegex(review.ReviewError, "unsupported_key_limit_reset"):
            review.reserve_budget({"data": data}, policy, 1)

    def test_budget_requires_byok_charges_to_count_towards_limit(self):
        policy = {**POLICY, "spending_mode": "capped"}
        data = {"limit": 100, "limit_remaining": 90, "limit_reset": None}
        for byok in (False, None, 1, "true"):
            with self.subTest(byok=byok), self.assertRaisesRegex(review.ReviewError, "byok_must_count"):
                review.reserve_budget({"data": {**data, "include_byok_in_limit": byok}}, policy, 1)
        with self.assertRaisesRegex(review.ReviewError, "byok_must_count"):
            review.reserve_budget({"data": data}, policy, 1)

    def test_payg_accepts_uncapped_keys_without_claiming_byok_coverage(self):
        for fields in ({}, {"limit_reset": None, "include_byok_in_limit": False}):
            review.reserve_budget({"data": {"limit": None, "limit_remaining": None, **fields}}, POLICY, 3)
        review.reserve_budget({"data": {"limit": 500, "limit_remaining": 3}}, POLICY, 3)

    def test_payg_rejects_missing_or_inconsistent_metadata(self):
        for data in ({}, {"limit": None}, {"limit_remaining": None},
                     {"limit": None, "limit_remaining": 10}):
            with self.subTest(data=data), self.assertRaisesRegex(review.ReviewError, "key_budget_metadata"):
                review.reserve_budget({"data": data}, POLICY, 1)

    def test_payg_rejects_invalid_limit_and_exhausted_remaining(self):
        for limit in (0, -1, float("nan"), float("inf"), "100", True):
            with self.subTest(limit=limit), self.assertRaisesRegex(review.ReviewError, "invalid_key_limit"):
                review.reserve_budget({"data": {"limit": limit, "limit_remaining": 3}}, POLICY, 1)
        for remaining in (None, 0, -1, float("nan"), float("inf"), "3", True):
            with self.subTest(remaining=remaining), self.assertRaisesRegex(review.ReviewError, "key_budget_exhausted"):
                review.reserve_budget({"data": {"limit": 100, "limit_remaining": remaining}}, POLICY, 1)

    def test_all_spending_modes_reject_invalid_or_excessive_reservations(self):
        for mode in ("payg", "capped"):
            for reservation in (0, -1, float("nan"), float("inf"), "1", True, 3.01, 3.0000000000000004):
                with self.subTest(mode=mode, reservation=reservation), self.assertRaisesRegex(review.ReviewError, "review_budget_exceeded"):
                    review.reserve_budget({"data": {}}, {**POLICY, "spending_mode": mode}, reservation)
        with self.assertRaisesRegex(review.ReviewError, "unsupported_spending_mode"):
            review.reserve_budget({"data": {}}, {**POLICY, "spending_mode": "unknown"}, 1)

    def test_json_transport_is_bounded_redirect_free_and_sanitised(self):
        with mock.patch.object(review, "request_json", return_value={"ok": True}) as send, mock.patch.dict(os.environ, {"GH_TOKEN": "synthetic"}):
            self.assertEqual(review.github("test"), {"ok": True})
            self.assertEqual(send.call_args.args[0], "https://api.github.com/test")
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok":true}'
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(review, "build_opener", return_value=opener):
            self.assertEqual(review.request_json("https://api.github.com/test", "fixture-key", {"x": 1}), {"ok": True})
            request = opener.open.call_args.args[0]
            self.assertEqual(request.get_header("Authorization"), "Bearer fixture-key")
            review.request_json("https://api.github.com/test", "")
            self.assertIsNone(opener.open.call_args.args[0].get_header("Authorization"))
            for error in (HTTPError("url", 429, "secret-body", {}, None), URLError("secret-body"),
                          TimeoutError("secret-body"), ValueError("secret-body")):
                opener.open.side_effect = error
                with self.assertRaises(review.ReviewError) as caught:
                    review.request_json("https://api.github.com/test", "fixture-key")
                self.assertNotIn("secret-body", str(caught.exception))
            opener.open.side_effect = None
            observer = mock.Mock()
            review.request_json("https://api.github.com/test", "", response_observer=observer)
            observer.assert_called_once()
            opener.open.side_effect = None
            for raw, code in ((b'{"cost":0,"cost":1}', "duplicate_api_json_key"),
                              (b'{"cost":NaN}', "non_finite_api_json_number")):
                response.__enter__.return_value.read.return_value = raw
                with self.assertRaisesRegex(review.ReviewError, code):
                    review.request_json("https://api.github.com/test", "")
            response.__enter__.return_value.read.return_value = b"x" * (review.MAX_RESPONSE_BYTES + 1)
            with self.assertRaisesRegex(review.ReviewError, "oversized"):
                review.request_json("https://api.github.com/test", "")
        with self.assertRaisesRegex(review.ReviewError, "origin"):
            review.request_json("https://attacker.example", "fixture-key")
        with self.assertRaisesRegex(review.ReviewError, "redirect"):
            review.NoRedirect().redirect_request(None, None, 302, "", {}, "https://attacker.example")

    def test_summary_escapes_model_output_and_failure_identity(self):
        report = model_report()
        report["reviews"][0]["results"][0]["key_issues_to_review"] = [{**finding(), "issue_content": "<img src=x>"}]
        self.assertIn("&lt;img", review.render_summary(report))
        post = mock.Mock()
        review.failed_status({"repository": REPO, "head": HEAD, "snapshot_kind": "installed_pr_review_v1",
                              "publication_capability": "status", "caller_repository": REPO,
                              "repository_id": 123, "caller_repository_id": 123}, REPO, post)
        self.assertEqual(post.call_args.args[1]["state"], "failure")
        with self.assertRaises(review.ReviewError):
            review.failed_status({"repository": "other/repo", "head": HEAD}, REPO, post)


class ReviewAgentTests(unittest.TestCase):
    def test_payload_disallows_routing_fallback_and_model_substitution(self):
        for model in review.MODELS:
            body = agent.request_body(model, POLICY, "system", "user")
            self.assertFalse(body["provider"]["allow_fallbacks"])
            self.assertTrue(body["provider"]["require_parameters"])
            self.assertEqual(body["provider"]["only"], [POLICY["routes"][model]["slug"]])
            self.assertEqual(body["provider"]["quantizations"], ["fp8"])
            self.assertEqual(body["temperature"], 0)
        self.assertEqual(agent.request_body(review.MODELS[1], POLICY, "", "")["reasoning"], {"enabled": True})
        schema = agent.request_body(review.MODELS[0], POLICY, "", "")["response_format"]
        self.assertEqual(schema["type"], "json_schema")
        self.assertIs(schema["json_schema"]["strict"], True)
        self.assertEqual(schema["json_schema"]["schema"], review.REVIEW_SCHEMA)
        self.assertEqual(agent.request_body(review.MODELS[1], POLICY, "", "")["response_format"], {"type": "json_object"})
        with self.assertRaises(review.ReviewError):
            agent.request_body("claude", POLICY, "", "")

    def test_key_missing_stops_before_imports_or_calls(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}), self.assertRaisesRegex(review.ReviewError, "not_provisioned"):
            agent.review_snapshot(snapshot(), POLICY, send=mock.Mock())

    def test_independence_failures_and_cost_errors_cannot_complete(self):
        snap = snapshot()
        prompts = {model: [("system", "identical frozen context")] for model in review.MODELS}

        def send(url, key, body=None, **kwargs):
            if url.endswith("/key"):
                return {"data": {"limit": 100, "limit_remaining": 90,
                                 "limit_reset": None, "include_byok_in_limit": True}}
            return completion(body["model"])

        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "synthetic-fixture-key"}):
            report = agent.review_snapshot(snap, POLICY, send, lambda *args: prompts,
                                           lambda content: json.loads(content)["review"])
            self.assertTrue(report["complete"])
            self.assertEqual(report["remaining_files"], [])
            self.assertEqual([r["model"] for r in report["reviews"]], list(review.MODELS))
            wrong = {**prompts, review.MODELS[1]: [("system", "previous model's opinion")]}
            with self.assertRaisesRegex(review.ReviewError, "context_mismatch"):
                agent.review_snapshot(snap, POLICY, send, lambda *args: wrong)
            for error in (review.ReviewError("fixture_failure"), ValueError("raw-private-response")):
                with self.assertRaises(review.ReviewError):
                    agent.review_snapshot(snap, POLICY, send, lambda *args: prompts,
                                          mock.Mock(side_effect=error))

            def expensive(url, key, body=None, **kwargs):
                obj = send(url, key, body)
                if body:
                    obj["usage"]["cost"] = 99
                return obj
            with self.assertRaisesRegex(review.ReviewError, "billed_cost"):
                agent.review_snapshot(snap, POLICY, expensive, lambda *args: prompts,
                                      lambda content: clean_result())

    def test_unexpected_first_bill_stops_before_another_paid_call(self):
        prompts = {model: [("system", "identical frozen context")] for model in review.MODELS}
        paid = []

        def send(url, key, body=None, **kwargs):
            if url.endswith("/key"):
                return {"data": {"limit": None, "limit_remaining": None}}
            paid.append(body["model"])
            response = completion(body["model"])
            # This bill fits the whole reservation but leaves too little for
            # the second model's conservative ceiling.
            response["usage"]["cost"] = 0.06
            return response

        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "synthetic-fixture-key"}):
            with self.assertRaisesRegex(review.ReviewError, "unexpected_billed_cost"):
                agent.review_snapshot(snapshot(), POLICY, send, lambda *args: prompts,
                                      lambda content: clean_result())
        self.assertEqual(paid, [review.MODELS[0]])

    def test_aggregate_reservation_exhaustion_stops_before_paid_requests(self):
        prompts = {model: [("s" * 1_800_000, "context")] for model in review.MODELS}
        send = mock.Mock(return_value={"data": {"limit": None, "limit_remaining": None}})
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "synthetic-fixture-key"}):
            with self.assertRaisesRegex(review.ReviewError, "review_budget_exceeded"):
                agent.review_snapshot(snapshot(), POLICY, send, lambda *args: prompts)
        self.assertEqual(len(send.call_args_list), 1)
        self.assertTrue(send.call_args.args[0].endswith("/key"))

    def test_billing_overruns_stop_later_calls_after_the_charge(self):
        prompts = {model: [("system", "context")] for model in review.MODELS}
        bills = []

        def send(url, key, body=None, **kwargs):
            if url.endswith("/key"):
                return {"data": {"limit": None, "limit_remaining": None}}
            bills.append(body["model"])
            response = completion(body["model"])
            response["usage"]["cost"] = cost
            return response

        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "synthetic-fixture-key"}):
            cost = 0
            baseline = agent.review_snapshot(snapshot(), POLICY, send, lambda *args: prompts,
                                            lambda content: clean_result())
            initial = baseline["reservation_usd"]
            for cost in (initial - 0.001, initial, initial + 0.000001, 3, 3.000001):
                bills.clear()
                with self.subTest(cost=cost), self.assertRaisesRegex(review.ReviewError, "unexpected_billed_cost"):
                    agent.review_snapshot(snapshot(), POLICY, send, lambda *args: prompts,
                                          lambda content: clean_result())
                self.assertEqual(bills, [review.MODELS[0]])

    def test_json_parser_rejects_duplicate_keys(self):
        module = types.ModuleType("pr_agent.algo.output_models")
        module.PRReview = types.SimpleNamespace(model_validate=mock.Mock())
        with mock.patch.dict(sys.modules, {"pr_agent.algo.output_models": module,
                                           "pydantic": types.SimpleNamespace(ValidationError=SchemaRejection)}):
            self.assertEqual(agent.parse_review(json.dumps({"review": clean_result()})), clean_result())
            with self.assertRaisesRegex(review.ReviewError, "duplicate_review_key"):
                agent.parse_review('{"review":{},"review":{}}')
            with self.assertRaisesRegex(review.ReviewError, "invalid_review_json"):
                agent.parse_review("partial {")

    def test_output_categories_are_fixed_and_preserve_strict_rejection(self):
        validator = mock.Mock()
        module = types.SimpleNamespace(PRReview=types.SimpleNamespace(model_validate=validator))
        cases = [("partial private-data", "json_syntax"),
                 ('{} trailing-private-data', "json_syntax"),
                 ('```json\n{}\n```', "json_syntax"),
                 ('NaN', "json_syntax"), ('Infinity', "json_syntax"), ('-Infinity', "json_syntax"),
                 ('{"review":{"risk_level":NaN}}', "json_syntax"),
                 ('{"review":{},"review":{}}', "duplicate_keys"),
                 ('{"review":{"private-key":1,"private-key":2}}', "duplicate_keys"),
                 ('{"review":{},"revi\\u0065w":{}}', "duplicate_keys"),
                 ('null', "root_or_nesting"), ('[]', "root_or_nesting"),
                 ('{"review":null}', "root_or_nesting"),
                 ('{"review":"private-data"}', "root_or_nesting")]
        with mock.patch.dict(sys.modules, {"pr_agent.algo.output_models": module,
                                           "pydantic": types.SimpleNamespace(ValidationError=SchemaRejection)}):
            self.assertEqual(agent.parse_review(json.dumps({"review": clean_result()})), clean_result())
            validator.assert_called_once_with({"review": clean_result()}, strict=True)
            for content, category in cases:
                with self.subTest(category=category, content=content):
                    with self.assertRaises(agent.OutputError) as caught:
                        agent.parse_review(content)
                    self.assertEqual(caught.exception.category, category)
                    self.assertNotIn("private", str(caught.exception))
            validator.side_effect = SchemaRejection("private-value; private-field-path; private-source")
            with self.assertRaises(agent.OutputError) as caught:
                agent.parse_review(json.dumps({"review": clean_result()}))
            self.assertEqual(caught.exception.category, "strict_schema")
            self.assertNotIn("private", str(caught.exception))

    def test_native_adapter_pin_and_context_contract_without_model_calls(self):
        native_system = "Review only concrete defects.\nPreserve uncertainty.\nThe output must be a YAML object equivalent to PRReview.\nExample output:\n```yaml\nreview: {}\n```\nAnswer should be a valid YAML."
        native_user = "Preserve complete PR evidence.\nResponse (should be a valid YAML, and nothing else):\n```yaml\n"
        settings = types.SimpleNamespace(pr_review_prompt=types.SimpleNamespace(system=native_system, user=native_user))
        configured = {}

        def setting(key, value):
            configured[key] = value
            if key == "pr_review_prompt.system":
                settings.pr_review_prompt.system = value
            if key == "pr_review_prompt.user":
                settings.pr_review_prompt.user = value
        settings.set = setting

        class FakeNativeReviewer:
            async def _get_prediction(self, model, diff):
                self.ai_handler.get_output_token_reserve(model)
                return await self.ai_handler.chat_completion(model=model, temperature=0,
                                                             system=self.token_handler.system, user=self.token_handler.user + diff)

        modules = {}
        for name, fields in (("pr_agent.algo.token_handler", {"TokenHandler": lambda pr, variables, system, user, model: types.SimpleNamespace(system=system, user=user)}),
                             ("pr_agent.config_loader", {"get_settings": lambda: settings}),
                             ("pr_agent.log", {"get_logger": lambda: types.SimpleNamespace(remove=lambda: None)}),
                             ("pr_agent.tools.pr_reviewer", {"PRReviewer": FakeNativeReviewer})):
            module = types.ModuleType(name)
            module.__dict__.update(fields)
            modules[name] = module
        with tempfile.TemporaryDirectory() as directory:
            spec = types.SimpleNamespace(origin=str(Path(directory) / "pr_agent/__init__.py"))
            run = mock.Mock(side_effect=[types.SimpleNamespace(stdout=review.PR_AGENT_SHA), types.SimpleNamespace(stdout="")])
            with mock.patch.dict(sys.modules, modules), mock.patch.object(agent.importlib.util, "find_spec", return_value=spec), mock.patch.object(agent.subprocess, "run", run):
                snap = snapshot()
                prompts = agent.prepare_prompts(snap, review.chunks(snap, POLICY), POLICY)
                self.assertEqual(prompts[review.MODELS[0]], prompts[review.MODELS[1]])
                system = prompts[review.MODELS[0]][0][0]
                self.assertTrue(system.startswith("Review only concrete defects.\nPreserve uncertainty.\n"))
                self.assertIn("The output must be a JSON object", system)
                self.assertIn("review.security_concerns, never beside review at the root", system)
                self.assertIn('"review":{"key_issues_to_review":[],"merge_recommendation":"merge_with_caution","risk_level":"medium","security_concerns":"No"}', system)
                self.assertIn(review.canonical(review.REVIEW_SCHEMA).decode(), system)
                self.assertNotIn("```yaml", system)
                self.assertNotIn("Answer should be a valid YAML", system)
                user = prompts[review.MODELS[0]][0][1]
                self.assertTrue(user.startswith("Preserve complete PR evidence.\n"))
                self.assertIn("Response (must be one JSON object", user)
                self.assertNotIn("valid YAML", user)
                self.assertNotIn("```yaml", user)
                self.assertEqual(settings.pr_review_prompt.user, native_user)
                self.assertEqual(configured["config.fallback_models"], [])
                self.assertTrue(configured["config.model"].endswith(review.MODELS[1]))
                for unsupported in ("The output must be a YAML object", "Example output: changed layout"):
                    settings.pr_review_prompt.system = unsupported
                    run.side_effect = [types.SimpleNamespace(stdout=review.PR_AGENT_SHA),
                                       types.SimpleNamespace(stdout="")]
                    with self.assertRaisesRegex(review.ReviewError, "unsupported_native_prompt_format"):
                        agent.prepare_prompts(snap, [], POLICY)
                settings.pr_review_prompt.system = native_system
                for unsupported in ("changed user layout", "Response (should be a valid YAML, and nothing else):\n```json"):
                    settings.pr_review_prompt.user = unsupported
                    run.side_effect = [types.SimpleNamespace(stdout=review.PR_AGENT_SHA),
                                       types.SimpleNamespace(stdout="")]
                    with self.assertRaisesRegex(review.ReviewError, "unsupported_native_prompt_format"):
                        agent.prepare_prompts(snap, [], POLICY)
                settings.pr_review_prompt.user = native_user
            with mock.patch.object(agent.importlib.util, "find_spec", return_value=None):
                with self.assertRaisesRegex(review.ReviewError, "not_installed"):
                    agent.prepare_prompts(snapshot(), [], POLICY)
            with mock.patch.object(agent.importlib.util, "find_spec", return_value=spec), mock.patch.object(agent.subprocess, "run", return_value=types.SimpleNamespace(stdout="wrong")):
                with self.assertRaisesRegex(review.ReviewError, "pin_mismatch"):
                    agent.prepare_prompts(snapshot(), [], POLICY)
            secret_path = Path(directory) / "pr_agent/settings/.secrets.toml"
            secret_path.parent.mkdir(parents=True)
            secret_path.touch()
            with mock.patch.object(agent.importlib.util, "find_spec", return_value=spec):
                with self.assertRaisesRegex(review.ReviewError, "secret_file_refused"):
                    agent.prepare_prompts(snapshot(), [], POLICY)


class ReviewCliTests(unittest.TestCase):
    def setUp(self):
        environment = mock.patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "1", "GITHUB_ACTIONS": "false",
                                                  "GITHUB_STEP_SUMMARY": ""})
        environment.start()
        self.addCleanup(environment.stop)

    def test_script_preserves_the_adapters_sanitised_failure_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_path, snap_path = root / "policy.json", root / "snapshot.json"
            review.write_json(policy_path, POLICY)
            review.write_json(snap_path, snapshot())
            args = [str(ROOT / "scripts/pr_review.py"), "review", "--policy", str(policy_path),
                    "--policy-sha", POLICY_SHA, "--snapshot", str(snap_path)]
            errors = io.StringIO()
            with mock.patch.object(sys, "argv", args), \
                    mock.patch.object(agent, "review_snapshot", side_effect=review.ReviewError("key_budget_exhausted")), \
                    redirect_stderr(errors), self.assertRaises(SystemExit) as caught:
                runpy.run_path(args[0], run_name="__main__")
            self.assertEqual(caught.exception.code, 1)
            self.assertIn("key_budget_exhausted", errors.getvalue())

    def test_reruns_cannot_read_source_spend_or_mutate_status(self):
        args = ["--policy", "unused.json", "--policy-sha", POLICY_SHA, "--repo", REPO]
        for attempt in ("2", "", None):
            with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), redirect_stderr(io.StringIO()) as errors:
                if attempt is None:
                    os.environ.pop("GITHUB_RUN_ATTEMPT", None)
                else:
                    os.environ["GITHUB_RUN_ATTEMPT"] = attempt
                with mock.patch.object(review, "read_json") as source_read, \
                        mock.patch.object(review, "github") as api, \
                        mock.patch.object(agent, "review_snapshot") as paid:
                    for command in ("capture", "review", "publish", "fail"):
                        with self.subTest(attempt=attempt, command=command):
                            self.assertEqual(review.main([command, *args]), 1)
                    source_read.assert_not_called()
                    api.assert_not_called()
                    paid.assert_not_called()
                self.assertIn("workflow_rerun_requires_fresh_dispatch", errors.getvalue())

    def test_capture_review_publish_fail_and_safe_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_path, snap_path, report_path = (root / name for name in ("policy.json", "snapshot.json", "review.json"))
            review.write_json(policy_path, POLICY)
            args = ["--policy", str(policy_path), "--policy-sha", POLICY_SHA, "--snapshot", str(snap_path),
                    "--report", str(report_path), "--repo", REPO]
            fetch = fake_fetch()
            post = mock.Mock(side_effect=lambda path, payload=None: fetch(path) if payload is None else {})
            with mock.patch.object(review, "github", post), mock.patch.object(review, "scan_context"), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(review.main(["capture", *args, "--pr", str(PR)]), 0)
                self.assertFalse(any(len(call.args) > 1 for call in post.call_args_list))
                self.assertEqual(review.main(["capture", *args, "--pr", str(PR), "--publish-pending"]), 0)
                with mock.patch.object(agent, "review_snapshot", return_value=model_report()):
                    self.assertEqual(review.main(["review", *args]), 0)
                self.assertEqual(review.main(["publish", *args, "--jobs-ok"]), 0)
                summary = root / "summary.md"
                with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}):
                    self.assertEqual(review.main(["publish", *args, "--jobs-ok"]), 0)
                    self.assertIn("Independent PR review", summary.read_text())
                self.assertEqual(review.main(["publish", *args]), 1)
                report_path.unlink()
                self.assertEqual(review.main(["publish", *args]), 1)
                self.assertEqual(review.main(["fail", *args]), 0)
                event_path = root / "event.json"
                review.write_json(event_path, {"inputs": {"pr-number": str(PR)}})
                with mock.patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(event_path), "GITHUB_EVENT_NAME": "workflow_dispatch"}):
                    self.assertEqual(review.main(["capture", *args]), 0)
                with mock.patch.object(review, "read_json", side_effect=ValueError("secret-value")):
                    self.assertEqual(review.main(["capture", *args]), 1)
                with mock.patch.object(review, "read_json", side_effect=review.ReviewError("known_failure")):
                    self.assertEqual(review.main(["capture", *args]), 1)
                with mock.patch.object(review.Path, "stat", return_value=types.SimpleNamespace(st_size=review.MAX_RESPONSE_BYTES + 1)):
                    with self.assertRaises(review.ReviewError):
                        review.read_json(policy_path)
            # Exercise the actual script entry point without any HTTP call.
            os.environ.pop("GITHUB_RUN_ATTEMPT", None)
            with mock.patch.object(sys, "argv", [str(ROOT / "scripts/pr_review.py"), "fail", *args]), mock.patch.object(review.Path, "stat", side_effect=OSError), self.assertRaises(SystemExit) as caught, redirect_stderr(io.StringIO()):
                runpy.run_path(str(ROOT / "scripts/pr_review.py"), run_name="__main__")
            self.assertEqual(caught.exception.code, 1)

    def test_completed_findings_do_not_trigger_generic_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snap = snapshot()
            review.write_json(root / "policy.json", POLICY)
            review.write_json(root / "snapshot.json", snap)
            review.write_json(root / "snapshot.identity.json", {key: snap[key] for key in
                              ("repository", "head", "snapshot_kind", "publication_capability",
                               "caller_repository", "repository_id", "caller_repository_id")})
            args = ["--policy", str(root / "policy.json"), "--policy-sha", POLICY_SHA,
                    "--snapshot", str(root / "snapshot.json"), "--report", str(root / "review.json"),
                    "--repo", REPO]
            for changes in ({}, {"key_issues_to_review": [finding()]},
                            {"merge_recommendation": "merge_with_caution"},
                            {"security_concerns": "Concern"}, {"risk_level": "medium"}):
                report = model_report(snap)
                report["reviews"][0]["results"][0].update(changes)
                review.write_json(root / "review.json", report)
                writes = []

                def api(path, payload=None):
                    if payload is None:
                        return fake_fetch()(path)
                    self.assertTrue((root / "summary.md").exists())
                    writes.append(payload)
                    return {"state": payload["state"]}

                with mock.patch.object(review, "github", api), \
                        mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(root / "summary.md")}), \
                        redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    result = review.main(["publish", *args, "--jobs-ok"])
                    if result:
                        review.main(["fail", *args])
                self.assertEqual(result, 0)
                self.assertEqual(len(writes), 1)
                self.assertEqual(writes[0]["state"], "failure" if changes else "success")
                self.assertIn("complete", writes[0]["description"])
                (root / "summary.md").unlink()

    def test_operational_failures_publish_only_the_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snap = snapshot()
            for name, data in (("policy.json", POLICY), ("snapshot.json", snap),
                               ("snapshot.identity.json", {key: snap[key] for key in
                                ("repository", "head", "snapshot_kind", "publication_capability",
                                 "caller_repository", "repository_id", "caller_repository_id")})):
                review.write_json(root / name, data)
            args = ["--policy", str(root / "policy.json"), "--policy-sha", POLICY_SHA,
                    "--snapshot", str(root / "snapshot.json"), "--report", str(root / "review.json"),
                    "--repo", REPO]
            for case in ("missing", "incomplete", "stale", "invalid", "render", "save", "api"):
                report = model_report(snap)
                if case == "incomplete":
                    report["complete"] = False
                if case == "invalid":
                    report["reviews"][0]["results"][0]["risk_level"] = "private-invalid-value"
                review.write_json(root / "review.json", report)
                if case == "missing":
                    (root / "review.json").unlink()
                current = metadata()
                if case == "stale":
                    current["head"]["sha"] = "f" * 40
                fetch = fake_fetch(current)
                writes = []
                attempted = []

                def api(path, payload=None):
                    if payload is None:
                        return fetch(path)
                    attempted.append(payload)
                    if case == "api" and len(attempted) == 1:
                        raise OSError("private-api-error")
                    writes.append(payload)
                    return {}

                render = mock.Mock(side_effect=ValueError("private-render-error")) if case == "render" else review.render_summary
                summary = root / "absent" / "summary.md" if case == "save" else root / "summary.md"
                with mock.patch.object(review, "github", api), \
                        mock.patch.object(review, "render_summary", render), \
                        mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}), \
                        redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(review.main(["publish", *args, "--jobs-ok"]), 1)
                    self.assertEqual(review.main(["fail", *args]), 0)
                self.assertNotIn("private", errors.getvalue())
                self.assertEqual(len(writes), 1)
                self.assertEqual(len(attempted), 2 if case == "api" else 1)
                self.assertEqual(writes[0]["description"], "Review failed or was cancelled; inspect this run")


class BenchmarkTests(unittest.TestCase):
    def test_pinned_manifest_and_clean_control_behaviour(self):
        manifest = review.read_json(ROOT / "benchmarks/pr-review-cases.json")
        self.assertEqual(len(manifest["cases"]), 200)
        self.assertEqual(len({case["id"] for case in manifest["cases"]}), 200)
        controls = manifest["cases"][-4:]
        inputs = (((0, 0), (2, -1)), ((-1,), (0,), (1,)), ((0,), (-3,), (4,)), (([],), ([1, 2, 3],), ([-1, 1],)))
        for case, arguments in zip(controls, inputs, strict=True):
            with mock.patch.object(benchmark, "github", side_effect=AssertionError("synthetic control must stay offline")):
                snap = benchmark.prepare_case(case, POLICY, POLICY_SHA)
            review.verify_snapshot(snap, POLICY, POLICY_SHA)
            before, after = {}, {}
            # Execute only our four tracked synthetic controls, never remote PR source.
            exec(case["source"]["before"], before)
            exec(case["source"]["after"], after)
            old_fn = next(value for value in before.values() if callable(value))
            new_fn = next(value for value in after.values() if callable(value))
            for args in arguments:
                self.assertEqual(old_fn(*args), new_fn(*args))
            self.assertEqual(case["reference_findings"], [])

    def test_historical_cases_are_public_immutable_and_unpublished(self):
        case = {"id": "case1", "repository": REPO, "pr": PR, "base": MERGE_BASE, "head": HEAD}

        def fetch(path):
            if path == f"repos/{REPO}":
                return {"private": False}
            if "/compare/" in path:
                return {"merge_base_commit": {"sha": MERGE_BASE}, "files": [file_change()]}
            return fake_fetch()(path)
        with mock.patch.object(benchmark, "github", side_effect=fetch) as api:
            snap = benchmark.prepare_case(case, POLICY, POLICY_SHA)
            self.assertEqual(snap["base"], MERGE_BASE)
            self.assertFalse(any("/statuses/" in call.args[0] for call in api.call_args_list))
        with mock.patch.object(benchmark, "github", return_value={"private": True}), self.assertRaises(review.ReviewError):
            benchmark.prepare_case(case, POLICY, POLICY_SHA)
        with mock.patch.object(benchmark, "github", side_effect=[{"private": False}, {"merge_base_commit": {"sha": BASE}, "files": []}]), self.assertRaises(review.ReviewError):
            benchmark.prepare_case(case, POLICY, POLICY_SHA)
        with self.assertRaises(review.ReviewError):
            benchmark.prepare_case({**case, "head": "invalid"}, POLICY, POLICY_SHA)

    def test_benchmark_default_has_no_paid_calls_and_cache_is_checked(self):
        controls = review.read_json(ROOT / "benchmarks/pr-review-cases.json")["cases"][-4:]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_path, cases_path = root / "policy.json", root / "cases.json"
            review.write_json(policy_path, POLICY)
            review.write_json(cases_path, {"cases": controls})
            args = ["--cases", str(cases_path), "--policy", str(policy_path), "--policy-sha", POLICY_SHA,
                    "--output", str(root / "out"), "--limit", "4"]
            with mock.patch.object(benchmark, "scan_context"), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), mock.patch.object(agent, "review_snapshot") as paid:
                self.assertEqual(benchmark.main(args), 0)
                paid.assert_not_called()
                paid.return_value = model_report()
                self.assertEqual(benchmark.main([*args, "--run"]), 0)
                self.assertEqual(paid.call_count, 4)
                self.assertEqual(benchmark.main([*args, "--offset", "100"]), 1)
                self.assertEqual(benchmark.main([*args, "--limit", "0"]), 1)
                review.write_json(cases_path, {"cases": [{**controls[0], "id": "../escape"}]})
                self.assertEqual(benchmark.main(args), 1)
                review.write_json(cases_path, {"cases": [{**controls[0], "head": "c" * 40}]})
                self.assertEqual(benchmark.main(args), 1)
                with mock.patch.object(sys, "argv", [str(ROOT / "scripts/pr_review_benchmark.py"), *args]), self.assertRaises(SystemExit) as caught:
                    runpy.run_path(str(ROOT / "scripts/pr_review_benchmark.py"), run_name="__main__")
                self.assertEqual(caught.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
