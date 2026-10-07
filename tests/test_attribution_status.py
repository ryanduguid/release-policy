from __future__ import annotations

import io
import json
import runpy
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from scripts import attribution_status as policy

REPOSITORY = "example/policy"
HEAD = "a" * 40
BASE = "b" * 40


def payload(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "number": 1,
        "title": "Clean title",
        "body": None,
        "head": {"sha": HEAD, "repo": {"full_name": "example/fork"}},
        "base": {"sha": BASE, "ref": "main", "repo": {"full_name": REPOSITORY}},
    }
    value.update(changes)
    return value


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "snapshot.json"
        self.result = self.path.with_name("result.json")
        self.posts: list[tuple[str, str]] = []
        self.fetch = lambda _: payload()
        self.post = lambda head, state: self.posts.append((head, state))
        policy.prepare(REPOSITORY, 1, self.path, self.fetch, self.post)

    def proof(self, **changes: object) -> None:
        value = json.loads(self.path.read_text())
        result = {"head_sha": HEAD, "clean": True, "fingerprint": policy.fingerprint(value)}
        result.update(changes)
        self.result.write_text(json.dumps(result))

    def finish(self, status: str = "success", fetch=None) -> bool:
        return policy.finish(
            REPOSITORY, 1, self.path, self.result, status, fetch or self.fetch, self.post
        )

    def test_completed_clean_scan_is_bound_to_the_current_snapshot(self) -> None:
        self.proof()
        self.assertTrue(self.finish())
        self.assertEqual(self.posts, [(HEAD, "pending"), (HEAD, "success")])

    def test_same_head_metadata_change_and_changed_revisions_fail(self) -> None:
        self.proof()
        for changed in (
            payload(title="Changed title"),
            payload(body="Changed body"),
            payload(head={"sha": "c" * 40, "repo": {"full_name": "example/fork"}}),
            payload(base={"sha": "c" * 40, "ref": "main", "repo": {"full_name": REPOSITORY}}),
            payload(base={"sha": BASE, "ref": "other", "repo": {"full_name": REPOSITORY}}),
        ):
            with self.subTest(changed=changed):
                self.assertFalse(self.finish(fetch=lambda _: changed))
                self.assertEqual(self.posts[-1], (HEAD, "failure"))

    def test_missing_malformed_or_mismatched_proof_fails(self) -> None:
        self.assertFalse(self.finish())
        self.result.write_text("not json")
        self.assertFalse(self.finish())
        for changes in (
            {"head_sha": BASE},
            {"clean": False},
            {"clean": 1},
            {"fingerprint": "different"},
        ):
            self.proof(**changes)
            self.assertFalse(self.finish())

    def test_failed_or_cancelled_prerequisite_never_publishes_success(self) -> None:
        self.proof()
        for status in ("failure", "cancelled", "skipped"):
            self.assertFalse(self.finish(status))

    def test_api_read_failure_closes_owned_pending(self) -> None:
        self.proof()

        def unavailable(_):
            raise RuntimeError("unavailable")

        self.assertFalse(self.finish(fetch=unavailable))
        self.assertEqual(self.posts[-1], (HEAD, "failure"))

    def test_unreadable_identity_is_rejected_before_pending(self) -> None:
        for bad in (
            None,
            {},
            payload(number=2),
            payload(body=1),
            payload(title=None),
            payload(head={"sha": "bad", "repo": {"full_name": "example/fork"}}),
            payload(base={"sha": BASE, "ref": "main", "repo": {"full_name": "wrong/repo"}}),
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                policy.snapshot(bad, REPOSITORY, 1)

    def test_revision_and_metadata_shapes_fail_closed(self) -> None:
        for side in ("head", "base"):
            for bad in (
                None,
                {},
                {"sha": HEAD, "repo": {}},
                {"sha": HEAD, "repo": {"full_name": "invalid"}},
            ):
                with self.subTest(side=side, bad=bad), self.assertRaises(ValueError):
                    policy.snapshot(payload(**{side: bad}), REPOSITORY, 1)
        for base_ref in (None, ""):
            value = payload(base={"sha": BASE, "ref": base_ref, "repo": {"full_name": REPOSITORY}})
            with self.assertRaises(ValueError):
                policy.snapshot(value, REPOSITORY, 1)

    def test_unreadable_owned_snapshot_cannot_write_an_arbitrary_status(self) -> None:
        for text in ("[]", '{"head_sha":"invalid"}'):
            self.path.write_text(text)
            count = len(self.posts)
            with self.assertRaises(ValueError):
                self.finish()
            self.assertEqual(len(self.posts), count)

    def test_cli_reports_only_policy_outcome_and_closes_failed_scan(self) -> None:
        args = [
            "policy",
            "finish",
            "--repository",
            REPOSITORY,
            "--number",
            "1",
            "--snapshot",
            str(self.path),
            "--result",
            str(self.result),
            "--job-status",
            "success",
            "--run-url",
            "https://github.com/example/policy/actions/runs/1",
        ]
        with (
            mock.patch.object(sys, "argv", args),
            mock.patch.object(policy, "gh_json", self.fetch),
            mock.patch.object(
                policy.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
            ),
        ):
            self.proof()
            self.assertEqual(policy.main(), 0)
            self.result.unlink()
            with redirect_stderr(io.StringIO()):
                self.assertEqual(policy.main(), 1)
            self.path.unlink()
            args[1] = "prepare"
            self.assertEqual(policy.main(), 0)
        with mock.patch.object(sys, "argv", args), redirect_stderr(io.StringIO()):
            self.assertEqual(policy.main(), 1)  # Existing snapshot refuses overwrite.
        args[args.index(REPOSITORY)] = "invalid"
        with mock.patch.object(sys, "argv", args), redirect_stderr(io.StringIO()):
            self.assertEqual(policy.main(), 1)

    def test_failed_status_delivery_and_unreadable_api_never_pass(self) -> None:
        for response in (
            subprocess.CompletedProcess([], 1, b"", b"private error"),
            subprocess.CompletedProcess([], 0, b"not json", b""),
        ):
            with (
                mock.patch.object(policy.subprocess, "run", return_value=response),
                self.assertRaises(RuntimeError),
            ):
                policy.gh_json("repos/example/policy/pulls/1")
        response = subprocess.CompletedProcess([], 0, b'{"number":1}', b"")
        with mock.patch.object(policy.subprocess, "run", return_value=response):
            self.assertEqual(policy.gh_json("endpoint"), {"number": 1})
        args = [
            "policy",
            "finish",
            "--repository",
            REPOSITORY,
            "--number",
            "1",
            "--snapshot",
            str(self.path),
            "--result",
            str(self.result),
            "--job-status",
            "success",
            "--run-url",
            "https://github.com/example/policy/actions/runs/1",
        ]
        self.proof()
        output = io.StringIO()
        with (
            mock.patch.object(sys, "argv", args),
            mock.patch.object(policy, "gh_json", self.fetch),
            mock.patch.object(
                policy.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)
            ),
            redirect_stderr(output),
        ):
            self.assertEqual(policy.main(), 1)
        self.assertNotIn("private error", output.getvalue())
        with (
            mock.patch.object(sys, "argv", args),
            mock.patch.object(policy, "gh_json", self.fetch),
            mock.patch.object(
                policy.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, json.dumps(payload()).encode()),
            ),
            self.assertRaisesRegex(SystemExit, "0"),
        ):
            runpy.run_path(str(Path(policy.__file__)), run_name="__main__")

    def test_surviving_old_event_reads_current_metadata(self) -> None:
        self.path.unlink()
        live = payload(body="Current metadata")
        policy.prepare(REPOSITORY, 1, self.path, lambda _: live, self.post)
        self.assertEqual(json.loads(self.path.read_text())["body"], "Current metadata")


if __name__ == "__main__":
    unittest.main()
