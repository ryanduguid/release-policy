"""Require the consumer's mandatory checks to have succeeded for the exact release commit.

The release gates prove that the tag names the current ``main`` commit. They do
not prove that the consumer's own CI passed for that commit: branch protection
runs the required checks on the pull request head, the merge produces a new
commit, and a path-filtered monorepo gate reports success for an engine whose
tests never ran. This gate closes that gap. The caller names the checks it
considers mandatory, and each one must have completed with ``success`` in a
workflow run of this repository for exactly this commit.

Each required check is one line of ``<workflow path>: <job name>``. The workflow
path is the file under ``.github/workflows/`` that GitHub records on the run and
the job name is the check name GitHub shows, ``lint`` or
``payday-super-checker / test (3.12)``. Only ``push`` and ``workflow_dispatch``
runs on ``main`` count, because those execute the workflow file at the commit
itself; a ``pull_request`` run tests a merge ref instead. A skipped, cancelled,
failed, missing or still-running check never authorises publication, and a
bounded wait ends in failure, not in a retry.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, Sequence

_FULL_SHA = re.compile(r"[0-9a-f]{40}\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_WORKFLOW = re.compile(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml\Z")
# Runs whose workflow file is the one committed at the release commit.
_TRUSTED_EVENTS = frozenset({"push", "workflow_dispatch"})
_RELEASE_BRANCH = "main"
_PAGE_SIZE = 100
# GitHub caps the wait a caller may ask for; a release that needs longer than
# this has something else wrong with it.
MAX_WAIT_SECONDS = 1800


@dataclass(frozen=True)
class RequiredCheck:
    workflow: str
    job: str

    @property
    def label(self) -> str:
        return f"{self.workflow}: {self.job}"


@dataclass(frozen=True)
class Job:
    run_id: int
    job_id: int
    status: str
    conclusion: str


@dataclass(frozen=True)
class Verdict:
    passed: tuple[str, ...]
    pending: tuple[str, ...]
    failed: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.pending and not self.failed


def parse_required_checks(text: str) -> tuple[RequiredCheck, ...]:
    """Parse the caller's list. Empty, malformed or duplicate lines fail closed."""
    checks: list[RequiredCheck] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        workflow, separator, job = line.partition(": ")
        workflow = workflow.strip()
        job = job.strip()
        if not separator or not job:
            raise ValueError(f"required check {line!r} is not '<workflow path>: <job name>'")
        if _WORKFLOW.match(workflow) is None:
            raise ValueError(f"required check {line!r} names a path outside .github/workflows/")
        check = RequiredCheck(workflow, job)
        if check in checks:
            raise ValueError(f"required check {check.label!r} is listed twice")
        checks.append(check)
    if not checks:
        raise ValueError("no required checks were named; the gate fails closed")
    return tuple(checks)


def _paginate(
    fetch_json: Callable[[str], object], endpoint: str, key: str
) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    page = 1
    while True:
        joiner = "&" if "?" in endpoint else "?"
        payload = fetch_json(f"{endpoint}{joiner}per_page={_PAGE_SIZE}&page={page}")
        if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
            raise RuntimeError(f"GitHub returned no {key!r} list for {endpoint}")
        batch = [item for item in payload[key] if isinstance(item, dict)]
        items.extend(batch)
        if len(payload[key]) < _PAGE_SIZE:
            return items
        page += 1


def _full_name(value: object) -> str:
    if isinstance(value, dict) and isinstance(value.get("full_name"), str):
        return value["full_name"].casefold()
    return ""


def trusted_runs(
    fetch_json: Callable[[str], object], repository: str, commit: str
) -> dict[str, list[int]]:
    """Map each workflow path to the ids of its trusted runs for the exact commit."""
    runs = _paginate(fetch_json, f"repos/{repository}/actions/runs?head_sha={commit}", "workflow_runs")
    wanted = repository.casefold()
    by_workflow: dict[str, list[int]] = {}
    for run in runs:
        path = run.get("path")
        run_id = run.get("id")
        if (
            run.get("head_sha") != commit
            or run.get("event") not in _TRUSTED_EVENTS
            or run.get("head_branch") != _RELEASE_BRANCH
            or _full_name(run.get("repository")) != wanted
            or _full_name(run.get("head_repository")) != wanted
            or not isinstance(path, str)
            or not isinstance(run_id, int)
        ):
            continue
        by_workflow.setdefault(path, []).append(run_id)
    return by_workflow


def run_jobs(
    fetch_json: Callable[[str], object], repository: str, run_id: int, commit: str
) -> dict[str, Job]:
    """The latest attempt of each job in one run, keyed by check name."""
    jobs = _paginate(
        fetch_json, f"repos/{repository}/actions/runs/{run_id}/jobs?filter=latest", "jobs"
    )
    latest: dict[str, Job] = {}
    for raw in jobs:
        name = raw.get("name")
        job_id = raw.get("id")
        if not isinstance(name, str) or not isinstance(job_id, int) or raw.get("head_sha") != commit:
            continue
        job = Job(
            run_id=run_id,
            job_id=job_id,
            status=str(raw.get("status") or ""),
            conclusion=str(raw.get("conclusion") or ""),
        )
        if name not in latest or job.job_id > latest[name].job_id:
            latest[name] = job
    return latest


def evaluate(
    required: Sequence[RequiredCheck],
    runs: dict[str, list[int]],
    jobs: Callable[[int], dict[str, Job]],
) -> Verdict:
    """Judge every required check from the newest trusted run of its workflow."""
    passed: list[str] = []
    pending: list[str] = []
    failed: list[str] = []
    cache: dict[int, dict[str, Job]] = {}
    for check in required:
        found: Job | None = None
        # Newest run first: an older success must not stand in for a newer
        # failure or cancellation of the same check for the same commit.
        for run_id in sorted(runs.get(check.workflow, ()), reverse=True):
            if run_id not in cache:
                cache[run_id] = jobs(run_id)
            if check.job in cache[run_id]:
                found = cache[run_id][check.job]
                break
        if found is None:
            failed.append(f"{check.label}: no trusted run of that workflow reported this check")
        elif found.status != "completed":
            pending.append(f"{check.label}: run {found.run_id} job {found.job_id} is {found.status}")
        elif found.conclusion == "success":
            passed.append(f"{check.label}: run {found.run_id} job {found.job_id}")
        else:
            failed.append(
                f"{check.label}: run {found.run_id} job {found.job_id} concluded "
                f"{found.conclusion!r}, not 'success'"
            )
    return Verdict(tuple(passed), tuple(pending), tuple(failed))


def check(
    required: Sequence[RequiredCheck],
    *,
    repository: str,
    commit: str,
    fetch_json: Callable[[str], object],
    wait_seconds: int = 0,
    poll_seconds: int = 30,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Verdict:
    """Evaluate the checks, waiting only for pending ones and only for a bounded time."""
    deadline = clock() + wait_seconds
    while True:
        runs = trusted_runs(fetch_json, repository, commit)
        verdict = evaluate(
            required, runs, lambda run_id: run_jobs(fetch_json, repository, run_id, commit)
        )
        if not verdict.pending or clock() >= deadline:
            return verdict
        sleep(min(poll_seconds, max(deadline - clock(), 0)))


def _gh_json(endpoint: str) -> object:
    result = subprocess.run(
        ["gh", "api", "-H", "X-GitHub-Api-Version: 2026-03-10", endpoint],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"gh api failed for {endpoint}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"GitHub returned invalid JSON for {endpoint}") from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--wait-seconds", type=int, default=0)
    parser.add_argument("--poll-seconds", type=int, default=30)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: str | None = None,
    fetch_json: Callable[[str], object] = _gh_json,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    arguments = _parser().parse_args(argv)
    if _REPOSITORY.match(arguments.repository) is None:
        print(f"FAIL repository {arguments.repository!r} is not owner/name", file=sys.stderr)
        return 1
    if _FULL_SHA.match(arguments.commit) is None:
        print(f"FAIL commit {arguments.commit!r} is not a 40-hex commit", file=sys.stderr)
        return 1
    if not 0 <= arguments.wait_seconds <= MAX_WAIT_SECONDS or arguments.poll_seconds < 1:
        print(
            f"FAIL wait must be 0 to {MAX_WAIT_SECONDS} seconds and poll at least 1",
            file=sys.stderr,
        )
        return 1
    try:
        required = parse_required_checks(sys.stdin.read() if stdin is None else stdin)
        verdict = check(
            required,
            repository=arguments.repository,
            commit=arguments.commit,
            fetch_json=fetch_json,
            wait_seconds=arguments.wait_seconds,
            poll_seconds=arguments.poll_seconds,
            sleep=sleep,
        )
    except (RuntimeError, ValueError) as error:
        print(f"FAIL {error}", file=sys.stderr)
        return 1
    for line in verdict.passed:
        print(f"ok   {line}")
    for line in verdict.pending:
        print(f"FAIL {line} after the {arguments.wait_seconds}s wait", file=sys.stderr)
    for line in verdict.failed:
        print(f"FAIL {line}", file=sys.stderr)
    if not verdict.ok:
        return 1
    print(f"required checks passed: {len(verdict.passed)} for {arguments.commit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
