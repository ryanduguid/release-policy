# Copyright 2026 Ryan Duguid
# SPDX-License-Identifier: LicenseRef-Duguid-Public-Attribution-1.0

from __future__ import annotations

import ast
import base64
import copy
import hashlib
import importlib
import io
import json
import os
import runpy
import socket
import ssl
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
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
    obj = {"id": f"msg_1-{review.MODELS.index(selected_model)}", "type": "message", "role": "assistant",
           "model": selected_model, "stop_reason": "end_turn",
           "content": [{"type": "thinking", "thinking": "", "signature": "fixture"},
                       {"type": "text", "text": json.dumps({"review": clean_result()})}],
           "usage": {"input_tokens": 100, "output_tokens": 100}}
    obj.update(changes)
    return obj


def counted(paid=None):
    """Answer each free token count with one token; send paid calls to paid."""
    def send(url, key, body=None, **kwargs):
        if url.endswith("/count_tokens"):
            return {"input_tokens": 1}
        return completion(body["model"]) if paid is None else paid(url, key, body, **kwargs)
    return send


def model_report(snap=None):
    snap = snapshot() if snap is None else snap
    groups = review.chunks(snap, POLICY)
    return {"schema": 3, "context_hash": snap["context_hash"], "complete": True, "engine_sha": review.PR_AGENT_SHA,
            "remaining_files": [], "failed_chunks": 0, "reviews": [
                {"model": model,
                 "chunk_hashes": [review.digest(group) for group in groups],
                 "results": [clean_result() for _ in groups],
                 "generations": [{"stop_reason": "end_turn",
                                  "generation_id_sha256": review.hashlib.sha256(f"msg_{review.MODELS.index(model)}-{i}".encode()).hexdigest(),
                                  "usage": {"input_tokens": 100, "output_tokens": 100, "cost": 0.0024}}
                                 for i, _ in enumerate(groups)]} for model in review.MODELS]}


class AdvisoryStatusTests(unittest.TestCase):
    def setUp(self):
        local = mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "false"})
        local.start()
        self.addCleanup(local.stop)

    def actions_context(self):
        return {"GITHUB_ACTIONS": "true", "GITHUB_SERVER_URL": "https://github.com",
                "GITHUB_REPOSITORY": REPO, "GITHUB_REPOSITORY_ID": "123",
                "GITHUB_RUN_ID": "456", "GITHUB_RUN_ATTEMPT": "1"}

    def test_actions_paths_publish_exact_run_and_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            review.write_json(root / "policy.json", POLICY)
            args = ["--policy", str(root / "policy.json"), "--policy-sha", POLICY_SHA,
                    "--snapshot", str(root / "snapshot.json"), "--repo", REPO]
            writes = []

            def api(path, payload=None):
                if payload is None:
                    return fake_fetch()(path)
                writes.append((path, payload))
                return {}

            with mock.patch.dict(os.environ, self.actions_context()), \
                    mock.patch.object(review, "github", api), \
                    mock.patch.object(review, "scan_context"), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(review.main(["capture", *args, "--pr", str(PR),
                                              "--publish-pending"]), 0)
                snap = review.read_json(root / "snapshot.json")
                for findings in (False, True):
                    report = model_report(snap)
                    if findings:
                        report["reviews"][0]["results"][0]["key_issues_to_review"] = [finding()]
                    self.assertEqual(review.publish(snap, report, POLICY, POLICY_SHA,
                                                    fake_fetch(), api, True),
                                     "failure" if findings else "success")
                self.assertEqual(review.main(["fail", *args]), 0)
            expected = [("pending", "Two independent reviews requested"),
                        ("success", "Both reviews complete; inspect CI"),
                        ("failure", "Reviews complete; findings or caution require attention"),
                        ("failure", "Review failed or was cancelled; inspect this run")]
            self.assertEqual(writes, [(f"repos/{REPO}/statuses/{HEAD}",
                                      {"state": state, "context": review.CONTEXT,
                                       "description": description,
                                       "target_url": f"https://github.com/{REPO}/actions/runs/456/attempts/1"})
                                     for state, description in expected])
            self.assertNotIn("target_url", json.dumps(snap))

    def test_actions_context_fails_before_status_write(self):
        invalid = {"GITHUB_SERVER_URL": [None, "https://github.com/", "https://example.com"],
                   "GITHUB_REPOSITORY": [None, "other/repo"],
                   "GITHUB_REPOSITORY_ID": [None, "0", "-1", "+123", " 123", "0123", "124"],
                   "GITHUB_RUN_ID": [None, "0", "+456", " 456", "0456", "x", "1" * 21],
                   "GITHUB_RUN_ATTEMPT": [None, "0", "2", "01", " 1"]}
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=value), \
                        mock.patch.dict(os.environ, self.actions_context()):
                    if value is None:
                        os.environ.pop(field)
                    else:
                        os.environ[field] = value
                    post = mock.Mock()
                    with self.assertRaisesRegex(review.ReviewError,
                                                "invalid_status_run_context|workflow_rerun_requires_fresh_dispatch"):
                        review.post_advisory_status(REPO, 123, HEAD, "pending", post)
                    post.assert_not_called()

    def test_status_identity_and_outcome_fail_before_post(self):
        for repo, repository_id, head, outcome in (("invalid", 123, HEAD, "pending"),
                (REPO, True, HEAD, "pending"), (REPO, 0, HEAD, "pending"),
                (REPO, 123, HEAD.upper(), "pending"), (REPO, 123, "bad", "pending"),
                (REPO, 123, HEAD, "unknown")):
            with self.subTest(repo=repo, repository_id=repository_id, head=head, outcome=outcome):
                post = mock.Mock()
                with self.assertRaisesRegex(review.ReviewError, "invalid_status_identity"):
                    review.post_advisory_status(repo, repository_id, head, outcome, post)
                post.assert_not_called()

    def test_local_statuses_omit_run_url(self):
        for actions in (None, "false", "True"):
            with self.subTest(actions=actions), mock.patch.dict(os.environ,
                    {"GITHUB_RUN_ID": "456", "GITHUB_REPOSITORY_ID": "999"}):
                if actions is None:
                    os.environ.pop("GITHUB_ACTIONS", None)
                else:
                    os.environ["GITHUB_ACTIONS"] = actions
                for outcome in ("pending", "clear", "findings", "execution_failure"):
                    post = mock.Mock()
                    review.post_advisory_status(REPO, 123, HEAD, outcome, post)
                    self.assertNotIn("target_url", post.call_args.args[1])

    def test_common_helper_owns_every_advisory_status_post(self):
        source = ast.parse((ROOT / "scripts/pr_review.py").read_text())
        owners = [function.name for function in source.body if isinstance(function, ast.FunctionDef)
                  for node in ast.walk(function) if isinstance(node, ast.JoinedStr)
                  and any(isinstance(part, ast.Constant) and "/statuses/" in str(part.value)
                          for part in node.values)]
        self.assertEqual(owners, ["post_advisory_status"])
        helper = next(node for node in source.body if isinstance(node, ast.FunctionDef)
                      and node.name == "post_advisory_status")
        self.assertNotIn("target_url", [argument.arg for argument in helper.args.args])


class ReviewBoundaryTests(unittest.TestCase):
    def setUp(self):
        local = mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "false"})
        local.start()
        self.addCleanup(local.stop)
    def test_policy_allows_exactly_two_distinct_models(self):
        review.validate_policy(POLICY)
        for field, value in (("models", ["claude"]), ("mode", "enforce"), ("schema", 2),
                             ("private_repositories", True), ("max_review_usd", float("nan")),
                             ("max_review_usd", 4),
                             ("output_tokens", 16001), ("output_tokens", 15999.5), ("output_tokens", True),
                             ("max_chunks", 1.5), ("instructions", "x" * 8001)):
            policy = copy.deepcopy(POLICY)
            policy[field] = value
            with self.subTest(field=field), self.assertRaises(review.ReviewError):
                review.validate_policy(policy)
        for prices in ({review.MODELS[0]: POLICY["prices"][review.MODELS[0]]},
                       {**POLICY["prices"], "claude-haiku-5-5": {"input": 1, "output": 5}}):
            with self.subTest(prices=prices), self.assertRaisesRegex(review.ReviewError, "invalid_price_ceiling"):
                review.validate_policy({**POLICY, "prices": prices})
        for price in ({"input": 4}, {"input": 4, "output": 20, "cache_read": 1}, {"input": 0, "output": 20},
                      {"input": 26, "output": 20}, {"input": True, "output": 20}, {"input": "4", "output": 20},
                      {"input": 4, "output": float("inf")}):
            policy = copy.deepcopy(POLICY)
            policy["prices"][review.MODELS[0]] = price
            with self.subTest(price=price), self.assertRaisesRegex(review.ReviewError, "invalid_price_ceiling"):
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

    def test_larger_complete_records_preserve_source_and_exact_byte_boundaries(self):
        before = 'x = "\\β🙂"\r\n' * 3500
        after = "y" + before[1:]
        patch = "@@ -1 +1 @@\n-" + before.splitlines()[0] + "\n+" + after.splitlines()[0]
        snap = review.collect(fake_fetch(old=before, new=after, files=[file_change(patch=patch)]),
                              REPO, PR, POLICY_SHA, POLICY)
        size = len(review.canonical(snap["files"]))
        self.assertGreater(size, 120000)
        self.assertLessEqual(size, 180000)
        groups = review.chunks(snap, POLICY)
        self.assertEqual(groups, [snap["files"]])
        self.assertEqual(groups[0][0]["before"], before)
        self.assertEqual(groups[0][0]["after"], after)
        self.assertEqual(groups[0][0]["patch"], patch)
        self.assertEqual(review.chunks(snap, {**POLICY, "chunk_bytes": size}), groups)
        with self.assertRaisesRegex(review.ReviewError, "file_exceeds_context_budget"):
            review.chunks(snap, {**POLICY, "chunk_bytes": size - 1})
        for value in (180001, True, 180000.5):
            with self.subTest(value=value), self.assertRaises(review.ReviewError):
                review.validate_policy({**POLICY, "chunk_bytes": value})

        def boundary_record(padding, heading=""):
            text = "return True" + " " * padding + "\n"
            patch = "@@ -1 +1 @@" + heading + "\n-return False\n+" + text.rstrip("\n")
            return review.collect(fake_fetch(new=text, files=[file_change(patch=patch)]),
                                  REPO, PR, POLICY_SHA, POLICY)

        overhead = len(review.canonical(boundary_record(0)["files"]))
        padding, odd_byte = divmod(POLICY["chunk_bytes"] - overhead, 2)
        exact = boundary_record(padding, " " * odd_byte)
        self.assertEqual(len(review.canonical(exact["files"])), 180000)
        self.assertEqual(review.chunks(exact, POLICY), [exact["files"]])
        over = boundary_record(padding, " " * (odd_byte + 1))
        self.assertEqual(len(review.canonical(over["files"])), 180001)
        send = mock.Mock()
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "synthetic-fixture-key"}), \
                self.assertRaisesRegex(review.ReviewError, "file_exceeds_context_budget"):
            agent.review_snapshot(over, POLICY, send)
        send.assert_not_called()
        with self.assertRaisesRegex(review.ReviewError, "snapshot_policy_mismatch"):
            review.verify_snapshot(exact, {**POLICY, "chunk_bytes": 120000}, POLICY_SHA)

    def test_larger_multi_group_coverage_rejects_missing_reordered_or_substituted_groups(self):
        snap = snapshot()
        template = snap["files"][0]
        snap["files"] = [{**template, "path": f"file-{i}.py", "old_path": f"file-{i}.py",
                          "before": "x\n" * 20000, "after": "y\n" + "x\n" * 19999,
                          "patch": "@@ -1 +1 @@\n-x\n+y"} for i in range(3)]
        snap["context_hash"] = review.digest({key: value for key, value in snap.items() if key != "context_hash"})
        groups = review.chunks(snap, POLICY)
        self.assertEqual(len(groups), 3)
        self.assertEqual([file["path"] for group in groups for file in group],
                         [file["path"] for file in snap["files"]])
        report = model_report(snap)
        post = mock.Mock()
        review.publish(snap, report, POLICY, POLICY_SHA,
                       fake_fetch(metadata(changed_files=3)), post, True)
        for hashes in (report["reviews"][0]["chunk_hashes"][:-1],
                       report["reviews"][0]["chunk_hashes"][::-1],
                       ["0" * 64] * 3):
            damaged = copy.deepcopy(report)
            damaged["reviews"][0]["chunk_hashes"] = hashes
            post.reset_mock()
            with self.assertRaisesRegex(review.ReviewError, "review_coverage_mismatch"):
                review.publish(snap, damaged, POLICY, POLICY_SHA,
                               fake_fetch(metadata(changed_files=3)), post, True)
            post.assert_not_called()

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
        with tempfile.TemporaryDirectory() as directory:
            rules = Path(directory) / "gitleaks.toml"
            rules.write_bytes(b"fabricated rules")
            pinned = mock.patch.object(review, "GITLEAKS_RULES_SHA256", hashlib.sha256(b"fabricated rules").hexdigest())
            with mock.patch.object(review.subprocess, "run", return_value=types.SimpleNamespace(returncode=0)) as run:
                # Only the pinned Gitleaks rule file may stand in for the scan it replaces.
                with self.assertRaisesRegex(review.ReviewError, "secret_scan"):
                    review.scan_context(snapshot(), "verified-betterleaks", rules)
                run.assert_not_called()
                with pinned:
                    review.scan_context(snapshot(), "verified-betterleaks", rules)
                self.assertEqual(run.call_args.kwargs["stdout"], subprocess.DEVNULL)
                # One scan for each rule file. Without the flag, an allow comment
                # in PR source passes a credential.
                commands = [call.args[0] for call in run.call_args_list]
                self.assertEqual([Path(command[command.index("--config") + 1]) for command in commands],
                                 [rules, ROOT / ".github/pr-review-betterleaks.toml"])
                self.assertTrue(review.BETTERLEAKS_CONFIG.is_file())
                source = "\n".join(f[field] for f in snapshot()["files"] for field in ("before", "after", "patch"))
                for call in run.call_args_list:
                    self.assertEqual(call.args[0][1], "stdin")
                    # The source, then its copy without the blank lines between fields.
                    self.assertEqual(call.kwargs["input"], f"{source}\n\n{source.replace('\n\n', '\n')}".encode())
                    self.assertIn("--redact", call.args[0])
                    self.assertIn("--ignore-gitleaks-allow", call.args[0])
                encoded = {"files": [{"before": "", "after": f'blob = "{"ab" * 16}-staging"\n', "patch": ""}]}
                with pinned:
                    review.scan_context(encoded, "verified-betterleaks", rules)
                self.assertIn(b'"' + b"ab" * 16 + b"\n-staging", run.call_args.kwargs["input"])
            for codes in ((1, 0), (0, 1), (126, 0), (0, 126)):
                results = [types.SimpleNamespace(returncode=code) for code in codes]
                with pinned, mock.patch.object(review.subprocess, "run", side_effect=results):
                    with self.assertRaisesRegex(review.ReviewError, "secret_scan"):
                        review.scan_context(snapshot(), "verified-betterleaks", rules)


    def test_scan_adds_a_copy_that_betterleaks_reads_as_gitleaks_read_the_source(self):
        plain = "value = 1\n" * 10
        self.assertEqual(review.scanned_source(plain), plain)
        encoded = "ab" * 16
        self.assertEqual(review.scanned_source(f'blob = "{encoded}"\n'), f'blob = "{encoded}"\n')
        for suffix in ("-staging", "_v2"):
            source = f'blob = "{encoded}{suffix}"\n'
            self.assertEqual(review.scanned_source(source), f'{source}\n\nblob = "{encoded}\n{suffix}"\n')
        # The copies drop the source's blank lines and add their own 8,000 bytes
        # apart, so lines that take under 8,000 bytes are unbroken in one of them,
        # and no stretch is long enough for a scanner to cut a line of it.
        lines = [f"{number:098d}\n" for number in range(400)]
        source = "y" * 15_999 + "\n" + "".join(lines).replace(lines[7], lines[7] + " \t\r\n\n")
        copies = review.scanned_source(source).removeprefix(source + "\n\n")
        for start in range(len(lines) - 79):
            self.assertIn("".join(lines[start:start + 80]), copies)
        self.assertLess(max(map(len, copies.split("\n\n\n"))), review.COPY_STRETCH_BYTES + len(lines[0]))

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

    def test_message_shape_model_and_standard_rate_usage_are_required(self):
        model = review.MODELS[0]
        text = json.dumps({"review": clean_result()})
        self.assertEqual(review.validate_completion(completion(model), model), text)
        usage = completion(model)["usage"]
        for changes in ({"cache_creation_input_tokens": None, "cache_read_input_tokens": 0,
                         "speed": "standard", "service_tier": "standard", "inference_geo": "us"}, {}):
            self.assertEqual(review.validate_completion(completion(model, usage={**usage, **changes}), model), text)
        text_block = {"type": "text", "text": text}
        for changes, expected in (({"model": review.MODELS[1]}, "unexpected_model"), ({"model": None}, "unexpected_model"),
                                   ({"type": "error"}, "unexpected_model"), ({"role": "user"}, "unexpected_model"),
                                   ({"content": [{"type": "tool_use"}, text_block]}, "unexpected_completion_content"),
                                   ({"content": ["text"]}, "unexpected_completion_content"),
                                   ({"content": None}, "unexpected_completion_content"),
                                   ({"content": [text_block, text_block]}, "empty_completion"),
                                   ({"content": [{"type": "thinking"}]}, "empty_completion"),
                                   ({"content": [{"type": "text", "text": " "}]}, "empty_completion"),
                                   ({"content": [{"type": "text"}]}, "empty_completion"),
                                   ({"id": ""}, "generation_id"), ({"id": None}, "generation_id"),
                                   ({"usage": None}, "missing_usage"), ({"usage": {}}, "missing_usage"),
                                   ({"usage": {"input_tokens": True, "output_tokens": 1}}, "missing_usage"),
                                   ({"usage": {"input_tokens": 1, "output_tokens": 0}}, "missing_usage")):
            with self.subTest(changes=changes), self.assertRaisesRegex(review.ReviewError, expected):
                review.validate_completion(completion(model, **changes), model)
        # Cached, fast or priority tokens bill at other rates than the computed cost.
        for changes in ({"cache_creation_input_tokens": 5}, {"cache_read_input_tokens": 1},
                        {"cache_read_input_tokens": False}, {"speed": "fast"}, {"service_tier": "priority"},
                        {"service_tier": "batch"}):
            with self.subTest(changes=changes), self.assertRaisesRegex(review.ReviewError, "unexpected_billing_rate"):
                review.validate_completion(completion(model, usage={**usage, **changes}), model)
        for reason in ("max_tokens", "refusal", "pause_turn", "tool_use", "stop_sequence",
                       "model_context_window_exceeded", None):
            with self.subTest(reason=reason), self.assertRaisesRegex(review.ReviewError, "incomplete_completion"):
                review.validate_completion(completion(model, stop_reason=reason), model)

    def test_cost_is_priced_exactly_from_usage_at_list_prices(self):
        prices = POLICY["prices"]
        self.assertEqual(review.call_cost({"input_tokens": 100, "output_tokens": 100}, prices[review.MODELS[0]]),
                         Decimal("0.0024"))
        self.assertEqual(review.call_cost({"input_tokens": 376000, "output_tokens": 16000}, prices[review.MODELS[0]])
                         + review.call_cost({"input_tokens": 376000, "output_tokens": 16000}, prices[review.MODELS[1]]),
                         Decimal("2.736"))
        self.assertEqual(review.call_cost({"input_tokens": 1, "output_tokens": 1}, {"input": 0.1, "output": 0.2}),
                         Decimal("3E-7"))

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
        bad["reviews"][0]["generations"][0]["stop_reason"] = "max_tokens"
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
            self.assertIsNone(request.get_header("X-api-key"))
            review.request_json("https://api.github.com/test", "")
            self.assertIsNone(opener.open.call_args.args[0].get_header("Authorization"))
            review.request_json("https://api.anthropic.com/v1/messages", "fixture-key", {"x": 1}, timeout=600)
            request = opener.open.call_args.args[0]
            self.assertEqual(request.get_header("X-api-key"), "fixture-key")
            self.assertEqual(request.get_header("Anthropic-version"), "2026-10-21")
            self.assertIsNone(request.get_header("Authorization"))
            self.assertEqual(opener.open.call_args.kwargs, {"timeout": 600})
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
        for url in ("https://attacker.example", "https://openrouter.ai/api/v1/key",
                    "https://api.anthropic.com/v2/messages", "https://api.anthropic.com.attacker.example/v1/"):
            with self.subTest(url=url), mock.patch.object(review, "build_opener") as opener, \
                    self.assertRaisesRegex(review.ReviewError, "origin"):
                review.request_json(url, "fixture-key")
            opener.assert_not_called()
        for path in ("repos/../user", "repos/owner/./name", "repos/%2E%2E/user", "repos/owner/..?ref=x"):
            with (self.subTest(path=path), mock.patch.object(review, "build_opener") as opener,
                  self.assertRaisesRegex(review.ReviewError, "dot_segment_api_path")):
                review.request_json("https://api.github.com/" + path, "fixture-key")
            opener.assert_not_called()
        with self.assertRaisesRegex(review.ReviewError, "redirect"):
            review.NoRedirect().redirect_request(None, None, 302, "", {}, "https://attacker.example")

    def test_https_connections_keep_alive_with_default_verification(self):
        opener = review.build_opener(review.NoRedirect(), review.KeepAliveHTTPSHandler())
        handler = next(item for item in opener.handlers if isinstance(item, review.HTTPSHandler))
        self.assertIsInstance(handler, review.KeepAliveHTTPSHandler)
        with mock.patch.object(review.KeepAliveHTTPSHandler, "do_open", return_value="opened") as do_open:
            self.assertEqual(handler.https_open("request"), "opened")
        do_open.assert_called_once_with(review.KeepAliveConnection, "request")
        connection = review.KeepAliveConnection("api.anthropic.com")
        self.assertEqual(connection._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(connection._context.check_hostname)
        connection.sock = mock.Mock()
        with mock.patch.object(review.HTTPSConnection, "connect") as connect:
            connection.connect()
        connect.assert_called_once_with()
        self.assertEqual(connection.sock.setsockopt.call_args_list,
                         [mock.call(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
                          mock.call(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60),
                          mock.call(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 60)])

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
    def test_guard_conversion_never_rounds_below_the_decimal_reservation(self):
        for value in (Decimal("0.1"), Decimal("0.3"), Decimal("0.173"),
                      Decimal("2.99999999999999999999999999"), Decimal(3)):
            converted = agent.guard_amount(value)
            self.assertGreaterEqual(Decimal.from_float(converted), value)

    def test_payload_pins_model_price_region_and_strict_output(self):
        original = copy.deepcopy(review.REVIEW_SCHEMA)
        for model in review.MODELS:
            body = agent.request_body(model, POLICY, "system", "user")
            # No temperature (version 2026-10-21 refuses it), fallbacks, tools, caching
            # or fast mode, and global inference on standard capacity at standard prices.
            self.assertEqual(set(body), {"model", "max_tokens", "system", "messages", "thinking",
                                         "output_config", "inference_geo", "service_tier"})
            self.assertEqual(body["model"], model)
            self.assertEqual(body["max_tokens"], 16000)
            self.assertEqual(body["system"], "system")
            self.assertEqual(body["messages"], [{"role": "user", "content": "user"}])
            self.assertEqual(body["thinking"], {"type": "adaptive"})
            self.assertEqual(body["inference_geo"], "global")
            self.assertEqual(body["service_tier"], "standard_only")
            self.assertEqual(body["output_config"]["effort"], "high")
            self.assertEqual(body["output_config"]["format"]["type"], "json_schema")
        schema = agent.request_body(review.MODELS[0], POLICY, "", "")["output_config"]["format"]["schema"]
        self.assertEqual(review.REVIEW_SCHEMA, original)
        wire = review.canonical(schema).decode()
        for keyword in ("minLength", "maxLength", "maxItems", "minimum"):
            self.assertIn(f'"{keyword}"', review.canonical(original).decode())
            self.assertNotIn(f'"{keyword}"', wire)
        self.assertIn('"pattern"', wire)
        self.assertIn('"additionalProperties":false', wire)
        with self.assertRaises(review.ReviewError):
            agent.request_body("claude", POLICY, "", "")

    def test_key_missing_stops_before_imports_or_calls(self):
        for environment in ({"ANTHROPIC_API_KEY": ""}, {"OPENROUTER_API_KEY": "synthetic-fixture-key"}):
            send, builder = mock.Mock(), mock.Mock()
            with self.subTest(environment=list(environment)), mock.patch.dict(os.environ, environment), \
                    self.assertRaisesRegex(review.ReviewError, "anthropic_key_not_provisioned"):
                if not environment.get("ANTHROPIC_API_KEY"):
                    os.environ.pop("ANTHROPIC_API_KEY", None)
                agent.review_snapshot(snapshot(), POLICY, send=send, prompt_builder=builder)
            send.assert_not_called()
            builder.assert_not_called()

    def test_key_leaves_the_environment_before_engine_code_runs(self):
        prompts = {model: [("system", "context")] for model in review.MODELS}
        keys = []

        def builder(*args):
            self.assertNotIn("ANTHROPIC_API_KEY", os.environ)
            return prompts

        def send(url, key, body=None, **kwargs):
            keys.append(key)
            return counted()(url, key, body)

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "synthetic-fixture-key"}):
            agent.review_snapshot(snapshot(), POLICY, send, builder, lambda content: clean_result())
            self.assertNotIn("ANTHROPIC_API_KEY", os.environ)
        self.assertEqual(keys, ["synthetic-fixture-key"] * 4)

    def test_token_count_must_fit_the_byte_bound_before_any_paid_call(self):
        prompts = {model: [("system", "context")] for model in review.MODELS}
        body = agent.request_body(review.MODELS[0], POLICY, "system", "context")
        size = len(review.canonical(body))
        for answer in ({"input_tokens": size}, {"input_tokens": 1}):
            calls = []

            def send(url, key, payload=None, **kwargs):
                calls.append((url, payload))
                return answer if url.endswith("/count_tokens") else completion(payload["model"])

            with self.subTest(answer=answer), mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "synthetic"}):
                report = agent.review_snapshot(snapshot(), POLICY, send, lambda *args: prompts,
                                               lambda content: clean_result())
            self.assertTrue(report["complete"])
            self.assertEqual([url for url, _ in calls],
                             [review.ANTHROPIC_API + "messages/count_tokens"] * 2 + [review.ANTHROPIC_API + "messages"] * 2)
            # The count endpoint takes neither the output cap, the region nor the capacity tier.
            self.assertEqual(calls[0][1], {key: value for key, value in body.items()
                                           if key not in ("max_tokens", "inference_geo", "service_tier")})
        for answer in ({"input_tokens": size + 1}, {"input_tokens": 0}, {"input_tokens": True},
                       {"input_tokens": 1.0}, {}, [], review.ReviewError("http_500")):
            paid = []

            def refuse(url, key, payload=None, **kwargs):
                if url.endswith("/count_tokens"):
                    if isinstance(answer, Exception):
                        raise answer
                    return answer
                paid.append(payload)
                return completion(payload["model"])

            with tempfile.TemporaryDirectory() as directory, self.subTest(answer=answer), \
                    mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "synthetic"}), \
                    self.assertRaisesRegex(review.ReviewError, "unbounded_input_tokens|http_500"):
                agent.review_snapshot(snapshot(), POLICY, refuse, lambda *args: prompts,
                                      lambda content: clean_result(), receipt_path=Path(directory) / "receipts.json")
            self.assertEqual(paid, [])

    def test_independence_failures_and_cost_errors_cannot_complete(self):
        snap = snapshot()
        prompts = {model: [("system", "identical frozen context")] for model in review.MODELS}

        send = counted()

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "synthetic-fixture-key"}):
            report = agent.review_snapshot(snap, POLICY, send, lambda *args: prompts,
                                           lambda content: json.loads(content)["review"])
            self.assertTrue(report["complete"])
            self.assertEqual(report["remaining_files"], [])
            self.assertEqual([r["model"] for r in report["reviews"]], list(review.MODELS))
            # Opus at US$4/US$20 and Sonnet at US$2/US$10, each for 100 input and 100 output tokens,
            # reported as the nearest float that is not below the exact cost.
            self.assertEqual([r["generations"][0]["usage"] for r in report["reviews"]],
                             [{"input_tokens": 100, "output_tokens": 100, "cost": review.guard_amount(Decimal(cost))}
                              for cost in ("0.0024", "0.0012")])
            self.assertGreaterEqual(Decimal.from_float(report["spent_usd"]), Decimal("0.0036"))
            self.assertLess(report["spent_usd"], 0.0036000001)
        wrong = {**prompts, review.MODELS[1]: [("system", "previous model's opinion")]}
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "synthetic-fixture-key"}),                 self.assertRaisesRegex(review.ReviewError, "context_mismatch"):
            agent.review_snapshot(snap, POLICY, send, lambda *args: wrong)
        for error in (review.ReviewError("fixture_failure"), ValueError("raw-private-response")):
            with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "synthetic-fixture-key"}),                     self.assertRaisesRegex(review.ReviewError, "fixture_failure|invalid_model_output"):
                agent.review_snapshot(snap, POLICY, send, lambda *args: prompts,
                                      mock.Mock(side_effect=error))

        def expensive(url, key, body=None, **kwargs):
            obj = send(url, key, body)
            if not url.endswith("/count_tokens"):
                obj["usage"]["input_tokens"] = 10**7
            return obj
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "synthetic-fixture-key"}),                 self.assertRaisesRegex(review.ReviewError, "unexpected_token_usage"):
            agent.review_snapshot(snap, POLICY, expensive, lambda *args: prompts,
                                  lambda content: clean_result())

    def test_aggregate_reservation_exhaustion_stops_before_paid_requests(self):
        snap = snapshot()
        template = snap["files"][0]
        snap["files"] = [{**template, "path": f"file-{i}.py", "old_path": f"file-{i}.py",
                          "before": "x\n" * 20000, "after": "y\n" + "x\n" * 19999,
                          "patch": "@@ -1 +1 @@\n-x\n+y"} for i in range(3)]
        prompts = {model: [("s" * 600000, "context")] * 3 for model in review.MODELS}
        send = mock.Mock(return_value={"input_tokens": 1})
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "synthetic-fixture-key"}):
            with self.assertRaisesRegex(review.ReviewError, "review_budget_exceeded"):
                agent.review_snapshot(snap, POLICY, send, lambda *args: prompts)
        send.assert_not_called()

    def test_complete_request_context_boundary_stops_before_any_network_call(self):
        body = agent.request_body(review.MODELS[0], POLICY, "", "")
        available = agent.CONTEXT_TOKENS - POLICY["output_tokens"] - 1000 - len(review.canonical(body))
        body["messages"][0]["content"] = "s" * available
        self.assertEqual(agent.input_token_bound(body) + POLICY["output_tokens"], agent.CONTEXT_TOKENS)
        body["messages"][0]["content"] += "s"
        with self.assertRaisesRegex(review.ReviewError, "request_exceeds_context_budget"):
            agent.input_token_bound(body)
        prompts = {model: [("s" * agent.CONTEXT_TOKENS, "context")] for model in review.MODELS}
        send = mock.Mock()
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "synthetic"}), \
                self.assertRaisesRegex(review.ReviewError, "request_exceeds_context_budget"):
            agent.review_snapshot(snapshot(), POLICY, send, lambda *args: prompts)
        send.assert_not_called()

    def test_reported_token_overruns_preserve_bill_and_block_remaining_calls(self):
        prompts = {model: [("system", "context")] for model in review.MODELS}
        body = agent.request_body(review.MODELS[0], POLICY, "system", "context")
        for usage in ({"input_tokens": agent.input_token_bound(body) + 1, "output_tokens": 100},
                      {"input_tokens": 100, "output_tokens": POLICY["output_tokens"] + 1}):
            response = completion(review.MODELS[0], usage=usage)
            send = mock.Mock(side_effect=[{"input_tokens": 1}, {"input_tokens": 1}, response])
            with tempfile.TemporaryDirectory() as directory, \
                    mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "synthetic"}):
                path = Path(directory) / "receipts.json"
                with self.assertRaisesRegex(review.ReviewError, "unexpected_token_usage"):
                    agent.review_snapshot(snapshot(), POLICY, send, lambda *args: prompts, receipt_path=path)
                data = review.read_json(path)
                self.assertEqual(data["calls"][0]["diagnostic_category"], "completion_contract")
                self.assertEqual(data["calls"][0]["usage"]["cost"],
                                 review.guard_amount(review.call_cost(usage, POLICY["prices"][review.MODELS[0]])))
                self.assertEqual(data["calls"][1]["transport_state"], "not_started")
                self.assertFalse(data["finished"])
                self.assertEqual(send.call_count, 3)

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
        native_system = native_system.replace("Example output:",
            "One or two word title for the issue. For example: 'Possible Bug', etc.\nExample output:")
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
                configured.setdefault("previous_findings", []).append(self.vars["previous_findings"])
                reserve = self.ai_handler.get_output_token_reserve(model, 1000)
                configured.setdefault("reserves", []).append(reserve)
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
                self.assertIn("Title starting with [P0], [P1], [P2] or [P3]", system)
                self.assertNotIn("One or two word title", system)
                self.assertEqual(configured["reserves"], [16000, 16000])
                self.assertEqual(configured["previous_findings"], ["", ""])
                self.assertNotIn("```yaml", system)
                self.assertNotIn("Answer should be a valid YAML", system)
                user = prompts[review.MODELS[0]][0][1]
                self.assertTrue(user.startswith("Preserve complete PR evidence.\n"))
                self.assertIn("Response (must be one JSON object", user)
                self.assertNotIn("valid YAML", user)
                self.assertNotIn("```yaml", user)
                self.assertEqual(settings.pr_review_prompt.user, native_user)
                self.assertEqual(configured["config.fallback_models"], [])
                self.assertEqual(configured["config.model"], review.MODELS[1])
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
                    mock.patch.object(agent, "review_snapshot", side_effect=review.ReviewError("unbounded_input_tokens")), \
                    redirect_stderr(errors), self.assertRaises(SystemExit) as caught:
                runpy.run_path(args[0], run_name="__main__")
            self.assertEqual(caught.exception.code, 1)
            self.assertIn("unbounded_input_tokens", errors.getvalue())

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
            with mock.patch.object(review, "github", post), mock.patch.object(review, "scan_context") as scan, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(review.main(["capture", *args, "--pr", str(PR)]), 0)
                scan.assert_called_with(mock.ANY, "betterleaks", Path("gitleaks-8.30.1.toml"))
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
