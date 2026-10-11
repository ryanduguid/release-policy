"""Fabricated server records exercise the temporary caller's pre-secret admission."""

from __future__ import annotations

import base64
import copy
import importlib
import io
import runpy
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_pr_review import ROOT

gate = importlib.import_module("pr_review_qualification_gate")
CALLER = "f" * 40


class QualificationGateTests(unittest.TestCase):
    def setUp(self):
        self.event = {"inputs": {"pr-number": "141", "activation-id": gate.ACTIVATION,
                                "caller-sha": CALLER, "intent-start": "2026-10-11T10:00:00Z",
                                "intent-end": "2026-10-11T10:02:00Z"}}
        self.environment = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": gate.REPO,
            "GITHUB_REPOSITORY_ID": str(gate.REPO_ID), "GITHUB_ACTOR": "ryanduguid",
            "GITHUB_ACTOR_ID": str(gate.ACTOR_ID), "GITHUB_RUN_ATTEMPT": "1", "GITHUB_RUN_ID": "99",
            "GITHUB_REF": "refs/heads/" + gate.BRANCH, "GITHUB_SHA": CALLER,
            "GITHUB_WORKFLOW_SHA": CALLER,
            "GITHUB_WORKFLOW_REF": gate.REPO + "/" + gate.WORKFLOW + "@refs/heads/" + gate.BRANCH}
        self.workflow = (ROOT / gate.WORKFLOW).read_bytes().replace(b"\r\n", b"\n")
        self.run = {"id": 99, "workflow_id": gate.WORKFLOW_ID, "event": "workflow_dispatch",
            "path": gate.WORKFLOW, "head_sha": CALLER, "head_branch": gate.BRANCH, "run_attempt": 1,
            "created_at": "2026-10-11T10:00:30Z", "status": "in_progress", "conclusion": None,
            "actor": {"login": "ryanduguid", "id": gate.ACTOR_ID},
            "triggering_actor": {"login": "ryanduguid", "id": gate.ACTOR_ID},
            "repository": {"full_name": gate.REPO, "id": gate.REPO_ID, "private": False}, "head_repository": {"id": gate.REPO_ID, "full_name": gate.REPO},
            "pull_requests": [], "referenced_workflows": []}
        self.previous = [{**{key: self.run[key] for key in gate.HISTORY_KEYS}, "id": 10,
                          "head_sha": "d" * 40, "head_branch": "main", "status": "completed",
                          "conclusion": "failure", "created_at": "2026-10-04T10:00:00Z"}]
        patch = mock.patch.object(gate, "BASELINE_HASH", gate.review.digest(gate.history(self.previous)))
        patch.start()
        self.addCleanup(patch.stop)
        self.ordinary = []
        ordinary_patch = mock.patch.object(gate, "ORDINARY_HASH", gate.review.digest([]))
        ordinary_patch.start()
        self.addCleanup(ordinary_patch.stop)
        clock = mock.patch.object(gate, "datetime", wraps=datetime)
        clock.start().now.return_value = datetime(2026, 10, 11, 10, 9, tzinfo=timezone.utc)
        self.addCleanup(clock.stop)
        self.pr = {"state": "open", "draft": False,
                   "head": {"sha": gate.RUNTIME, "repo": {"id": gate.REPO_ID}},
                   "base": {"sha": gate.MAIN, "ref": "main", "repo": {
                       "id": gate.REPO_ID, "full_name": gate.REPO, "private": False}}}
        self.consumer = {"id": 373307813, "path": ".github/workflows/pr-review-consumer.yml",
                         "state": "disabled_manually"}
        self.central = {"id": gate.WORKFLOW_ID, "path": gate.WORKFLOW, "state": "active"}
        self.main = gate.MAIN
        self.branch = CALLER
        self.parents = [gate.RUNTIME]
        self.extra = []
        self.calls = []

    def fetch(self, path):
        self.calls.append(path)
        prefix = "repos/" + gate.REPO
        if path == prefix + "/actions/runs/99":
            return copy.deepcopy(self.run)
        if path == prefix + "/pulls/141":
            return copy.deepcopy(self.pr)
        if path == prefix + "/git/ref/heads/main":
            return {"object": {"sha": self.main}}
        if path == prefix + "/git/ref/heads/" + gate.BRANCH:
            return {"object": {"sha": self.branch}}
        if path == prefix + "/actions/workflows/373307813":
            return copy.deepcopy(self.consumer)
        if path == prefix + f"/actions/workflows/{gate.WORKFLOW_ID}":
            return copy.deepcopy(self.central)
        if "/actions/runs?head_sha=" in path:
            return {"workflow_runs": copy.deepcopy(self.ordinary), "total_count": len(self.ordinary)}
        if "/runs?" in path:
            workflow_id = int(path.split("/workflows/", 1)[1].split("/", 1)[0])
            rows = [row for row in [*self.previous, self.run, *self.extra] if row["workflow_id"] == workflow_id]
            return {"workflow_runs": copy.deepcopy(rows), "total_count": len(rows)}
        if path == prefix + "/git/commits/" + CALLER:
            return {"sha": CALLER, "parents": [{"sha": sha} for sha in self.parents]}
        if path == prefix + "/contents/" + gate.WORKFLOW + "?ref=" + CALLER:
            return {"type": "file", "encoding": "base64", "size": len(self.workflow),
                    "content": base64.b64encode(self.workflow).decode()}
        raise AssertionError("Unexpected fabricated request: " + path)

    def admit(self):
        return gate.admit(self.event, self.environment, self.fetch, self.workflow,
                          now=datetime(2026, 10, 11, 10, 9, tzinfo=timezone.utc))

    def test_exact_created_run_is_admitted_even_if_runner_starts_later(self):
        result = self.admit()
        self.assertTrue(result["passed"])
        self.assertEqual(result["run_id"], 99)
        self.assertEqual(result["caller_sha"], CALLER)
        self.assertEqual(result["runtime_sha"], gate.RUNTIME)
        self.assertEqual(result["baseline_paid_sha256"], gate.review.digest(gate.history(self.previous)))
        self.assertEqual(self.calls.count("repos/" + gate.REPO + "/actions/runs/99"), 2)

    def test_invalid_inputs_and_context_fail_before_rest(self):
        for field, value in (("pr-number", "142"), ("activation-id", "other"), ("caller-sha", "bad"),
                             ("intent-end", "2026-10-11T10:02:01Z"),
                             ("intent-start", "2026-10-11T10:00:00.1Z"),
                             ("intent-end", "2026-10-11T10:00:00Z")):
            with self.subTest(field=field), mock.patch.dict(self.event["inputs"], {field: value}):
                self.calls.clear()
                with self.assertRaises(gate.review.ReviewError):
                    self.admit()
                self.assertEqual(self.calls, [])
        for field in self.environment:
            with self.subTest(field=field), mock.patch.dict(self.environment, {field: "wrong"}):
                self.calls.clear()
                with self.assertRaises(gate.review.ReviewError):
                    self.admit()
                self.assertEqual(self.calls, [])

    def test_run_and_authority_mismatches_fail_before_admission(self):
        for field, value in (("id", 98), ("path", "other.yml"), ("workflow_id", 373307813),
                             ("event", "push"), ("head_sha", "a" * 40), ("head_branch", "main"),
                             ("run_attempt", 2), ("run_attempt", True), ("status", "completed"),
                             ("conclusion", "failure"), ("created_at", "2026-10-11T09:59:59Z"),
                             ("created_at", "2026-10-11T10:02:01Z")):
            with self.subTest(field=field, value=value), mock.patch.dict(self.run, {field: value}):
                with self.assertRaises(gate.review.ReviewError):
                    self.admit()
        for key in ("actor", "triggering_actor"):
            with self.subTest(key=key), mock.patch.dict(self.run[key], {"id": 1}):
                with self.assertRaises(gate.review.ReviewError):
                    self.admit()
        with mock.patch.dict(self.run["repository"], {"private": True}):
            with self.assertRaises(gate.review.ReviewError):
                self.admit()

    def test_source_workflow_and_parent_changes_fail(self):
        for owner, field, value in ((self.pr, "draft", True), (self.pr, "state", "closed"),
                                  (self.pr["head"], "sha", "a" * 40), (self.pr["base"], "sha", "b" * 40),
                                  (self.consumer, "state", "active"), (self.central, "state", "disabled_manually")):
            with self.subTest(field=field), mock.patch.dict(owner, {field: value}):
                with self.assertRaises(gate.review.ReviewError):
                    self.admit()
        for field, value in (("main", "b" * 40), ("branch", "a" * 40),
                             ("parents", [gate.MAIN]), ("parents", [gate.RUNTIME, gate.MAIN]),
                             ("workflow", self.workflow.replace(gate.RUNTIME.encode(), b"a" * 40))):
            with self.subTest(field=field), mock.patch.object(self, field, value):
                with self.assertRaises(gate.review.ReviewError):
                    self.admit()

    def test_history_changes_and_second_runs_fail(self):
        with mock.patch.dict(self.previous[0], {"run_attempt": 2}):
            with self.assertRaises(gate.review.ReviewError):
                self.admit()
        self.extra = [{**self.run, "id": 100}]
        with self.assertRaises(gate.review.ReviewError):
            self.admit()
        self.extra = []
        self.calls.clear()
        original = self.fetch
        def changing(path):
            if len([call for call in self.calls if call.endswith("/actions/runs/99")]) == 2:
                self.extra = [{**self.run, "id": 100}]
            return original(path)
        with self.assertRaises(gate.review.ReviewError):
            gate.admit(self.event, self.environment, changing, self.workflow,
                       now=datetime(2026, 10, 11, 10, 9, tzinfo=timezone.utc))

    def test_caller_has_one_manual_installed_path_after_read_only_admission(self):
        text = self.workflow.decode()
        self.assertIn("  workflow_dispatch:\n", text)
        self.assertNotIn("pull_request_target:", text)
        self.assertNotIn("workflow_run:", text)
        self.assertNotIn("central_public_report_v1", text)
        self.assertNotIn("pr-url", text)
        self.assertIn("cancel-in-progress: false", text)
        admission, paid = text.split("\n  review:\n", 1)
        self.assertNotIn("OPENROUTER_API_KEY", admission)
        self.assertNotIn("statuses: write", admission)
        self.assertIn("    needs: admit\n", paid)
        self.assertIn("if: ${{ needs.admit.result == 'success' }}", paid)
        self.assertIn("mode: installed", paid)
        self.assertIn("pr-review.yml@" + gate.RUNTIME, paid)
        self.assertIn("qualification-admission-${{ github.run_id }}-${{ github.run_attempt }}", admission)
        self.assertEqual(text.count("OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}"), 1)

    def test_inventory_pagination_is_complete_bounded_and_validates_rows(self):
        rows = [{"id": index + 1, "workflow_id": gate.PAID_IDS[0]} for index in range(100)]
        def pages(path):
            selected = str(gate.PAID_IDS[0]) in path
            return {"workflow_runs": rows if selected and path.endswith("page=1") else [],
                    "total_count": 100 if selected else 0}
        self.assertEqual(len(gate.paid_runs(pages)), 100)
        with self.assertRaisesRegex(gate.review.ReviewError, "incomplete_paid_inventory"):
            gate.paid_runs(lambda path: {"workflow_runs": rows, "total_count": 10000})
        for batch, total in (([], True), (rows * 2, 200), ([rows[0]], 2),
                             ([{"id": 1, "workflow_id": 0}], 1),
                             ([{"id": True, "workflow_id": gate.PAID_IDS[0]}], 1),
                             ([rows[0], rows[0]], 2)):
            with self.subTest(batch=batch, total=total), self.assertRaises(gate.review.ReviewError):
                gate.paid_runs(lambda path: {"workflow_runs": batch, "total_count": total}
                              if str(gate.PAID_IDS[0]) in path else {"workflow_runs": [], "total_count": 0})

    def test_cli_retains_admission_and_sanitises_failure_without_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "qualification-admission.json"
            event = root / "event.json"
            gate.review.write_json(event, self.event)
            environment = {**self.environment, "GITHUB_EVENT_PATH": str(event)}
            with mock.patch.dict(gate.os.environ, environment, clear=True), \
                    mock.patch.object(gate.argparse.ArgumentParser, "parse_args",
                                      return_value=SimpleNamespace(output=output)), \
                    mock.patch.object(gate, "github", self.fetch):
                self.assertEqual(gate.main(), 0)
                self.assertEqual(gate.review.read_json(output)["run_id"], 99)
                with mock.patch.object(gate.review, "write_json", side_effect=OSError("private")), \
                        redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(gate.main(), 1)
                    self.assertEqual(error.getvalue(), "Qualification admission failed\n")
            self.event["inputs"]["activation-id"] = "wrong"
            gate.review.write_json(event, self.event)
            with mock.patch.dict(gate.os.environ, environment, clear=True), \
                    mock.patch.object(sys, "argv", [str(ROOT / "scripts/pr_review_qualification_gate.py"),
                                                  "--output", str(root / "rejected.json")]), \
                    mock.patch.object(gate, "github") as fetch, redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as stopped:
                runpy.run_path(str(ROOT / "scripts/pr_review_qualification_gate.py"), run_name="__main__")
            self.assertEqual(stopped.exception.code, 1)
            fetch.assert_not_called()
            self.assertFalse((root / "rejected.json").exists())

    def test_history_binds_nested_identities_and_normalises_order_and_absence(self):
        sample = copy.deepcopy(self.run)
        repo = {"id": gate.REPO_ID, "name": "release-policy",
                "url": "https://api.github.com/repos/" + gate.REPO}
        sample["pull_requests"] = [{"id": 20, "number": 141,
                                   "head": {"ref": "migration", "sha": gate.RUNTIME, "repo": repo},
                                   "base": {"ref": "main", "sha": gate.MAIN, "repo": repo}}]
        sample["referenced_workflows"] = [{"path": gate.REPO + "/review.yml@" + gate.RUNTIME,
                                           "sha": gate.RUNTIME}]
        original = gate.history([sample])
        explicit = copy.deepcopy(sample)
        explicit["referenced_workflows"][0]["ref"] = None
        self.assertEqual(gate.history([explicit]), original)
        for path, value in ((["actor", "id"], 42), (["actor", "login"], "other"),
                            (["triggering_actor", "id"], 42), (["triggering_actor", "login"], "other"),
                            (["repository", "id"], 42), (["repository", "full_name"], "other/repo"),
                            (["head_repository", "id"], 42), (["head_repository", "full_name"], "other/repo"),
                            (["pull_requests", 0, "id"], 42), (["pull_requests", 0, "number"], 142),
                            (["pull_requests", 0, "head", "ref"], "other"),
                            (["pull_requests", 0, "base", "sha"], "a" * 40),
                            (["pull_requests", 0, "head", "repo", "id"], 42),
                            (["referenced_workflows", 0, "path"], "other/review.yml"),
                            (["referenced_workflows", 0, "ref"], "refs/heads/other"),
                            (["referenced_workflows", 0, "sha"], "a" * 40)):
            changed = copy.deepcopy(sample)
            target = changed
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            with self.subTest(path=path):
                self.assertNotEqual(gate.history([changed]), original)
        second = copy.deepcopy(sample)
        second["id"] = 100
        self.assertEqual(gate.history([second, sample]), gate.history([sample, second]))
        nullable = copy.deepcopy(sample)
        nullable["head_repository"] = None
        nullable["pull_requests"][0]["head"]["repo"] = None
        self.assertIsNone(gate.history([nullable])[0]["head_repository"])

    def test_history_rejects_malformed_and_duplicate_identity_fields(self):
        invalid = []
        for key, value in (("id", True), ("run_attempt", 0), ("event", ""), ("head_sha", 1),
                           ("head_sha", "bad"), ("conclusion", False), ("conclusion", ""),
                           ("repository", None), ("repository", "bad"),
                           ("pull_requests", None), ("referenced_workflows", None)):
            invalid.append(dict(self.run, **{key: value}))
        for key in ("actor", "triggering_actor", "repository", "head_repository"):
            changed = copy.deepcopy(self.run)
            changed[key]["id"] = True
            invalid.append(changed)
        invalid.append(dict(self.run, repository={"id": 42, "full_name": "bad"}))
        repo = {"id": 42, "name": "release-policy", "url": "https://api.github.com/repos/" + gate.REPO}
        association = {"id": 20, "number": 141,
                       "head": {"ref": "branch", "sha": gate.RUNTIME, "repo": repo},
                       "base": {"ref": "main", "sha": gate.MAIN, "repo": repo}}
        invalid.append(dict(self.run, pull_requests=[association, association]))
        for key, value in (("url", "https://elsewhere/repos/" + gate.REPO), ("name", "other")):
            changed = copy.deepcopy(association)
            changed["head"]["repo"][key] = value
            invalid.append(dict(self.run, pull_requests=[changed]))
        reference = {"path": "review.yml", "sha": gate.RUNTIME}
        invalid.extend((dict(self.run, referenced_workflows=[dict(reference, ref=42)]),
                        dict(self.run, referenced_workflows=[reference, reference])))
        for sample in invalid:
            with self.subTest(sample=sample), self.assertRaises((gate.review.ReviewError, KeyError, TypeError)):
                gate.history([sample])
        with self.assertRaises(gate.review.ReviewError):
            gate.history([self.run, self.run])
        with self.assertRaises(gate.review.ReviewError):
            gate.history(None)

    def test_date_and_full_ordinary_history_are_admission_conditions(self):
        for now in (datetime(2026, 10, 4, tzinfo=timezone.utc), datetime(2026, 10, 6, tzinfo=timezone.utc)):
            self.calls.clear()
            with self.assertRaises(gate.review.ReviewError):
                gate.admit(self.event, self.environment, self.fetch, self.workflow, now=now)
            self.assertEqual(self.calls, [])
        ordinary = copy.deepcopy(self.previous[0])
        ordinary["head_sha"] = gate.RUNTIME
        self.ordinary = [ordinary]
        with mock.patch.object(gate, "ORDINARY_HASH", gate.review.digest(gate.history(self.ordinary))):
            self.admit()
            self.ordinary[0]["triggering_actor"]["login"] = "other"
            with self.assertRaisesRegex(gate.review.ReviewError, "ordinary_qualification_history_changed"):
                self.admit()

    def test_fetch_pins_origin_and_version_and_rejects_ambiguous_json(self):
        with mock.patch.dict(gate.os.environ, {"GH_HOST": "other.example"}), \
                mock.patch.object(gate.subprocess, "check_output", return_value=b'{"safe":true}') as send:
            self.assertEqual(gate.github("repos/" + gate.REPO + "/pulls/141"), {"safe": True})
            self.assertEqual(send.call_args.args[0][3:5], ["github.com", "-H"])
            self.assertIn("X-GitHub-Api-Version: 2026-03-10", send.call_args.args[0])
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{}{}'):
            with mock.patch.object(gate.subprocess, "check_output", return_value=raw), \
                    self.assertRaises((gate.review.ReviewError, ValueError)):
                gate.github("repos/" + gate.REPO + "/pulls/141")
        with self.assertRaises(gate.review.ReviewError):
            gate.github("repos/other/repo/pulls/141")
        with mock.patch.object(gate.subprocess, "check_output", return_value=b'{}'), \
                mock.patch.object(gate.review, "MAX_RESPONSE_BYTES", 1), \
                self.assertRaises(gate.review.ReviewError):
            gate.github("repos/" + gate.REPO + "/pulls/141")

    def test_ordinary_pages_require_complete_bounded_inventory(self):
        rows = [dict(self.previous[0], id=index + 200, head_sha=gate.RUNTIME) for index in range(100)]
        original = self.fetch
        def pages(path):
            if "/actions/runs?head_sha=" in path:
                return {"workflow_runs": rows if path.endswith("page=1") else [], "total_count": 100}
            return original(path)
        now = datetime(2026, 10, 11, 10, 9, tzinfo=timezone.utc)
        with mock.patch.object(gate, "ORDINARY_HASH", gate.review.digest(gate.history(rows))):
            gate.admit(self.event, self.environment, pages, self.workflow, now=now)
        def unbounded(path):
            return {"workflow_runs": rows, "total_count": 10000} if "/actions/runs?head_sha=" in path else original(path)
        with self.assertRaisesRegex(gate.review.ReviewError, "incomplete_ordinary_inventory"):
            gate.admit(self.event, self.environment, unbounded, self.workflow, now=now)


if __name__ == "__main__":
    unittest.main()
