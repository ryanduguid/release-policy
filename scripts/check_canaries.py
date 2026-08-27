"""Validate release-family canary pins and live successful-run evidence."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
from typing import Callable, Sequence


_FULL_SHA = re.compile(r"[0-9a-f]{40}\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_WORKFLOW = re.compile(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml\Z")
_RELEASE_TAG = re.compile(
    r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z"
)
_FAMILIES = {"archive", "python", "skills", "verify"}


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


@dataclass(frozen=True)
class CheckResult:
    errors: tuple[str, ...]
    warnings: tuple[str, ...]


def _mapping(value: object, *, label: str, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{label} must contain exactly {sorted(keys)}")
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
        )
        family = _text(entry["family"], label=f"family[{index}].family")
        if family not in _FAMILIES:
            raise ValueError(f"unsupported canary family: {family}")
        if family in seen:
            raise ValueError(f"duplicate canary family: {family}")
        seen.add(family)

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
        elif _RELEASE_TAG.fullmatch(ref) is None:
            raise ValueError(f"{family} release evidence must use a canonical version tag")

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
                    started_at=_text(
                        evidence_raw["started_at"],
                        label=f"{family}.evidence.started_at",
                    ),
                ),
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
    for run in payload["workflow_runs"]:
        if not isinstance(run, dict) or run.get("conclusion") != "success":
            continue
        branch = run.get("head_branch")
        if canary.family == "verify":
            if branch == "main":
                return run
        elif isinstance(branch, str) and _RELEASE_TAG.fullmatch(branch):
            return run
    return None


def check_live(
    canaries: Sequence[Canary],
    *,
    fetch_json: Callable[[str], object],
    fetch_text: Callable[[str], str],
) -> CheckResult:
    errors: list[str] = []
    warnings: list[str] = []
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

        run_endpoint = f"repos/{canary.repository}/actions/runs/{canary.evidence.run_id}"
        try:
            raw_run = fetch_json(run_endpoint)
        except RuntimeError as error:
            errors.append(f"{prefix}: could not read evidence run: {error}")
            raw_run = None
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

        workflow_name = Path(canary.workflow_path).name
        runs_endpoint = (
            f"repos/{canary.repository}/actions/workflows/{workflow_name}/runs"
            "?status=success&per_page=100"
        )
        try:
            latest = _latest_relevant_run(fetch_json(runs_endpoint), canary)
        except RuntimeError as error:
            errors.append(f"{prefix}: could not read latest successes: {error}")
            latest = None
        if latest is None:
            errors.append(f"{prefix}: no relevant successful workflow run was returned")
        elif latest.get("id") != canary.evidence.run_id:
            errors.append(
                f"{prefix}: latest successful run {latest.get('id')!r} != recorded "
                f"{canary.evidence.run_id}"
            )

        if canary.evidence.policy_sha != canary.current_policy_sha:
            warnings.append(
                f"{prefix}: current pin lacks release evidence; latest success used "
                f"{canary.evidence.policy_sha}"
            )
    return CheckResult(errors=tuple(errors), warnings=tuple(warnings))


def _gh_json(endpoint: str) -> object:
    result = subprocess.run(
        ["gh", "api", endpoint],
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

    result = check_live(canaries, fetch_json=_gh_json, fetch_text=_gh_text)
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
