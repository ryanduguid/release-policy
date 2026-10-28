"""The repin workflow files the attribution repin patch, and only when it is owed.

These checks build the pin, the action and the baseline digest in a throwaway
repository, and stand in for `gh`, so they need no clone history or network.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import runpy
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Sequence
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import repin_issue  # noqa: E402

ZERO = "0" * 40


def workflow(pin: str) -> str:
    return (
        "jobs:\n  policy:\n    steps:\n"
        "      - uses: actions/checkout@" + "a" * 40 + " # v7.0.1\n"
        f"      - uses: ryanduguid/release-policy/.github/actions/no-ai-attribution@{pin}\n"
        "      # - uses: ryanduguid/release-policy/.github/actions/no-ai-attribution@"
        + "1" * 40 + "\n"
    )


def baseline(digest: str) -> str:
    return (
        "CANONICAL_SHA256 = {\n"
        f'    ".github/workflows/attribution-policy.yml": "{digest}",\n'
        '    ".github/workflows/ci.yml": "' + "c" * 64 + '",\n'
        "}\n"
    )


class FakeGh:
    """Answers `gh api` calls from canned issue pages and records each call."""

    def __init__(self, pages: list[object]) -> None:
        self.pages = pages
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, arguments: Sequence[str], stdin: str | None) -> str:
        self.calls.append((list(arguments), stdin))
        if "--method" not in arguments:
            page = int(re.search(r"page=([0-9]+)$", arguments[-1]).group(1))  # type: ignore[union-attr]
            return json.dumps(self.pages[page - 1])
        return json.dumps({"number": 7})


def issue(number: int, title: str, login: str = repin_issue.BOT, body: str = "old") -> dict[str, object]:
    return {"number": number, "title": title, "user": {"login": login}, "body": body}


class DigestTests(unittest.TestCase):
    def test_the_digest_matches_the_baseline_entry_for_the_live_workflow(self) -> None:
        document = (ROOT / repin_issue.WORKFLOW).read_bytes()
        text = (ROOT / repin_issue.BASELINE).read_text(encoding="utf-8")
        recorded = repin_issue._single(repin_issue._DIGEST_LINE, text, "digest").group("digest")
        self.assertEqual(repin_issue.canonical_digest(document), recorded)

    def test_third_party_pins_hash_as_their_major_and_own_pins_stay_exact(self) -> None:
        third = b"  - uses: actions/checkout@" + b"a" * 40 + b" # v7.0.1\r\n"
        self.assertEqual(
            repin_issue.canonical_digest(third),
            hashlib.sha256(b"  - uses: actions/checkout@" + b"0" * 40 + b" # v7\n").hexdigest(),
        )
        own = b"  - uses: ryanduguid/release-policy/x@" + b"a" * 40 + b" # v7.0.1\n"
        self.assertEqual(repin_issue.canonical_digest(own), hashlib.sha256(own).hexdigest())


class PrepareTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "repo"
        self.root.mkdir()
        self.patch = Path(directory.name) / "repin.patch"
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.test")
        self.git("config", "user.name", "Fixture")
        self.git("config", "core.autocrlf", "false")
        # The first commit pins a placeholder no checkout can read.
        self.first = self.commit("runs: scan v1\n", ZERO)
        self.current = self.commit("runs: scan v1\n", self.first)

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True,
                              check=True).stdout.strip()

    def commit(self, action: str, pin: str) -> str:
        files = {
            repin_issue.ACTION: action,
            repin_issue.WORKFLOW: workflow(pin),
            repin_issue.BASELINE: baseline(repin_issue.canonical_digest(workflow(pin).encode())),
        }
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode("utf-8"))
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "fixture")
        return self.git("rev-parse", "HEAD")

    def test_a_current_pin_writes_nothing(self) -> None:
        self.assertIsNone(repin_issue.prepare(self.root, self.current, self.patch))
        self.assertFalse(self.patch.exists())
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_a_changed_action_is_repinned_with_its_digest_and_the_patch_applies(self) -> None:
        target = self.commit("runs: scan v2\n", self.first)
        self.assertEqual(repin_issue.prepare(self.root, target, self.patch), self.first)
        repinned = (self.root / repin_issue.WORKFLOW).read_text(encoding="utf-8")
        self.assertEqual(repinned, workflow(target))
        self.assertEqual(
            (self.root / repin_issue.BASELINE).read_text(encoding="utf-8"),
            baseline(repin_issue.canonical_digest(workflow(target).encode())),
        )
        # The commented pin is untouched, and the patch reproduces the edit on
        # a clean checkout of the target.
        self.assertIn("@" + "1" * 40, repinned)
        self.assertIsNone(repin_issue.prepare(self.root, target, Path(str(self.patch) + ".2")))
        self.git("checkout", "-q", "--", ".")
        self.git("apply", str(self.patch))
        self.assertEqual((self.root / repin_issue.WORKFLOW).read_text(encoding="utf-8"), repinned)

    def test_a_pin_git_cannot_read_is_stale(self) -> None:
        self.git("checkout", "-q", self.first)
        self.assertEqual(repin_issue.prepare(self.root, self.first, self.patch), ZERO)
        self.assertIn(f"+      - uses: ryanduguid/release-policy/.github/actions/no-ai-attribution@{self.first}",
                      self.patch.read_text(encoding="utf-8"))

    def test_a_target_that_is_not_a_full_sha_is_refused(self) -> None:
        for target in ("HEAD", self.current[:12], self.current.upper()):
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "full commit SHA"):
                repin_issue.prepare(self.root, target, self.patch)

    def test_an_ambiguous_pin_or_digest_is_refused(self) -> None:
        path = self.root / repin_issue.WORKFLOW
        path.write_text(workflow(self.first) + workflow(self.first), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "exactly one live full-SHA action pin, found 2"):
            repin_issue.prepare(self.root, self.current, self.patch)
        self.git("checkout", "-q", "--", ".")
        target = self.commit("runs: scan v2\n", self.first)
        (self.root / repin_issue.BASELINE).write_text("CANONICAL_SHA256 = {}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "digest, found 0"):
            repin_issue.prepare(self.root, target, self.patch)

    def test_a_target_other_than_the_checkout_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "not the checked-out commit"):
            repin_issue.prepare(self.root, self.first, self.patch)

    def test_a_pin_off_the_target_history_is_stale_even_with_the_same_action(self) -> None:
        # A side-branch commit carrying identical action bytes, as the pin test rejects.
        self.git("checkout", "-q", "-b", "side", self.first)
        (self.root / "side.txt").write_text("side\n", encoding="utf-8")
        side = self.commit("runs: scan v1\n", ZERO)
        self.git("checkout", "-q", "-")
        target = self.commit("runs: scan v1\n", side)
        self.assertEqual(repin_issue.prepare(self.root, target, self.patch), side)

    def test_a_changed_helper_beside_the_action_is_stale(self) -> None:
        helper = self.root / repin_issue.ACTION_DIRECTORY / "scan.py"
        helper.write_text("print('scan')\n", encoding="utf-8")
        target = self.commit("runs: scan v1\n", self.first)
        self.assertEqual(repin_issue.prepare(self.root, target, self.patch), self.first)

    def test_publication_requires_main_unchanged_since_the_target(self) -> None:
        unrelated = self.root / "docs.md"
        unrelated.write_text("prose\n", encoding="utf-8")
        later = self.commit("runs: scan v1\n", self.first)
        self.assertTrue(repin_issue.unchanged_since(self.root, self.current, later))
        self.assertTrue(repin_issue.unchanged_since(self.root, self.current, self.current))
        self.assertFalse(repin_issue.unchanged_since(self.root, later, self.current))
        changed_action = self.commit("runs: scan v2\n", self.first)
        self.assertFalse(repin_issue.unchanged_since(self.root, later, changed_action))
        changed_pin = self.commit("runs: scan v2\n", changed_action)
        self.assertFalse(repin_issue.unchanged_since(self.root, changed_action, changed_pin))
        patch = self.root.parent / "x.patch"
        patch.write_text("diff\n", encoding="utf-8")
        with redirect_stderr(io.StringIO()) as err:
            code = repin_issue.main(["publish", "--repository", "owner/repo", "--target", later,
                                     "--current", changed_action, "--patch", str(patch)],
                                    root=self.root, gh=FakeGh([]))
        self.assertEqual(code, 1)
        self.assertIn(f"main no longer descends from {later}", err.getvalue())

    def test_a_failed_diff_is_reported(self) -> None:
        target = self.commit("runs: scan v2\n", self.first)
        real = repin_issue._git

        def failing_diff(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
            if arguments[0] == "diff":
                return subprocess.CompletedProcess(arguments, 128, "", "fatal: broken\n")
            return real(root, *arguments)

        with mock.patch.object(repin_issue, "_git", failing_diff):
            with self.assertRaisesRegex(ValueError, "git diff failed: fatal: broken"):
                repin_issue.prepare(self.root, target, self.patch)

    def test_the_command_reports_current_and_stale_pins(self) -> None:
        with redirect_stdout(io.StringIO()) as out:
            code = repin_issue.main(["prepare", "--target", self.current, "--patch",
                                     str(self.patch)], root=self.root)
        self.assertEqual((code, out.getvalue()), (0, "attribution pin is current\n"))
        target = self.commit("runs: scan v2\n", self.first)
        with redirect_stdout(io.StringIO()) as out:
            code = repin_issue.main(["prepare", "--target", target, "--patch", str(self.patch)],
                                    root=self.root)
        self.assertEqual(
            (code, out.getvalue()), (0, f"attribution pin {self.first} is stale; wrote {self.patch}\n")
        )


class PublishTests(unittest.TestCase):
    target = "b" * 40
    patch = "diff --git a/x b/x\n"

    def body(self) -> str:
        return repin_issue.issue_body(self.target, self.patch)

    def test_the_body_carries_the_patch_and_the_target(self) -> None:
        body = self.body()
        self.assertIn(f"repinned to `{self.target}`", body)
        self.assertTrue(body.endswith(f"````diff\n{self.patch}````\n"))
        with self.assertRaisesRegex(ValueError, "four-backtick"):
            repin_issue.issue_body(self.target, "````\n")

    def test_no_open_issue_opens_one_and_a_pull_request_is_not_an_issue(self) -> None:
        pull = {**issue(3, repin_issue.TITLE), "pull_request": {}}
        gh = FakeGh([[issue(2, "No, Mr Bond, I expect you to repin"), pull]])
        self.assertEqual(repin_issue.publish("owner/repo", self.target, self.patch, gh), "opened #7")
        arguments, stdin = gh.calls[-1]
        self.assertEqual(arguments, ["api", "--method", "POST", "repos/owner/repo/issues", "--input", "-"])
        self.assertEqual(json.loads(stdin or ""), {"title": repin_issue.TITLE, "body": self.body()})

    def test_the_workflow_issue_on_a_later_page_is_refreshed_in_place(self) -> None:
        gh = FakeGh([[issue(n, f"other {n}") for n in range(100)], [issue(140, repin_issue.TITLE)]])
        self.assertEqual(repin_issue.publish("owner/repo", self.target, self.patch, gh), "updated #140")
        self.assertEqual(gh.calls[1][0][-1], "repos/owner/repo/issues?state=open&per_page=100&page=2")
        arguments, stdin = gh.calls[-1]
        self.assertEqual(arguments, ["api", "--method", "PATCH", "repos/owner/repo/issues/140", "--input", "-"])
        self.assertEqual(json.loads(stdin or ""), {"body": self.body()})

    def test_an_issue_already_carrying_the_patch_is_left(self) -> None:
        gh = FakeGh([[issue(9, repin_issue.TITLE, body=self.body())]])
        self.assertEqual(repin_issue.publish("owner/repo", self.target, self.patch, gh),
                         "#9 already carries this patch")
        self.assertEqual(len(gh.calls), 1)

    def test_the_workflow_issue_is_refreshed_whatever_a_person_opened_beside_it(self) -> None:
        human = issue(5, repin_issue.TITLE, login="ryanduguid")
        ours = issue(6, repin_issue.TITLE)
        others = [issue(n, "other") for n in range(99)]
        for pages in ([[human, ours]], [[ours, human]], [[human, *others], [ours]]):
            gh = FakeGh(pages)
            with self.subTest(pages=len(pages), first=pages[0][0]["number"]):
                self.assertEqual(repin_issue.publish("owner/repo", self.target, self.patch, gh),
                                 "updated #6")
                self.assertEqual(gh.calls[-1][0][3], "repos/owner/repo/issues/6")

    def test_an_issue_a_person_opened_is_never_overwritten(self) -> None:
        for item in (issue(4, repin_issue.TITLE, login="ryanduguid"),
                     {"number": 4, "title": repin_issue.TITLE, "user": None}):
            gh = FakeGh([[item]])
            with self.subTest(item=item):
                self.assertEqual(repin_issue.publish("owner/repo", self.target, self.patch, gh),
                                 "left #4: a person opened it")
                self.assertEqual(len(gh.calls), 1)

    def test_malformed_or_endless_listings_and_bad_repositories_are_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-list"):
            repin_issue.publish("owner/repo", self.target, self.patch, FakeGh([{"message": "x"}]))
        full = [[issue(n, "other") for n in range(100)]] * repin_issue.MAX_ISSUE_PAGES
        with self.assertRaisesRegex(ValueError, "no decision after 10 pages"):
            repin_issue.publish("owner/repo", self.target, self.patch, FakeGh(full))
        with self.assertRaisesRegex(ValueError, "owner/name"):
            repin_issue.publish("owner/repo/extra", self.target, self.patch, FakeGh([[]]))

    def test_the_command_reads_the_patch_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            patch = Path(directory) / "repin.patch"
            patch.write_bytes(self.patch.encode("utf-8"))
            gh = FakeGh([[]])
            with redirect_stdout(io.StringIO()) as out, \
                    mock.patch.object(repin_issue, "unchanged_since", return_value=True) as fresh:
                code = repin_issue.main(["publish", "--repository", "owner/repo", "--target",
                                         self.target, "--current", "c" * 40, "--patch",
                                         str(patch)], gh=gh)
            fresh.assert_called_once_with(repin_issue.ROOT, self.target, "c" * 40)
        self.assertEqual((code, out.getvalue()), (0, "opened #7\n"))
        self.assertEqual(json.loads(gh.calls[-1][1] or "")["body"], self.body())


class GhTests(unittest.TestCase):
    def test_gh_passes_stdin_and_raises_on_failure(self) -> None:
        results = [
            subprocess.CompletedProcess([], 0, "{}", ""),
            subprocess.CompletedProcess([], 1, "", "HTTP 403\n"),
            subprocess.CompletedProcess([], 1, "", ""),
        ]
        with mock.patch.object(repin_issue.subprocess, "run", side_effect=results) as run:
            self.assertEqual(repin_issue._gh(["api", "x"], "in"), "{}")
            self.assertEqual(run.call_args.kwargs["input"], "in")
            with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
                repin_issue._gh(["api", "x"], None)
            with self.assertRaisesRegex(RuntimeError, "gh api x failed"):
                repin_issue._gh(["api", "x"], None)

    def test_the_script_runs_as_a_command(self) -> None:
        with mock.patch.object(sys, "argv", ["repin_issue.py", "--help"]), \
                redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path(str(ROOT / "scripts" / "repin_issue.py"), run_name="__main__")
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("prepare", out.getvalue())


if __name__ == "__main__":
    unittest.main()
