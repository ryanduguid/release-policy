"""Validate release-family canary pins and live successful-run evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Sequence

_FULL_SHA = re.compile(r"[0-9a-f]{40}\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_WORKFLOW = re.compile(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml\Z")
_RELEASE_TAG = re.compile(
    r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z"
)
_TAG_PREFIX = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_FAMILIES = {"archive", "python", "skills", "verify"}
# A release family publishes on a tag, so a success newer than the recorded
# evidence means that evidence has been overtaken and a person should look.
# The verification consumer runs on every push to its default branch, where a
# newer success is the ordinary state: requiring identity there made the
# manifest stale within a day of every consumer push and proved nothing the
# current-pin check does not prove directly.
_RELEASE_FAMILIES = frozenset(_FAMILIES - {"verify"})
# Successful runs are read page by page until the relevant namespaced run
# appears, because a repository with busy sibling namespaces can hide the
# recorded run behind newer successes from other namespaces. Ten pages of a
# hundred successes is far beyond anything the estate produces; a search that
# exhausts them is reported as its own condition rather than as a missing run.
MAX_RUN_PAGES = 10
# GitHub's compare API reports the pin relative to release-policy main. Only
# these 2 statuses mean the pin is an ancestor of main. GitHub refuses a
# reusable-workflow call at a commit no branch or tag reaches, before any job
# starts, so a pin that matches the consumer's workflow byte for byte can still
# be dead after a history rewrite of this repository.
_REACHABLE_STATUSES = frozenset({"identical", "behind"})
# From 1 October 2026 GitHub deletes workflow runs older than the repository's
# retention period, at most 90 days for a public repository. A recorded run
# that returns 404 only after that, plus a day's margin for long runs, has
# expired; an earlier 404 is a missing run and stays an error.
RETENTION = timedelta(days=90)
EXPIRY_MARGIN = timedelta(days=1)
_STARTED_AT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
# The release-policy workflow whose certificate signs each release family's
# provenance, read from verified attestations of the 4 October 2026 releases.
_SIGNER_WORKFLOWS = {
    "archive": ".github/workflows/publish-archives.yml",
    "python": ".github/workflows/release-python.yml",
    "skills": ".github/workflows/publish-archives.yml",
}
SLSA_PROVENANCE = "https://slsa.dev/provenance/v1"


class GitHubError(RuntimeError):
    """A failed GitHub read, with the HTTP status when GitHub returned one."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Evidence:
    run_id: int
    head_sha: str
    ref: str
    policy_sha: str
    started_at: str


@dataclass(frozen=True)
class Canary:
    family: str
    repository: str
    workflow_path: str
    policy_workflow: str
    current_policy_sha: str
    evidence: Evidence
    tag_prefix: str = ""


@dataclass(frozen=True)
class CheckResult:
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    # Verified conditions a reviewer should know about; never fail the audit.
    notices: tuple[str, ...] = ()


def _mapping(
    value: object,
    *,
    label: str,
    keys: set[str],
    optional_keys: set[str] | None = None,
) -> dict[str, object]:
    optional = optional_keys or set()
    if not isinstance(value, dict) or set(value) - optional != keys:
        raise ValueError(
            f"{label} must contain exactly {sorted(keys)} "
            f"and only optional keys {sorted(optional)}"
        )
    return value


def _text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _sha(value: object, *, label: str) -> str:
    result = _text(value, label=label)
    if _FULL_SHA.fullmatch(result) is None:
        raise ValueError(f"{label} must be a full lower-case SHA")
    return result


def _matches_release_tag(ref: object, tag_prefix: str) -> bool:
    if not isinstance(ref, str):
        return False
    if tag_prefix:
        prefix = f"{tag_prefix}/"
        if not ref.startswith(prefix):
            return False
        ref = ref[len(prefix):]
    return _RELEASE_TAG.fullmatch(ref) is not None


def parse_manifest(document: object) -> tuple[Canary, ...]:
    root = _mapping(document, label="manifest", keys={"families", "schema"})
    if root["schema"] != 1:
        raise ValueError("manifest schema must be 1")
    entries = root["families"]
    if not isinstance(entries, list):
        raise ValueError("manifest families must be a list")

    canaries: list[Canary] = []
    seen: set[str] = set()
    for index, raw_entry in enumerate(entries):
        entry = _mapping(
            raw_entry,
            label=f"family[{index}]",
            keys={
                "current_policy_sha",
                "evidence",
                "family",
                "policy_workflow",
                "repository",
                "workflow_path",
            },
            optional_keys={"tag_prefix"},
        )
        family = _text(entry["family"], label=f"family[{index}].family")
        if family not in _FAMILIES:
            raise ValueError(f"unsupported canary family: {family}")
        if family in seen:
            raise ValueError(f"duplicate canary family: {family}")
        seen.add(family)

        tag_prefix = entry.get("tag_prefix", "")
        if not isinstance(tag_prefix, str) or (
            tag_prefix and (
                family not in {"archive", "python"}
                or _TAG_PREFIX.fullmatch(tag_prefix) is None
            )
        ):
            raise ValueError(
                f"{family}.tag_prefix must be empty or a lower-case hyphenated "
                "archive/Python namespace"
            )

        repository = _text(entry["repository"], label=f"{family}.repository")
        if _REPOSITORY.fullmatch(repository) is None:
            raise ValueError(f"{family}.repository must be owner/name")
        workflow_path = _text(entry["workflow_path"], label=f"{family}.workflow_path")
        policy_workflow = _text(
            entry["policy_workflow"], label=f"{family}.policy_workflow"
        )
        if _WORKFLOW.fullmatch(workflow_path) is None:
            raise ValueError(f"{family}.workflow_path is not a workflow path")
        if _WORKFLOW.fullmatch(policy_workflow) is None:
            raise ValueError(f"{family}.policy_workflow is not a workflow path")

        evidence_raw = _mapping(
            entry["evidence"],
            label=f"{family}.evidence",
            keys={"head_sha", "policy_sha", "ref", "run_id", "started_at"},
        )
        run_id = evidence_raw["run_id"]
        if not isinstance(run_id, int) or run_id < 1:
            raise ValueError(f"{family}.evidence.run_id must be positive")
        ref = _text(evidence_raw["ref"], label=f"{family}.evidence.ref")
        if family == "verify":
            if ref != "main":
                raise ValueError("verify evidence must use main")
        elif not _matches_release_tag(ref, tag_prefix):
            raise ValueError(f"{family} release evidence must use a canonical version tag")
        started_at = _text(evidence_raw["started_at"], label=f"{family}.evidence.started_at")
        if _STARTED_AT.fullmatch(started_at) is None:
            raise ValueError(f"{family}.evidence.started_at must be a UTC timestamp")

        canaries.append(
            Canary(
                family=family,
                repository=repository,
                workflow_path=workflow_path,
                policy_workflow=policy_workflow,
                current_policy_sha=_sha(
                    entry["current_policy_sha"], label=f"{family}.current_policy_sha"
                ),
                evidence=Evidence(
                    run_id=run_id,
                    head_sha=_sha(
                        evidence_raw["head_sha"], label=f"{family}.evidence.head_sha"
                    ),
                    ref=ref,
                    policy_sha=_sha(
                        evidence_raw["policy_sha"], label=f"{family}.evidence.policy_sha"
                    ),
                    started_at=started_at,
                ),
                tag_prefix=tag_prefix,
            )
        )
    return tuple(canaries)


def _canonical_bytes(document: object) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def load_manifest(path: Path) -> tuple[Canary, ...]:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError("canary manifest must not contain a UTF-8 BOM")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("canary manifest must be valid UTF-8 JSON") from error
    if raw != _canonical_bytes(document):
        raise ValueError("canary manifest must use canonical sorted JSON")
    canaries = parse_manifest(document)
    observed = {canary.family for canary in canaries}
    if observed != _FAMILIES:
        raise ValueError(
            f"canary manifest must cover {sorted(_FAMILIES)}, found {sorted(observed)}"
        )
    return canaries


def _extract_pin(workflow: str, canary: Canary) -> str:
    literal = re.escape(f"ryanduguid/release-policy/{canary.policy_workflow}@")
    matches = re.findall(literal + r"([0-9a-f]{40})(?![0-9a-f])", workflow)
    if len(matches) != 1:
        raise ValueError(
            f"{canary.family}: expected one literal {canary.policy_workflow} pin, "
            f"found {len(matches)}"
        )
    return matches[0]


def _referenced_policy_sha(run: dict[str, object], canary: Canary) -> str | None:
    referenced = run.get("referenced_workflows")
    if not isinstance(referenced, list):
        return None
    expected_prefix = f"ryanduguid/release-policy/{canary.policy_workflow}@"
    matches: list[str] = []
    for raw in referenced:
        if not isinstance(raw, dict):
            continue
        path = raw.get("path")
        sha = raw.get("sha")
        if (
            isinstance(path, str)
            and path.startswith(expected_prefix)
            and isinstance(sha, str)
            and _FULL_SHA.fullmatch(sha)
        ):
            matches.append(sha)
    return matches[0] if len(matches) == 1 else None


def _latest_relevant_run(payload: object, canary: Canary) -> dict[str, object] | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("workflow_runs"), list):
        return None
    # The listing has returned an older success ahead of newer ones on the same
    # page (30 September and 7 October 2026), so order by start time, newest
    # first, instead of trusting the response order.
    runs = sorted(
        (run for run in payload["workflow_runs"] if isinstance(run, dict)),
        key=lambda run: str(run.get("run_started_at") or ""),
        reverse=True,
    )
    for run in runs:
        if run.get("conclusion") != "success":
            continue
        branch = run.get("head_branch")
        if canary.family == "verify":
            if branch == "main":
                return run
        elif _matches_release_tag(branch, canary.tag_prefix):
            return run
    return None


def _started_at(canary: Canary) -> datetime:
    return datetime.strptime(canary.evidence.started_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )


def _review_instruction(canary: Canary, newer: str) -> str:
    return (
        f"NEWER_RELEASE_REVIEW_REQUIRED: recorded run {canary.evidence.run_id} "
        f"({canary.evidence.ref}) is overtaken by {newer}. Review the newer release's "
        "run, source SHA and policy pin, record it in canaries.json in canonical "
        "form, then rerun the live audit."
    )


def _release_freshness_errors(
    canary: Canary, fetch_json: Callable[[str], object], prefix: str
) -> list[str]:
    """Check the published releases once the recorded run has expired.

    Runs disappear after retention, so an empty run listing cannot show that no
    newer release happened. Published releases stay, and every release family
    publishes one, so the newest release in the family's tag namespace by
    publication time has to be the recorded one.
    """
    releases: list[dict[str, object]] = []
    try:
        for page in range(1, MAX_RUN_PAGES + 1):
            payload = fetch_json(
                f"repos/{canary.repository}/releases?per_page=100&page={page}"
            )
            if not isinstance(payload, list):
                return [f"{prefix}: release listing is not a list"]
            releases.extend(item for item in payload if isinstance(item, dict))
            if len(payload) < 100:
                break
        else:
            return [
                f"{prefix}: more than {MAX_RUN_PAGES} pages of releases were read "
                "without reaching the end"
            ]
    except RuntimeError as error:
        return [f"{prefix}: could not read releases: {error}"]
    relevant = [
        release
        for release in releases
        if release.get("draft") is False
        and _matches_release_tag(release.get("tag_name"), canary.tag_prefix)
        and isinstance(release.get("published_at"), str)
    ]
    if not relevant:
        return [f"{prefix}: no published release in the family's tag namespace"]
    newest = max(relevant, key=lambda release: str(release["published_at"]))
    newest_tag = newest.get("tag_name")
    if newest_tag != canary.evidence.ref:
        return [f"{prefix}: {_review_instruction(canary, f'release {newest_tag!r}')}"]
    return []


def _ruleset_protects(ruleset: object, pattern: str) -> bool:
    """Whether an active tag ruleset stops tags under pattern moving or being deleted.

    Only the exact pattern or ~ALL counts, and any exclusion or visible bypass actor
    disqualifies the ruleset: a form this cannot read fails the audit rather than
    passing it.
    """
    if (
        not isinstance(ruleset, dict)
        or ruleset.get("target") != "tag"
        or ruleset.get("enforcement") != "active"
        or ruleset.get("bypass_actors") not in (None, [])
    ):
        return False
    rules = ruleset.get("rules")
    conditions = ruleset.get("conditions")
    ref_name = conditions.get("ref_name") if isinstance(conditions, dict) else None
    if not isinstance(rules, list) or not isinstance(ref_name, dict):
        return False
    types = {rule.get("type") for rule in rules if isinstance(rule, dict)}
    include = ref_name.get("include")
    return (
        {"update", "deletion"} <= types
        and ref_name.get("exclude") == []
        and isinstance(include, list)
        and (pattern in include or "~ALL" in include)
    )


def _tag_protection(
    canary: Canary, fetch_json: Callable[[str], object], prefix: str
) -> tuple[list[str], list[str]]:
    """Return errors and notices for the family's version tag protection.

    SLSA v1.2 source track level 2 and above requires tags that cannot be moved or
    deleted. The listing omits a ruleset's rules and conditions, so each active one
    is read in full. GitHub returns bypass_actors only to a caller with write access
    to the ruleset, which the audit's token lacks on a consumer. Requiring the list
    would fail every consumer, so a list it cannot see is a notice, not a pass claim.
    """
    namespace = f"{canary.tag_prefix}/" if canary.tag_prefix else ""
    pattern = f"refs/tags/{namespace}v*"
    endpoint = f"repos/{canary.repository}/rulesets"
    try:
        listing = fetch_json(f"{endpoint}?targets=tag&per_page=100")
        if not isinstance(listing, list):
            return [f"{prefix}: tag ruleset listing is not a list"], []
        for item in listing:
            if not isinstance(item, dict) or item.get("enforcement") != "active":
                continue
            ruleset = fetch_json(f"{endpoint}/{item.get('id')}")
            if _ruleset_protects(ruleset, pattern):
                if isinstance(ruleset, dict) and ruleset.get("bypass_actors") is None:
                    return [], [
                        f"{prefix}: this token cannot see the bypass list of tag ruleset "
                        f"{item.get('id')}, so its bypass actors were not checked"
                    ]
                return [], []
    except RuntimeError as error:
        return [f"{prefix}: could not read tag rulesets: {error}"], []
    return [f"{prefix}: no recognised active tag ruleset without bypass actors blocks update and deletion of {pattern}"], []


def _expired_verify_errors(
    canary: Canary,
    latest: dict[str, object],
    fetch_json: Callable[[str], object],
    prefix: str,
) -> list[str]:
    """Require a later, readable verification success once the recorded one expired.

    The expired record stays a reviewed pointer; current verification is shown by
    a different success that can still be read in full.
    """
    run_id = latest.get("id")
    if (
        not isinstance(run_id, int)
        or run_id < 1
        or run_id == canary.evidence.run_id
        or str(latest.get("run_started_at") or "") <= canary.evidence.started_at
    ):
        return [
            f"{prefix}: evidence run {canary.evidence.run_id} has expired and no later "
            "success replaces it; run the verification workflow on main and review it"
        ]
    try:
        detail = fetch_json(f"repos/{canary.repository}/actions/runs/{run_id}")
    except RuntimeError as error:
        return [f"{prefix}: could not read current success {run_id!r}: {error}"]
    if not isinstance(detail, dict):
        return [f"{prefix}: current success {run_id!r} response is not an object"]
    # The listing only nominates the run; every property it is accepted for has
    # to come from the run's own record.
    errors = [
        f"{prefix}: current success {run_id!r} {field} {detail.get(field)!r} != {value!r}"
        for field, value in {
            "id": run_id,
            "conclusion": "success",
            "head_branch": "main",
            "path": canary.workflow_path,
        }.items()
        if detail.get(field) != value
    ]
    started = detail.get("run_started_at")
    if not isinstance(started, str) or _STARTED_AT.fullmatch(started) is None or (
        started <= canary.evidence.started_at
    ):
        errors.append(
            f"{prefix}: current success {run_id!r} run_started_at {started!r} is not "
            f"later than the expired {canary.evidence.started_at}"
        )
    pin = _referenced_policy_sha(detail, canary)
    if pin != canary.current_policy_sha:
        errors.append(
            f"{prefix}: current success {run_id!r} used policy pin {pin!r} != current "
            f"{canary.current_policy_sha}"
        )
    return errors


def check_live(
    canaries: Sequence[Canary],
    *,
    fetch_json: Callable[[str], object],
    fetch_text: Callable[[str], str],
    verify_provenance: Callable[[Canary], None] | None = None,
    now: datetime | None = None,
) -> CheckResult:
    """Audit each canary against GitHub.

    ``verify_provenance`` raises RuntimeError unless the recorded release's signed
    provenance verifies. Supplying it selects release evidence mode, which ``main``
    always uses: provenance and the published-release freshness check run for
    every release family on every audit, so the path an expired run depends on
    is exercised every week. Without it an expired release run is an error.
    """
    current = now or datetime.now(timezone.utc)
    errors: list[str] = []
    warnings: list[str] = []
    notices: list[str] = []
    for canary in canaries:
        prefix = f"{canary.family} ({canary.repository})"
        workflow_endpoint = (
            f"repos/{canary.repository}/contents/{canary.workflow_path}?ref=main"
        )
        try:
            actual_pin = _extract_pin(fetch_text(workflow_endpoint), canary)
        except (RuntimeError, ValueError) as error:
            errors.append(f"{prefix}: {error}")
            actual_pin = None
        if actual_pin is not None and actual_pin != canary.current_policy_sha:
            errors.append(
                f"{prefix}: current pin {actual_pin} != recorded "
                f"{canary.current_policy_sha}"
            )

        compare_endpoint = (
            "repos/ryanduguid/release-policy/compare/"
            f"main...{canary.current_policy_sha}"
        )
        try:
            comparison = fetch_json(compare_endpoint)
        except RuntimeError as error:
            errors.append(
                f"{prefix}: current pin {canary.current_policy_sha} is not reachable "
                f"from release-policy main: {error}"
            )
        else:
            status = comparison.get("status") if isinstance(comparison, dict) else None
            if status not in _REACHABLE_STATUSES:
                errors.append(
                    f"{prefix}: current pin {canary.current_policy_sha} is not reachable "
                    f"from release-policy main (compare status {status!r})"
                )

        run_endpoint = f"repos/{canary.repository}/actions/runs/{canary.evidence.run_id}"
        run_expired = False
        try:
            raw_run = fetch_json(run_endpoint)
        except RuntimeError as error:
            if getattr(error, "status", None) != 404:
                errors.append(f"{prefix}: could not read evidence run: {error}")
            elif current - _started_at(canary) > RETENTION + EXPIRY_MARGIN:
                run_expired = True
            else:
                errors.append(
                    f"{prefix}: evidence run {canary.evidence.run_id} is missing "
                    f"before its retention period ended (started {canary.evidence.started_at})"
                )
        else:
            # Only a 404 can mark a run expired. Any other body, JSON null
            # included, is not a run record and fails.
            if not isinstance(raw_run, dict):
                errors.append(f"{prefix}: evidence run response is not an object")
            else:
                expected = {
                    "conclusion": "success",
                    "head_branch": canary.evidence.ref,
                    "head_sha": canary.evidence.head_sha,
                    "path": canary.workflow_path,
                    "run_started_at": canary.evidence.started_at,
                }
                for field, value in expected.items():
                    if raw_run.get(field) != value:
                        errors.append(
                            f"{prefix}: evidence run {field} {raw_run.get(field)!r} != {value!r}"
                        )
                observed_evidence_pin = _referenced_policy_sha(raw_run, canary)
                if observed_evidence_pin != canary.evidence.policy_sha:
                    errors.append(
                        f"{prefix}: evidence policy pin {observed_evidence_pin!r} != "
                        f"{canary.evidence.policy_sha}"
                    )

        if canary.family in _RELEASE_FAMILIES and verify_provenance is not None:
            try:
                verify_provenance(canary)
            except RuntimeError as error:
                errors.append(f"{prefix}: release provenance not verified: {error}")
            else:
                if run_expired:
                    notices.append(
                        f"{prefix}: evidence run {canary.evidence.run_id} has expired; "
                        f"release {canary.evidence.ref} and its signed provenance were "
                        "verified instead. The run's start time and conclusion can no "
                        "longer be checked."
                    )

        # A release can be published without a successful run to show for it,
        # so release evidence mode reads the published releases on every audit.
        if canary.family in _RELEASE_FAMILIES and (
            run_expired or verify_provenance is not None
        ):
            errors.extend(_release_freshness_errors(canary, fetch_json, prefix))
        if canary.family in _RELEASE_FAMILIES and verify_provenance is not None:
            tag_errors, tag_notices = _tag_protection(canary, fetch_json, prefix)
            errors.extend(tag_errors)
            notices.extend(tag_notices)
        if run_expired and canary.family in _RELEASE_FAMILIES:
            if verify_provenance is None:
                errors.append(
                    f"{prefix}: evidence run {canary.evidence.run_id} has expired and "
                    "no provenance verifier was supplied"
                )
        else:
            workflow_name = Path(canary.workflow_path).name
            # Page through the successes until the relevant run appears or the
            # pages run out. Reading one page let a namespaced release run hide
            # behind newer successes from other namespaces.
            latest = None
            partial_page_seen = False
            try:
                for page in range(1, MAX_RUN_PAGES + 1):
                    runs_endpoint = (
                        f"repos/{canary.repository}/actions/workflows/{workflow_name}/runs"
                        f"?status=success&per_page=100&page={page}"
                    )
                    payload = fetch_json(runs_endpoint)
                    latest = _latest_relevant_run(payload, canary)
                    runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
                    if latest is not None:
                        break
                    if not isinstance(runs, list) or len(runs) < 100:
                        partial_page_seen = True
                        break
                if latest is None and not partial_page_seen:
                    errors.append(
                        f"{prefix}: more than {MAX_RUN_PAGES} pages of successful runs "
                        "were searched without finding the relevant one"
                    )
            except RuntimeError as error:
                errors.append(f"{prefix}: could not read latest successes: {error}")
                latest = None
            if latest is None and partial_page_seen:
                errors.append(f"{prefix}: no relevant successful workflow run was returned")
            elif latest is None:
                # The pagination-exceeded error above already names this condition;
                # nothing more can be compared without a run.
                pass
            elif canary.family in _RELEASE_FAMILIES:
                if latest.get("id") != canary.evidence.run_id:
                    errors.append(
                        f"{prefix}: latest successful run {latest.get('id')!r} != recorded "
                        f"{canary.evidence.run_id}; "
                        + _review_instruction(
                            canary, f"run {latest.get('id')!r} ({latest.get('head_branch')!r})"
                        )
                    )
            else:
                # The recorded run stays the reviewed example, checked above like any
                # other. Freshness comes from the live listing instead: the newest
                # success has to be running the pin the consumer declares today.
                observed_pin = _referenced_policy_sha(latest, canary)
                if observed_pin != canary.current_policy_sha:
                    errors.append(
                        f"{prefix}: latest successful run {latest.get('id')!r} used policy "
                        f"pin {observed_pin!r} != current {canary.current_policy_sha}"
                    )
                elif run_expired:
                    expiry_errors = _expired_verify_errors(canary, latest, fetch_json, prefix)
                    errors.extend(expiry_errors)
                    if not expiry_errors:
                        notices.append(
                            f"{prefix}: evidence run {canary.evidence.run_id} has expired; "
                            f"current success {latest.get('id')!r} on the current pin "
                            "was verified instead."
                        )

        if (
            canary.family in _RELEASE_FAMILIES
            and canary.evidence.policy_sha != canary.current_policy_sha
        ):
            warnings.append(
                f"{prefix}: current pin lacks release evidence; latest success used "
                f"{canary.evidence.policy_sha}"
            )
    return CheckResult(
        errors=tuple(errors), warnings=tuple(warnings), notices=tuple(notices)
    )


_STATUS_LINE = re.compile(r"HTTP/[0-9.]+ ([0-9]{3})")


def _gh_json(endpoint: str) -> object:
    # --include puts the status line first, so a 404 is read from GitHub's
    # response rather than guessed from error text.
    result = subprocess.run(
        ["gh", "api", "--include", endpoint],
        capture_output=True,
        check=False,
        text=True,
    )
    head, _, body = result.stdout.partition("\n\n")
    match = _STATUS_LINE.match(head)
    status = int(match.group(1)) if match else None
    if result.returncode != 0:
        raise GitHubError(
            result.stderr.strip() or f"gh api failed for {endpoint}", status=status
        )
    try:
        return json.loads(body)
    except json.JSONDecodeError as error:
        raise GitHubError(f"GitHub returned invalid JSON for {endpoint}", status) from error


def _run_gh(arguments: Sequence[str]) -> str:
    result = subprocess.run(
        ["gh", *arguments], capture_output=True, check=False, text=True
    )
    if result.returncode != 0:
        raise GitHubError(result.stderr.strip() or f"gh {arguments[0]} failed")
    return result.stdout


def _tag_commit(repository: str, tag: str) -> str | None:
    target = _gh_json(f"repos/{repository}/git/ref/tags/{tag}")
    for _ in range(5):
        obj = target.get("object") if isinstance(target, dict) else None
        if not isinstance(obj, dict):
            return None
        if obj.get("type") == "commit":
            sha = obj.get("sha")
            return sha if isinstance(sha, str) else None
        if obj.get("type") != "tag":
            return None
        target = _gh_json(f"repos/{repository}/git/tags/{obj.get('sha')}")
    return None


def _verify_provenance(canary: Canary) -> None:
    """Verify the recorded release's signed SLSA provenance, or raise RuntimeError.

    The certificate binds the signer workflow and its commit (the policy pin),
    the source tag and commit, and the run; gh checks the first four against the
    flags below, and the run is matched exactly here. The release and its tag
    are read live, so a later move or deletion fails.
    """
    repository, ref = canary.repository, canary.evidence.ref
    release = _gh_json(f"repos/{repository}/releases/tags/{ref}")
    if (
        not isinstance(release, dict)
        or release.get("draft") is not False
        or release.get("immutable") is not True
    ):
        raise RuntimeError(f"release {ref} is not a published immutable release")
    tag_sha = _tag_commit(repository, ref)
    if tag_sha != canary.evidence.head_sha:
        raise RuntimeError(f"tag {ref} points to {tag_sha!r}, not {canary.evidence.head_sha}")
    assets = [
        asset
        for asset in release.get("assets") or []
        if isinstance(asset, dict) and str(asset.get("name", "")).endswith(".tar.gz")
    ]
    if len(assets) != 1:
        raise RuntimeError(f"release {ref} has {len(assets)} .tar.gz assets, expected 1")
    name = str(assets[0]["name"])
    with tempfile.TemporaryDirectory() as directory:
        _run_gh(
            ["release", "download", ref, "--repo", repository,
             "--pattern", name, "--dir", directory]
        )
        path = Path(directory) / name
        digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != assets[0].get("digest"):
            raise RuntimeError(f"{name} digest {digest} != release {assets[0].get('digest')!r}")
        output = _run_gh(
            [
                "attestation", "verify", str(path),
                "--repo", repository,
                "--signer-workflow",
                f"ryanduguid/release-policy/{_SIGNER_WORKFLOWS[canary.family]}",
                "--signer-digest", canary.evidence.policy_sha,
                "--source-ref", f"refs/tags/{ref}",
                "--source-digest", canary.evidence.head_sha,
                "--predicate-type", SLSA_PROVENANCE,
                "--format", "json",
            ]
        )
    if not _names_run(json.loads(output), repository, canary.evidence.run_id):
        raise RuntimeError(f"no verified attestation of {name} names run {canary.evidence.run_id}")


def _names_run(results: object, repository: str, run_id: int) -> bool:
    """True when a verified certificate's run invocation is exactly this run."""
    expected = re.compile(
        re.escape(f"https://github.com/{repository}/actions/runs/{run_id}/attempts/")
        + r"[1-9][0-9]*\Z"
    )
    for result in results if isinstance(results, list) else []:
        try:
            uri = result["verificationResult"]["signature"]["certificate"]["runInvocationURI"]
        except (KeyError, TypeError):
            continue
        if isinstance(uri, str) and expected.fullmatch(uri):
            return True
    return False


def _gh_text(endpoint: str) -> str:
    result = subprocess.run(
        [
            "gh",
            "api",
            "-H",
            "Accept: application/vnd.github.raw+json",
            endpoint,
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"gh api failed for {endpoint}")
    return result.stdout


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "canaries.json",
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--require-current-evidence", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    canaries = load_manifest(arguments.manifest)
    if not arguments.live:
        print(f"canary manifest valid: {len(canaries)} release families")
        return 0

    result = check_live(
        canaries,
        fetch_json=_gh_json,
        fetch_text=_gh_text,
        verify_provenance=_verify_provenance,
    )
    for notice in result.notices:
        print(f"INFO {notice}")
    for warning in result.warnings:
        print(f"WARN {warning}")
    for error in result.errors:
        print(f"FAIL {error}")
    if result.errors or (arguments.require_current_evidence and result.warnings):
        return 1
    print(f"canary audit passed: {len(canaries)} families")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
