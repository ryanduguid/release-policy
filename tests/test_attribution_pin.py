"""The shared attribution workflow must run the action this repository ships.

`attribution-policy.yml` pins the composite action by commit. Every consumer
calls the workflow at a release-policy commit, so the action they execute is
whatever that pin names, not the working tree. On 20 September 2026 the pin
named a commit that had been squashed away and carried an older scanner than
main, so every consumer ran the weaker check while the unit tests passed
against the newer one. These checks stop that recurring.
"""

from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "attribution-policy.yml"
ACTION = ".github/actions/no-ai-attribution/action.yml"
# Anchored to a live step line: a commented copy or a block scalar carrying
# the same text is not a pin.
PIN = re.compile(
    r"^(?:- )?uses: ryanduguid/release-policy/"
    + re.escape(ACTION.removesuffix("/action.yml"))
    + r"@([0-9a-f]{40})$"
)


def git(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(ROOT), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def pinned_sha(workflow_text: str) -> str:
    pins = [match.group(1) for line in workflow_text.splitlines() if (match := PIN.match(line.strip()))]
    if len(pins) != 1:
        raise AssertionError(f"expected exactly one live full-SHA action pin, found {pins}")
    return pins[0]


def integration_tip() -> str:
    # In pull request CI, HEAD is a synthetic merge that already contains the
    # branch, so a branch could pin its own head and pass. The base branch is
    # the reference that survives a squash merge.
    if git("rev-parse", "--verify", "--quiet", "origin/main").returncode == 0:
        return "origin/main"
    return "HEAD"


class PinParsingTests(unittest.TestCase):
    def test_a_commented_or_quoted_pin_is_not_a_pin(self) -> None:
        sha = "0" * 40
        live = f"      - uses: ryanduguid/release-policy/.github/actions/no-ai-attribution@{sha}\n"
        commented = "      # - uses: ryanduguid/release-policy/.github/actions/no-ai-attribution@" + "1" * 40 + "\n"
        scalar = "        note: uses: ryanduguid/release-policy/.github/actions/no-ai-attribution@" + "2" * 40 + "\n"
        self.assertEqual(pinned_sha(live + commented + scalar), sha)
        with self.assertRaises(AssertionError):
            pinned_sha(commented + scalar)
        with self.assertRaises(AssertionError):
            pinned_sha(live + live)


class AttributionPinTests(unittest.TestCase):
    def test_the_pin_is_a_commit_on_the_base_branch(self) -> None:
        sha = pinned_sha(WORKFLOW.read_text(encoding="utf-8"))
        tip = integration_tip()
        result = git("merge-base", "--is-ancestor", sha, tip)
        self.assertEqual(
            result.returncode,
            0,
            f"{sha} is not an ancestor of {tip}; a pin must name a commit on main, "
            "never a pull request head that a squash merge discards",
        )

    def test_the_pinned_action_is_the_action_on_disk(self) -> None:
        # A pull request that changes the action cannot know its own merge
        # commit, so this comparison runs on pushes and locally. A red push
        # run on main after such a merge means the repin is owed.
        if os.environ.get("GITHUB_EVENT_NAME") == "pull_request":
            self.skipTest("the repin follows the merge that changes the action")
        sha = pinned_sha(WORKFLOW.read_text(encoding="utf-8"))
        shown = git("show", f"{sha}:{ACTION}")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        on_disk = (ROOT / ACTION).read_bytes().decode("utf-8").replace("\r\n", "\n")
        self.assertEqual(
            shown.stdout.replace("\r\n", "\n"),
            on_disk,
            f"the action at {sha} differs from the working tree; repin the workflow to "
            "the main commit that carries the current action",
        )


if __name__ == "__main__":
    unittest.main()
