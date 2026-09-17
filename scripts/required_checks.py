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

Every trusted run of a named workflow has to report the check as a success, not
merely one of them. There is no most-recent run to prefer: re-running a workflow
keeps its run id and adds an attempt, so the highest id is not the latest
execution, and any rule that picks a single run lets a failing execution of a
mandatory check sit beside a passing one and be ignored.

A named check has to match exactly one job in each run. A display name is not an
identifier: two jobs in the same run may carry the same one, and choosing
between them would let a failed job hide behind a passing namesake. A name that
matches more than one job is refused as ambiguous, even when every job it
matches succeeded, so give each mandatory job a name of its own.
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
# How many times to re-read a listing that moved underneath the pagination.
_LISTING_ATTEMPTS = 3
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
class Run:
    run_id: int
    status: str


@dataclass(frozen=True)
class Job:
    run_id: int
    job_id: int
    # The run attempt GitHub reported, or 0 when it reported none. It appears in
    # diagnostics only; nothing here decides anything from it.
    attempt: int
    status: str
    conclusion: str

    @property
    def label(self) -> str:
        return f"{self.job_id} (attempt {self.attempt})" if self.attempt else str(self.job_id)


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


def _read_pages(
    fetch_json: Callable[[str], object], endpoint: str, key: str
) -> list[dict[str, object]] | None:
    """One complete read, or None when the listing moved while it was read."""
    items: list[dict[str, object]] = []
    seen: list[int] = []
    total: object = None
    page = 1
    while True:
        joiner = "&" if "?" in endpoint else "?"
        payload = fetch_json(f"{endpoint}{joiner}per_page={_PAGE_SIZE}&page={page}")
        if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
            raise RuntimeError(f"GitHub returned no {key!r} list for {endpoint}")
        if page == 1:
            total = payload.get("total_count")
        elif payload.get("total_count") != total:
            return None
        for entry in payload[key]:
            # Dropping an entry we cannot read would make the listing look
            # complete while hiding whatever that entry said, and what it said
            # might be that a mandatory check failed.
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), int):
                raise RuntimeError(
                    f"{endpoint}: GitHub returned an entry with no numeric id; "
                    "refusing to judge a listing that cannot be read in full"
                )
            seen.append(entry["id"])
            items.append(entry)
        if len(payload[key]) < _PAGE_SIZE:
            break
        page += 1
    if len(set(seen)) != len(seen):
        # A page repeated an entry, so an entry shifted out of view unseen.
        return None
    if isinstance(total, int) and total != len(seen):
        return None
    return items


def _paginate(
    fetch_json: Callable[[str], object], endpoint: str, key: str
) -> list[dict[str, object]]:
    """Read every page, refusing a listing that changed while it was read.

    Offset pagination is not a snapshot. A run started, re-run or removed while
    the pages are being read shifts the rest, so one page can repeat an entry
    and another can skip one. A skipped entry is what matters here, because it
    could be the run whose check failed. Read again when the listing moves, and
    fail closed rather than judge an incomplete set.
    """
    for _ in range(_LISTING_ATTEMPTS):
        items = _read_pages(fetch_json, endpoint, key)
        if items is not None:
            return items
    raise RuntimeError(
        f"{endpoint}: the listing changed while it was read, {_LISTING_ATTEMPTS} times over"
    )


def _text(value: object, field: str, where: str) -> str:
    """The field as text. Absent reads as empty; any other shape refuses.

    GitHub documents these fields as nullable, and a run with no head branch or
    no head repository cannot be a trusted run of this repository, so absence is
    an exclusion the policy below makes. A value of some other type is not an
    answer at all, and the run it describes might be the one whose mandatory
    check failed, so it refuses the listing rather than quietly dropping out of
    it.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise RuntimeError(
            f"{where}: {field} is {type(value).__name__}, not text; "
            "refusing to judge a listing that cannot be read in full"
        )
    return value


def _full_name(value: object, field: str, where: str) -> str:
    """The repository's full name, folded for comparison. Absent reads as empty."""
    if value is None:
        return ""
    if not isinstance(value, dict):
        raise RuntimeError(
            f"{where}: {field} is {type(value).__name__}, not a repository; "
            "refusing to judge a listing that cannot be read in full"
        )
    return _text(value.get("full_name"), f"{field}.full_name", where).casefold()


def trusted_runs(
    fetch_json: Callable[[str], object], repository: str, commit: str
) -> dict[str, tuple[Run, ...]]:
    """Group every trusted run of each workflow for the exact commit.

    All of them are kept, because all of them have to agree. Ordering is for
    readable messages only, never for deciding which run counts: a re-run keeps
    its run id and adds an attempt, so no field here identifies the latest
    execution.
    """
    runs = _paginate(fetch_json, f"repos/{repository}/actions/runs?head_sha={commit}", "workflow_runs")
    wanted = repository.casefold()
    by_workflow: dict[str, list[Run]] = {}
    for run in runs:
        where = f"{repository}: a workflow run for {commit}"
        path = run.get("path")
        run_id = run.get("id")
        # A run whose workflow cannot be read is a shape problem, not a run this
        # policy excludes, so it refuses the listing rather than vanishing from
        # it. Every field the trust decision reads is checked the same way.
        if not isinstance(path, str) or not isinstance(run_id, int):
            raise RuntimeError(
                f"{where} has no readable path or id; "
                "refusing to judge a listing that cannot be read in full"
            )
        # These comparisons are the policy. They exclude runs that are real and
        # readable but untrusted, such as a pull-request run or a fork's.
        if (
            _text(run.get("head_sha"), "head_sha", where) != commit
            or _text(run.get("event"), "event", where) not in _TRUSTED_EVENTS
            or _text(run.get("head_branch"), "head_branch", where) != _RELEASE_BRANCH
            or _full_name(run.get("repository"), "repository", where) != wanted
            or _full_name(run.get("head_repository"), "head_repository", where) != wanted
        ):
            continue
        by_workflow.setdefault(path, []).append(
            Run(run_id=run_id, status=_text(run.get("status"), "status", where))
        )
    return {
        path: tuple(sorted(found, key=lambda run: run.run_id, reverse=True))
        for path, found in by_workflow.items()
    }


def run_jobs(
    fetch_json: Callable[[str], object], repository: str, run_id: int, commit: str
) -> dict[str, tuple[Job, ...]]:
    """Every job of one run for this commit, grouped by the check name it reports.

    ``filter=latest`` asks GitHub for the latest attempt of each job, so two
    entries sharing a name are two distinct jobs rather than one job re-run. Both
    are kept. A display name is not an identifier, and preferring one of them
    here would let a failed job hide behind a passing namesake, so a required
    check that matches more than one job is refused as ambiguous instead.
    """
    jobs = _paginate(
        fetch_json, f"repos/{repository}/actions/runs/{run_id}/jobs?filter=latest", "jobs"
    )
    where = f"{repository}: run {run_id}"
    found: dict[str, list[Job]] = {}
    for raw in jobs:
        name = raw.get("name")
        job_id = raw.get("id")
        # Same rule as the run listing: an unreadable job refuses, because the
        # name it lacks might be the mandatory check that failed. A job for
        # another commit is excluded rather than refused.
        if not isinstance(name, str) or not isinstance(job_id, int):
            raise RuntimeError(
                f"{where} reported a job with no readable name or id; "
                "refusing to judge a listing that cannot be read in full"
            )
        if _text(raw.get("head_sha"), "head_sha", where) != commit:
            continue
        attempt = raw.get("run_attempt")
        found.setdefault(name, []).append(
            Job(
                run_id=run_id,
                job_id=job_id,
                attempt=attempt if isinstance(attempt, int) else 0,
                status=_text(raw.get("status"), "status", where),
                conclusion=_text(raw.get("conclusion"), "conclusion", where),
            )
        )
    return {
        name: tuple(sorted(jobs_found, key=lambda job: job.job_id))
        for name, jobs_found in found.items()
    }


def evaluate(
    required: Sequence[RequiredCheck],
    runs: dict[str, tuple[Run, ...]],
    jobs: Callable[[int], dict[str, tuple[Job, ...]]],
) -> Verdict:
    """Require every trusted run of a check's workflow to report it as a success.

    One run reporting success does not settle the check while another run of the
    same workflow, at the same commit, reports a failure, a cancellation, or
    nothing at all. A failure in any run refuses; otherwise a run that has not
    finished reporting leaves the check pending. A check that names more than one
    job in a run names nothing in particular, and refuses.
    """
    passed: list[str] = []
    pending: list[str] = []
    failed: list[str] = []
    cache: dict[int, dict[str, tuple[Job, ...]]] = {}
    for check in required:
        found_runs = runs.get(check.workflow, ())
        if not found_runs:
            failed.append(
                f"{check.label}: no push or workflow_dispatch run of that workflow on "
                f"{_RELEASE_BRANCH} reported this commit"
            )
            continue
        refused: list[str] = []
        waiting: list[str] = []
        witnesses: list[str] = []
        for run in found_runs:
            if run.run_id not in cache:
                cache[run.run_id] = jobs(run.run_id)
            matched = cache[run.run_id].get(check.job, ())
            if len(matched) > 1:
                # Only a selected name matters, so an unrelated duplicate
                # elsewhere in the run never blocks a release.
                refused.append(
                    f"{check.label}: run {run.run_id} has {len(matched)} distinct jobs "
                    f"named {check.job!r} (jobs {', '.join(job.label for job in matched)}); "
                    "a required check must name exactly one job, so give each job "
                    "a unique name"
                )
                continue
            job = matched[0] if matched else None
            if job is None and run.status != "completed":
                waiting.append(
                    f"{check.label}: run {run.run_id} is "
                    f"{run.status or 'in an unknown state'} and has not reported this check yet"
                )
            elif job is None:
                refused.append(
                    f"{check.label}: run {run.run_id} completed without reporting this check"
                )
            elif job.status != "completed":
                waiting.append(f"{check.label}: run {run.run_id} job {job.label} is {job.status}")
            elif job.conclusion == "success":
                witnesses.append(f"run {run.run_id} job {job.label}")
            else:
                refused.append(
                    f"{check.label}: run {run.run_id} job {job.label} concluded "
                    f"{job.conclusion!r}, not 'success'"
                )
        if refused:
            failed.extend(refused)
        elif waiting:
            pending.extend(waiting)
        else:
            passed.append(f"{check.label}: {', '.join(witnesses)}")
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
