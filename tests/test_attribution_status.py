from __future__ import annotations

import io
import json
import os
import runpy
import stat
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
        self.process_guard = self.enterContext(
            mock.patch.object(
                policy.subprocess, "run", side_effect=AssertionError("unexpected external process")
            )
        )
        policy.prepare(REPOSITORY, 1, self.path, self.fetch, self.post)

    def cli_args(self, operation: str = "finish") -> list[str]:
        return [
            "policy",
            operation,
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
        self.assertEqual(self.posts[-1], (HEAD, "failure"))
        self.result.write_text("not json")
        self.assertFalse(self.finish())
        self.assertEqual(self.posts[-1], (HEAD, "failure"))
        for changes in (
            {"head_sha": BASE},
            {"clean": False},
            {"clean": 1},
            {"fingerprint": "different"},
        ):
            self.proof(**changes)
            self.assertFalse(self.finish())
            self.assertEqual(self.posts[-1], (HEAD, "failure"))

    def test_failed_or_cancelled_prerequisite_never_publishes_success(self) -> None:
        self.proof()
        for status in ("failure", "cancelled", "skipped"):
            self.assertFalse(self.finish(status))
            self.assertEqual(self.posts[-1], (HEAD, "failure"))

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
            payload(number=True),
            payload(number=False),
            payload(number=1.0),
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
        for raw in (None, b"not json", b"\xff", b"[]", b"{}", b'{"head_sha":"invalid"}'):
            if raw is None:
                self.path.unlink()
            else:
                self.path.write_bytes(raw)
            count = len(self.posts)
            with self.subTest(raw=raw), self.assertRaises((OSError, ValueError)):
                self.finish()
            self.assertEqual(len(self.posts), count)

    def test_cli_reports_only_policy_outcome_and_closes_failed_scan(self) -> None:
        args = self.cli_args()
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
        original = self.path.read_bytes()
        with (
            mock.patch.object(sys, "argv", args),
            mock.patch.object(policy, "gh_json", side_effect=self.fetch) as fetch,
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(policy.main(), 1)  # Existing snapshot refuses overwrite.
        fetch.assert_called_once_with(f"repos/{REPOSITORY}/pulls/1")
        self.assertEqual(self.path.read_bytes(), original)
        self.process_guard.assert_not_called()
        args[args.index(REPOSITORY)] = "invalid"
        with mock.patch.object(sys, "argv", args), redirect_stderr(io.StringIO()):
            self.assertEqual(policy.main(), 1)

    def test_failed_and_unreadable_api_responses_never_pass(self) -> None:
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

    def test_cli_redacts_failed_status_delivery(self) -> None:
        args = self.cli_args()
        self.proof()
        output = io.StringIO()
        with (
            mock.patch.object(sys, "argv", args),
            mock.patch.object(policy, "gh_json", self.fetch),
            mock.patch.object(
                policy.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    [], 1, b"private status output", b"private status error"
                ),
            ),
            redirect_stderr(output),
        ):
            self.assertEqual(policy.main(), 1)
        self.assertEqual(output.getvalue(), "Attribution status validation could not complete\n")

    def test_cli_redacts_failed_or_malformed_api_reads(self) -> None:
        for response in (
            subprocess.CompletedProcess([], 1, b"private API output", b"private API error"),
            subprocess.CompletedProcess([], 0, b"private malformed API content", b""),
        ):
            output = io.StringIO()
            with (
                self.subTest(response=response),
                mock.patch.object(sys, "argv", self.cli_args("prepare")),
                mock.patch.object(policy.subprocess, "run", return_value=response),
                redirect_stderr(output),
            ):
                self.assertEqual(policy.main(), 1)
            self.assertEqual(
                output.getvalue(), "Attribution status validation could not complete\n"
            )

    def test_cli_entry_point_finishes_a_clean_scan(self) -> None:
        self.proof()
        with (
            mock.patch.object(sys, "argv", self.cli_args()),
            mock.patch.object(
                policy.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, json.dumps(payload()).encode()),
            ),
            self.assertRaisesRegex(SystemExit, "0"),
        ):
            runpy.run_path(str(Path(policy.__file__)), run_name="__main__")

    @unittest.skipUnless(os.name == "posix", "POSIX file modes and symlinks")
    def test_snapshot_creation_is_private_and_refuses_final_symlinks(self) -> None:
        private = self.path.with_name("private.json")
        previous_umask = os.umask(0)
        try:
            policy.prepare(REPOSITORY, 1, private, self.fetch, self.post)
        finally:
            os.umask(previous_umask)
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o600)
        original = self.path.read_bytes()
        before = list(self.posts)
        for target in (self.path, self.path.with_name("missing.json")):
            link = self.path.with_name("link.json")
            link.symlink_to(target)
            with self.assertRaises(FileExistsError):
                policy.prepare(REPOSITORY, 1, link, self.fetch, self.post)
            self.assertEqual(self.posts, before)
            self.assertEqual(self.path.read_bytes(), original)
            self.assertFalse(self.path.with_name("missing.json").exists())
            link.unlink()

    def test_surviving_old_event_reads_current_metadata(self) -> None:
        self.path.unlink()
        live = payload(body="Current metadata")
        policy.prepare(REPOSITORY, 1, self.path, lambda _: live, self.post)
        self.assertEqual(json.loads(self.path.read_text())["body"], "Current metadata")


if __name__ == "__main__":
    unittest.main()
