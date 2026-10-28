"""File the attribution repin patch as an issue when the action pin is stale.

`attribution-policy.yml` pins `.github/actions/no-ai-attribution` by commit, so
a change to the action owes a second pull request that repins the workflow to
the merge commit (CONTRIBUTING.md). Pushing a workflow change needs workflow
permission, which the job token never has, so the repin workflow prepares the
exact patch here and files it as an issue for a person to apply.

`prepare` edits a checkout of main and writes the patch only when the pin is
stale; the workflow then runs the baseline and pin tests on the edited tree
before `publish` opens the issue, or refreshes the one it opened before. The
target is main as checked out, never the triggering commit, so a rerun of an
older run cannot replace a newer patch, and `publish` refuses when main has
since changed the action or the workflow.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ".github/workflows/attribution-policy.yml"
ACTION = ".github/actions/no-ai-attribution/action.yml"
ACTION_DIRECTORY = ACTION.removesuffix("/action.yml")
BASELINE = "tests/test_repository_baseline.sh"
TITLE = "Repin the attribution action"
# Issues the job token opens are authored by this account. An issue a person
# opened under the same title is left alone rather than overwritten.
BOT = "github-actions[bot]"
MAX_ISSUE_PAGES = 10
_FULL_SHA = re.compile(r"[0-9a-f]{40}\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
# Anchored to a live step line, as tests/test_attribution_pin.py reads it: a
# commented copy or a block scalar carrying the same text is not a pin.
PIN = re.compile(
    r"^(?P<head>[ \t]*(?:- )?uses: ryanduguid/release-policy/"
    + re.escape(ACTION.removesuffix("/action.yml"))
    + r"@)(?P<sha>[0-9a-f]{40})(?P<tail>[ \t]*)$",
    re.MULTILINE,
)
_DIGEST_LINE = re.compile(
    r'^(?P<head>[ \t]*"' + re.escape(WORKFLOW) + r'": ")(?P<digest>[0-9a-f]{64})(?P<tail>",)$',
    re.MULTILINE,
)
# The same normalisation tests/test_repository_baseline.sh applies before it
# hashes a document; the workflow reruns that test on the edited tree, so a
# divergence here fails the run instead of filing a wrong patch.
_THIRD_PARTY_PIN = re.compile(
    rb"^(?P<head>[ \t]*(?:- )?uses: (?!(?i:ryanduguid)/)[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_./-]+)"
    rb"@[0-9a-f]{40} # (?P<major>v[0-9]+)(?:\.[0-9]+){0,2}$",
    re.MULTILINE,
)

Gh = Callable[[Sequence[str], str | None], str]


def _lf(text: str) -> str:
    return text.replace("\r\n", "\n")


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # NOSONAR
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def _single(pattern: re.Pattern[str], text: str, label: str) -> re.Match[str]:
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise ValueError(f"expected exactly one {label}, found {len(matches)}")
    return matches[0]


def canonical_digest(document: bytes) -> str:
    canonical = document.replace(b"\r\n", b"\n")
    return hashlib.sha256(
        _THIRD_PARTY_PIN.sub(rb"\g<head>@" + b"0" * 40 + rb" # \g<major>", canonical)
    ).hexdigest()


def _tree(root: Path, revision: str, path: str) -> str | None:
    shown = _git(root, "rev-parse", "--verify", "--quiet", f"{revision}:{path}")
    return shown.stdout.strip() if shown.returncode == 0 else None


def _ancestor(root: Path, older: str, newer: str) -> bool:
    return _git(root, "merge-base", "--is-ancestor", older, newer).returncode == 0


def _same(root: Path, first: str, second: str, path: str) -> bool:
    tree = _tree(root, first, path)
    return tree is not None and tree == _tree(root, second, path)


def pin_is_current(root: Path, pin: str, target: str) -> bool:
    """Whether the pin is an ancestor of the target carrying its whole action.

    The whole action directory is compared, so a helper file beside action.yml
    counts. A pin Git cannot read, such as a commit squashed away, or one off
    the target's history, is stale too.
    """
    return _same(root, pin, target, ACTION_DIRECTORY) and _ancestor(root, pin, target)


def unchanged_since(root: Path, target: str, current: str) -> bool:
    """Whether `current` descends from `target` with the same action and patched files.

    The patch edits the workflow and the baseline, so a later change to either,
    even to a neighbouring digest line, could stop it applying.
    """
    return (
        _ancestor(root, target, current)
        and _same(root, target, current, ACTION_DIRECTORY)
        and _same(root, target, current, WORKFLOW)
        and _same(root, target, current, BASELINE)
    )


def _replace(match: re.Match[str], group: str, value: str) -> str:
    text = match.string
    return text[: match.start(group)] + value + text[match.end(group) :]


def prepare(root: Path, target: str, patch: Path) -> str | None:
    """Repin the checkout to `target` and write the patch, or return None."""
    if _FULL_SHA.match(target) is None:
        raise ValueError(f"target is not a full commit SHA: {target}")
    # The patch edits the working tree, so the tree must be the target's.
    if _git(root, "rev-parse", "HEAD").stdout.strip() != target:
        raise ValueError(f"target {target} is not the checked-out commit")
    workflow_path = root / WORKFLOW
    workflow = _lf(workflow_path.read_bytes().decode("utf-8"))
    pin = _single(PIN, workflow, "live full-SHA action pin")
    if pin_is_current(root, pin.group("sha"), target):
        return None
    repinned = _replace(pin, "sha", target)
    baseline_path = root / BASELINE
    baseline = _lf(baseline_path.read_bytes().decode("utf-8"))
    digest = _single(_DIGEST_LINE, baseline, f"{WORKFLOW} digest")
    rebaselined = _replace(digest, "digest", canonical_digest(repinned.encode("utf-8")))
    workflow_path.write_bytes(repinned.encode("utf-8"))
    baseline_path.write_bytes(rebaselined.encode("utf-8"))
    diff = _git(root, "diff", "--no-color", "--no-ext-diff", "--", WORKFLOW, BASELINE)
    if diff.returncode != 0:
        raise ValueError(f"git diff failed: {diff.stderr.strip()}")
    patch.write_bytes(diff.stdout.encode("utf-8"))
    return pin.group("sha")


def issue_body(target: str, patch_text: str) -> str:
    if "````" in patch_text:
        raise ValueError("the patch contains a four-backtick fence")
    return (
        f"`{ACTION.removesuffix('/action.yml')}` changed, and `{WORKFLOW}` no longer pins "
        f"a commit carrying it. Consumers run the pinned action, and push CI on main fails "
        f"`tests/test_attribution_pin.py` until the workflow is repinned to `{target}`.\n\n"
        "The job token cannot push a workflow change, so the repin workflow files the patch "
        "here. From a checkout of main that contains that commit:\n\n"
        "1. Save the patch below as `repin.patch` and run `git apply repin.patch`.\n"
        "2. Run `bash tests/test_repository_baseline.sh` and "
        "`python -m unittest discover -s tests -p test_attribution_pin.py`; the workflow "
        "ran both on the patched tree before filing this issue.\n"
        "3. Open a pull request that closes this issue, then re-pin consumers as "
        "`docs/re-pinning.md` describes.\n\n"
        f"````diff\n{patch_text}````\n"
    )


def _gh(arguments: Sequence[str], stdin: str | None) -> str:
    result = subprocess.run(  # NOSONAR
        ["gh", *arguments],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"gh {' '.join(arguments)} failed")
    return result.stdout


def open_issues(repository: str, gh: Gh) -> list[dict[str, object]]:
    """Every open issue with the title, read to the last page."""
    found: list[dict[str, object]] = []
    for page in range(1, MAX_ISSUE_PAGES + 1):
        items = json.loads(
            gh(["api", f"repos/{repository}/issues?state=open&per_page=100&page={page}"], None)
        )
        if not isinstance(items, list):
            raise ValueError("GitHub returned a non-list issue page")
        # The issues endpoint lists pull requests too.
        found += [
            dict(item) for item in items
            if item.get("title") == TITLE and "pull_request" not in item
        ]
        if len(items) < 100:
            return found
    raise ValueError(f"no decision after {MAX_ISSUE_PAGES} pages of open issues")


def _opened_by_workflow(issue: dict[str, object]) -> bool:
    user = issue.get("user")
    return isinstance(user, dict) and user.get("login") == BOT


def publish(repository: str, target: str, patch_text: str, gh: Gh) -> str:
    if _REPOSITORY.match(repository) is None:
        raise ValueError(f"not an owner/name repository: {repository}")
    body = issue_body(target, patch_text)
    issues = open_issues(repository, gh)
    ours = [issue for issue in issues if _opened_by_workflow(issue)]
    if not ours and issues:
        return f"left #{issues[0]['number']}: a person opened it"
    if not ours:
        created = json.loads(
            gh(["api", "--method", "POST", f"repos/{repository}/issues", "--input", "-"],
               json.dumps({"title": TITLE, "body": body}))
        )
        return f"opened #{created['number']}"
    existing = ours[0]
    number = existing["number"]
    if existing.get("body") == body:
        return f"#{number} already carries this patch"
    gh(["api", "--method", "PATCH", f"repos/{repository}/issues/{number}", "--input", "-"],
       json.dumps({"body": body}))
    return f"updated #{number}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_command = commands.add_parser("prepare")
    prepare_command.add_argument("--target", required=True)
    prepare_command.add_argument("--patch", type=Path, required=True)
    publish_command = commands.add_parser("publish")
    publish_command.add_argument("--repository", required=True)
    publish_command.add_argument("--target", required=True)
    publish_command.add_argument("--patch", type=Path, required=True)
    publish_command.add_argument("--current", required=True,
                                 help="main as fetched immediately before publication")
    return parser


def main(argv: Sequence[str] | None = None, *, root: Path = ROOT, gh: Gh = _gh) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "prepare":
        stale = prepare(root, arguments.target, arguments.patch)
        if stale is None:
            print("attribution pin is current")
        else:
            print(f"attribution pin {stale} is stale; wrote {arguments.patch}")
        return 0
    if not unchanged_since(root, arguments.target, arguments.current):
        print(f"main no longer descends from {arguments.target} with the same action and "
              "workflow; rerun this workflow to repin to main", file=sys.stderr)
        return 1
    patch_text = arguments.patch.read_bytes().decode("utf-8")
    print(publish(arguments.repository, arguments.target, patch_text, gh))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
