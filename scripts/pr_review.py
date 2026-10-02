"""Freeze PR evidence and validate two independent reviews before publishing a status.

The model transport has no GitHub write operation. The publisher has no model
transport. PR source is fetched through REST at immutable commits and never run.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import math
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

from required_checks import _FULL_SHA, _REPOSITORY

MODELS = ("z-ai/glm-5.3", "xiaomi/mimo-v2.6-pro")
PR_AGENT_SHA = "1d01f24f455bb879c1d9c557ad7de3d72dcc7975"
CONTEXT = "Independent PR review (advisory)"
MAX_RESPONSE_BYTES = 12_000_000
Fetch = Callable[[str], Any]


class ReviewError(Exception):
    """A sanitised failure code; no remote body or source excerpt belongs here."""


def require(condition: Any, code: str) -> None:
    if not condition:
        raise ReviewError(code)


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_bytes(canonical(value) + b"\n")


def read_json(path: Path) -> Any:
    require(path.stat().st_size <= MAX_RESPONSE_BYTES, "oversized_json")
    return json.loads(path.read_bytes())


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: Any, msg: Any,
                         headers: Any, newurl: Any) -> Any:
        raise ReviewError("http_redirect_refused")


def request_json(url: str, key: str, payload: Any = None, *,
                 response_observer: Callable[[], None] | None = None) -> Any:
    """Send only to fixed API origins; never forward a credential through a redirect."""
    require(url.startswith(("https://api.github.com/", "https://openrouter.ai/api/v1/")),
            "untrusted_api_origin")
    headers = {"Accept": "application/json", "User-Agent": "independent-pr-review/1"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = Request(url, data=None if payload is None else canonical(payload), headers=headers)
    try:
        with build_opener(NoRedirect()).open(req, timeout=240) as response:
            if response_observer is not None:
                response_observer()
            body = response.read(MAX_RESPONSE_BYTES + 1)
        require(len(body) <= MAX_RESPONSE_BYTES, "oversized_api_response")
        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            require(len(items) == len(dict(items)), "duplicate_api_json_key")
            return dict(items)

        def constant(value: str) -> None:
            raise ReviewError("non_finite_api_json_number")

        return json.loads(body, object_pairs_hook=pairs, parse_constant=constant)
    except HTTPError as error:
        if response_observer is not None:
            response_observer()
        raise ReviewError(f"http_{error.code}") from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise ReviewError("api_transport_or_json_error") from None


def github(path: str, payload: Any = None) -> Any:
    return request_json("https://api.github.com/" + path, os.environ.get("GH_TOKEN", ""), payload)


def validate_policy(policy: dict[str, Any]) -> None:
    require(policy["schema"] == 1 and policy["mode"] == "advisory", "unsupported_policy")
    require(policy.get("spending_mode") in ("capped", "payg"), "unsupported_spending_mode")
    require(tuple(policy["models"]) == MODELS, "forbidden_model_or_fallback")
    require(policy["private_repositories"] is False, "private_routes_not_qualified")
    for field, ceiling in (("max_pilot_limit_usd", 100), ("max_key_limit_usd", 350), ("max_review_usd", 3),
                           ("max_chunks", 12), ("chunk_bytes", 60000), ("output_tokens", 12288)):
        value = policy[field]
        require(type(value) in (int, float) and math.isfinite(value) and 0 < value <= ceiling,
                "invalid_budget_or_size_limit")
    require(all(type(policy[field]) is int for field in ("max_chunks", "chunk_bytes", "output_tokens")),
            "non_integer_size_limit")
    require(isinstance(policy["instructions"], str) and len(policy["instructions"]) <= 8000,
            "invalid_review_instructions")
    expected = (("parasail/fp8", "Parasail"), ("xiaomi/fp8", "Xiaomi"))
    for model, (slug, name) in zip(MODELS, expected, strict=True):
        route = policy["routes"][model]
        require(route["slug"] == slug and route["name"] == name, "unqualified_provider")
        for field in ("prompt", "completion"):
            require(type(route[field]) in (int, float) and math.isfinite(route[field])
                    and 0 < route[field] <= 5, "invalid_price_ceiling")


def pr_metadata(fetch: Fetch, repo: str, number: int) -> dict[str, Any]:
    require(_REPOSITORY.fullmatch(repo) and type(number) is int and number > 0, "invalid_pr")
    pr = fetch(f"repos/{repo}/pulls/{number}")
    require(pr["base"]["repo"]["full_name"].casefold() == repo.casefold(), "wrong_repository")
    require(pr["state"] == "open" and not pr["draft"], "pr_not_ready")
    require(pr["base"]["repo"]["private"] is False, "private_routes_not_qualified")
    for side in ("head", "base"):
        require(_FULL_SHA.fullmatch(pr[side]["sha"]), "invalid_commit")
    return pr


def identity(repo: str, number: int, pr: dict[str, Any]) -> dict[str, Any]:
    return {"repository": repo, "pr": number, "head": pr["head"]["sha"], "base": pr["base"]["sha"]}


def resolve_event(event: dict[str, Any], event_name: str, repo: str, fetch: Fetch) -> int:
    if event_name == "pull_request_target":
        number = event["number"]
        pr = pr_metadata(fetch, repo, number)
        require(pr["user"]["login"] != "dependabot[bot]", "dependabot_requires_bridge")
        require(pr["user"]["login"].casefold() == repo.split("/")[0].casefold(),
                "external_author_requires_manual_dispatch")
        return number
    if event_name == "workflow_dispatch":
        number = int(event["inputs"]["pr-number"])
        pr_metadata(fetch, repo, number)
        return number
    require(event_name == "workflow_run", "unsupported_trigger")
    run_id = event["workflow_run"]["id"]
    require(type(run_id) is int and run_id > 0, "invalid_trigger_run")
    run = fetch(f"repos/{repo}/actions/runs/{run_id}")
    require(type(run.get("run_attempt")) is int and run["run_attempt"] == 1, "trigger_rerun_refused")
    trigger_path, marker, trigger_ref = run["path"].partition("@")
    require(run["event"] == "pull_request" and run["conclusion"] == "success"
            and trigger_path == ".github/workflows/pr-review-trigger.yml"
            and (not marker or trigger_ref), "untrusted_trigger_run")
    require(_FULL_SHA.fullmatch(run["head_sha"]), "invalid_trigger_commit")
    prs = run.get("pull_requests")
    require(isinstance(prs, list) and len(prs) == 1, "ambiguous_trigger_prs")
    pr = pr_metadata(fetch, repo, prs[0]["number"])
    require(pr["head"]["sha"] == run["head_sha"] and pr["user"]["login"] == "dependabot[bot]",
            "stale_or_non_dependabot_trigger")
    return pr["number"]


def safe_path(path: str) -> None:
    parts = PurePosixPath(path).parts
    require(isinstance(path, str) and path and not path.startswith("/")
            and all(part not in (".", "..") for part in parts)
            and not any(char in path for char in "\\\r\n\x00"), "unsafe_file_path")
    lower = path.lower()
    require(not re.search(r"(?:^|/)(?:\.env(?:\.|$)|\.secrets|credentials|secrets/|private[-_]key)", lower)
            and not lower.endswith((".pem", ".key", ".p12", ".pfx", ".token", ".tokens")),
            "secret_path_refused")


def file_content(fetch: Fetch, repo: str, path: str, sha: str) -> str:
    safe_path(path)
    obj = fetch(f"repos/{repo}/contents/{quote(path, safe='/')}?ref={sha}")
    require(obj["type"] == "file" and obj["encoding"] == "base64"
            and type(obj["size"]) is int and obj["size"] <= 1_000_000, "unreadable_file")
    raw = base64.b64decode(obj["content"].replace("\n", ""), validate=True)
    require(len(raw) == obj["size"] and b"\x00" not in raw, "incomplete_or_binary_file")
    return raw.decode("utf-8")


def verify_patch(before: str, after: str, patch: str) -> None:
    """Prove each hunk and every unchanged gap match the frozen source pair."""
    old, new = before.splitlines(), after.splitlines()
    old_pos = new_pos = 0
    expected_old = expected_new = 0
    in_hunk = False
    for line in patch.splitlines():
        if line.startswith("@@"):
            require(expected_old == expected_new == 0, "incomplete_patch_hunk")
            match: Any = re.fullmatch(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*", line)
            require(match, "invalid_patch_hunk")
            expected_old = int(match[2]) if match[2] is not None else 1
            expected_new = int(match[4]) if match[4] is not None else 1
            old_start = int(match[1]) - (1 if expected_old else 0)
            new_start = int(match[3]) - (1 if expected_new else 0)
            require(old_pos <= old_start <= len(old) and new_pos <= new_start <= len(new)
                    and old[old_pos:old_start] == new[new_pos:new_start], "patch_source_mismatch")
            old_pos, new_pos = old_start, new_start
            in_hunk = True
            continue
        if line == "\\ No newline at end of file":
            continue
        require(in_hunk and line and line[0] in " +-", "invalid_patch_line")
        if line[0] in " -":
            require(expected_old > 0 and old_pos < len(old) and old[old_pos] == line[1:], "patch_source_mismatch")
            old_pos += 1
            expected_old -= 1
        if line[0] in " +":
            require(expected_new > 0 and new_pos < len(new) and new[new_pos] == line[1:], "patch_source_mismatch")
            new_pos += 1
            expected_new -= 1
    require(expected_old == expected_new == 0 and old[old_pos:] == new[new_pos:], "patch_source_mismatch")


def collect(fetch: Fetch, repo: str, number: int, policy_sha: str,
            policy: dict[str, Any]) -> dict[str, Any]:
    validate_policy(policy)
    require(_FULL_SHA.fullmatch(policy_sha), "invalid_policy_commit")
    pr = pr_metadata(fetch, repo, number)
    require(type(pr["changed_files"]) is int and 0 < pr["changed_files"] <= 3000, "unsupported_file_count")
    files: list[dict[str, Any]] = []
    for page in range(1, 31):
        batch = fetch(f"repos/{repo}/pulls/{number}/files?per_page=100&page={page}")
        require(isinstance(batch, list) and len(batch) <= 100, "invalid_files_page")
        files.extend(batch)
        if len(batch) < 100:
            break
    require(len(files) == pr["changed_files"]
            and len({f["filename"] for f in files}) == len(files), "incomplete_files_listing")
    # Inspect all names before reading any potentially excluded source.
    for f in files:
        safe_path(f["filename"])
        if "previous_filename" in f:
            safe_path(f["previous_filename"])
    comparison = fetch(f"repos/{repo}/compare/{pr['base']['sha']}...{pr['head']['sha']}?per_page=1")
    merge_base = comparison["merge_base_commit"]["sha"]
    require(_FULL_SHA.fullmatch(merge_base), "invalid_merge_base")
    frozen = []
    for f in sorted(files, key=lambda f: f["filename"]):
        require(f["status"] in ("added", "modified", "removed", "renamed"), "unsupported_file_change")
        patch = f.get("patch", "")
        require(isinstance(patch, str), "invalid_patch")
        # GitHub can omit or truncate patches. Count changed lines, including
        # source lines whose content begins with another '+' or '-'.
        require(sum(line.startswith("+") for line in patch.splitlines()) == f["additions"]
                and sum(line.startswith("-") for line in patch.splitlines()) == f["deletions"],
                "missing_or_truncated_patch")
        require(patch or f["status"] == "renamed", "unreviewable_non_text_change")
        old = f.get("previous_filename", f["filename"])
        frozen.append({"path": f["filename"], "old_path": old, "status": f["status"], "patch": patch,
                       "before": "" if f["status"] == "added" else file_content(fetch, repo, old, merge_base),
                       "after": "" if f["status"] == "removed" else file_content(fetch, repo, f["filename"], pr["head"]["sha"])})
        verify_patch(frozen[-1]["before"], frozen[-1]["after"], patch)
    current = pr_metadata(fetch, repo, number)
    require(identity(repo, number, pr) == identity(repo, number, current)
            and type(current["changed_files"]) is int and current["changed_files"] == len(frozen),  # pylint: disable=unidiomatic-typecheck
            "revision_changed_during_capture")
    snapshot = {"schema": 1, **identity(repo, number, pr), "policy_sha": policy_sha,
                "policy_hash": digest(policy), "engine_sha": PR_AGENT_SHA, "merge_base": merge_base,
                "scanner_version": "gitleaks/8.30.1", "files": frozen}
    snapshot.update(snapshot_kind="installed_pr_review_v1", publication_capability="status",
                    caller_repository=repo, repository_id=pr["base"]["repo"].get("id"),
                    caller_repository_id=pr["base"]["repo"].get("id"))
    snapshot["date"] = datetime.now(UTC).date().isoformat()
    snapshot["context_hash"] = digest(snapshot)
    return snapshot


def verify_snapshot(snapshot: dict[str, Any], policy: dict[str, Any], policy_sha: str) -> None:
    validate_policy(policy)
    require(snapshot["schema"] == 1 and snapshot["policy_sha"] == policy_sha
            and _FULL_SHA.fullmatch(policy_sha) and snapshot["policy_hash"] == digest(policy)
            and snapshot["engine_sha"] == PR_AGENT_SHA, "snapshot_policy_mismatch")
    require(snapshot["context_hash"] == digest({k: v for k, v in snapshot.items() if k != "context_hash"}),
            "snapshot_hash_mismatch")
    require(_REPOSITORY.fullmatch(snapshot["repository"]) and type(snapshot["pr"]) is int
            and snapshot["pr"] > 0, "invalid_pr")
    require(all(_FULL_SHA.fullmatch(snapshot[side]) for side in ("head", "base", "merge_base")), "invalid_commit")
    require(snapshot["scanner_version"] == "gitleaks/8.30.1", "missing_secret_scan")
    require(snapshot["files"] and len({f["path"] for f in snapshot["files"]}) == len(snapshot["files"]),
            "invalid_snapshot_files")
    for f in snapshot["files"]:
        safe_path(f["path"])
        safe_path(f["old_path"])


def chunks(snapshot: dict[str, Any], policy: dict[str, Any]) -> list[list[dict[str, Any]]]:
    result: list[list[dict[str, Any]]] = []
    group: list[dict[str, Any]] = []
    for f in snapshot["files"]:
        require(len(canonical([f])) <= policy["chunk_bytes"], "file_exceeds_context_budget")
        if group and len(canonical(group + [f])) > policy["chunk_bytes"]:
            result.append(group)
            group = []
        group.append(f)
    result.append(group)
    require(len(result) <= policy["max_chunks"], "review_exceeds_chunk_budget")
    return result


def scan_context(snapshot: dict[str, Any], executable: str) -> None:
    # The binary is installed from a verified pin, with no PR configuration.
    raw_source = "\n".join(f[field] for f in snapshot["files"] for field in ("before", "after", "patch"))
    process = subprocess.run([executable, "stdin", "--no-banner", "--redact", "--exit-code", "1"],
                             input=raw_source.encode(), stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, check=False, timeout=60)
    require(process.returncode == 0, "secret_scan_failed_or_unavailable")


REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["review"],
    "properties": {"review": {
        "type": "object", "additionalProperties": False,
        "required": ["key_issues_to_review", "security_concerns", "merge_recommendation", "risk_level"],
        "properties": {
            "risk_level": {"type": "string", "enum": ["low", "medium", "high"]},
            "merge_recommendation": {"type": "string", "enum": ["safe_to_merge", "merge_with_caution", "changes_required"]},
            "security_concerns": {"type": "string", "minLength": 1},
            "key_issues_to_review": {"type": "array", "maxItems": 12, "items": {
                "type": "object", "additionalProperties": False,
                "required": ["relevant_file", "issue_header", "issue_content", "start_line", "end_line"],
                "properties": {
                    "relevant_file": {"type": "string"},
                    "issue_header": {"type": "string", "pattern": r"^\[P[0-3]\] .+"},
                    "issue_content": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
            }},
        },
    }},
}


def validate_review(review: dict[str, Any], group: list[dict[str, Any]]) -> None:
    require(set(review) == set(REVIEW_SCHEMA["properties"]["review"]["required"]),
            "invalid_review_schema")
    require(isinstance(review["key_issues_to_review"], list) and len(review["key_issues_to_review"]) <= 12,
            "invalid_findings")
    require(isinstance(review["security_concerns"], str) and review["security_concerns"].strip(),
            "missing_security_result")
    require(review["merge_recommendation"] in ("safe_to_merge", "merge_with_caution", "changes_required")
            and review["risk_level"] in ("low", "medium", "high"), "invalid_recommendation")
    files = {f["path"]: max(len(f["before" if f["status"] == "removed" else "after"].splitlines()), 1) for f in group}
    for issue in review["key_issues_to_review"]:
        require(set(issue) == set(REVIEW_SCHEMA["properties"]["review"]["properties"]["key_issues_to_review"]["items"]["required"]),
                "invalid_issue_schema")
        path = issue["relevant_file"]
        require(path in files and type(issue["start_line"]) is int and type(issue["end_line"]) is int
                and 1 <= issue["start_line"] <= issue["end_line"] <= files[path], "invalid_finding_location")
        require(isinstance(issue["issue_header"], str) and re.match(r"\[P[0-3]\] .+", issue["issue_header"])
                and isinstance(issue["issue_content"], str) and 0 < len(issue["issue_content"]) <= 4000,
                "invalid_finding_text")


def clear_review(review: dict[str, Any]) -> bool:
    return (not review["key_issues_to_review"] and review["security_concerns"].strip() == "No"
            and review["merge_recommendation"] == "safe_to_merge" and review["risk_level"] == "low")


def validate_completion(response: dict[str, Any], model: str, route: dict[str, Any]) -> str:
    require(response.get("model") == model and response.get("provider") == route["name"],
            "unexpected_model_or_provider")
    choices: Any = response.get("choices")
    require(isinstance(choices, list) and len(choices) == 1, "invalid_completion_choices")
    require(choices[0].get("finish_reason") == "stop", "incomplete_completion")
    content = choices[0]["message"].get("content")
    require(isinstance(content, str) and content.strip(), "empty_completion")
    require(isinstance(response.get("id"), str) and response["id"], "missing_generation_id")
    usage = response.get("usage", {})
    require(all(type(usage.get(field)) is int and usage[field] > 0
                for field in ("prompt_tokens", "completion_tokens")), "missing_usage")
    require(type(usage.get("cost")) in (int, float) and math.isfinite(usage["cost"])
            and usage["cost"] >= 0, "missing_billed_cost")
    return content


def reserve_budget(key_data: dict[str, Any], policy: dict[str, Any], reservation: float) -> None:
    require(type(reservation) in (int, float) and math.isfinite(reservation)
            and 0 < reservation <= policy["max_review_usd"], "review_budget_exceeded")
    require(policy.get("spending_mode") in ("capped", "payg"), "unsupported_spending_mode")
    data = key_data["data"]
    if policy["spending_mode"] == "payg":
        require("limit" in data and "limit_remaining" in data, "missing_key_budget_metadata")
        limit, remaining = data["limit"], data["limit_remaining"]
        if limit is None:
            require(remaining is None, "inconsistent_key_budget_metadata")
        else:
            require(type(limit) in (int, float) and math.isfinite(limit) and limit > 0,
                    "invalid_key_limit")
            require(type(remaining) in (int, float) and math.isfinite(remaining)
                    and remaining >= reservation, "key_budget_exhausted")
        return
    require("limit_reset" in data and data["limit_reset"] in (None, "monthly"), "unsupported_key_limit_reset")
    require(data.get("include_byok_in_limit") is True, "byok_must_count_towards_key_limit")
    ceiling = policy["max_pilot_limit_usd"] if data["limit_reset"] is None else policy["max_key_limit_usd"]
    require(type(data.get("limit")) in (int, float) and math.isfinite(data["limit"])
            and 0 < data["limit"] <= ceiling, "dedicated_capped_key_required")
    require(type(data.get("limit_remaining")) in (int, float) and math.isfinite(data["limit_remaining"])
            and data["limit_remaining"] >= reservation, "key_budget_exhausted")


def validate_generation_receipts(report: dict[str, Any]) -> None:
    require(report["schema"] == 2, "unsupported_public_report_schema")
    require(set(report) <= {"schema", "context_hash", "engine_sha", "complete", "remaining_files",
                            "failed_chunks", "spending_mode", "reservation_usd", "spent_usd", "reviews"},
            "unexpected_public_report_fields")
    for entry in report["reviews"]:
        require(set(entry) == {"model", "provider", "chunk_hashes", "results", "generations"},
                "unexpected_public_review_fields")
        for generation in entry["generations"]:
            require(set(generation) == {"generation_id_sha256", "finish_reason", "usage"}
                    and isinstance(generation["generation_id_sha256"], str)
                    and re.fullmatch(r"[a-f0-9]{64}", generation["generation_id_sha256"])
                    and generation["finish_reason"] == "stop", "incomplete_generation_receipt")
            usage = generation["usage"]
            require(set(usage) == {"prompt_tokens", "completion_tokens", "cost"}
                    and all(type(usage[field]) is int and usage[field] > 0  # pylint: disable=unidiomatic-typecheck
                            for field in ("prompt_tokens", "completion_tokens"))
                    and type(usage["cost"]) in (int, float) and math.isfinite(usage["cost"])
                    and usage["cost"] >= 0, "invalid_generation_usage")


def write_report(path: Path, report: dict[str, Any]) -> None:
    validate_generation_receipts(report)
    write_json(path, report)


def assess_report(snapshot: dict[str, Any], report: dict[str, Any] | None, policy: dict[str, Any],
                  policy_sha: str, fetch: Fetch, jobs_ok: bool) -> str:
    verify_snapshot(snapshot, policy, policy_sha)
    state = "failure"
    if jobs_ok and report is not None:
        require(report["context_hash"] == snapshot["context_hash"] and report["schema"] == 2
                and report["engine_sha"] == PR_AGENT_SHA
                and report["complete"] is True and report["remaining_files"] == []
                and report["failed_chunks"] == 0, "incomplete_review_report")
        groups = chunks(snapshot, policy)
        reviews = report["reviews"]
        require(len(reviews) == len(MODELS), "missing_independent_review")
        validate_generation_receipts(report)
        generation_ids: list[str] = []
        for entry, model in zip(reviews, MODELS, strict=True):
            require(entry["model"] == model and entry["provider"] == policy["routes"][model]["name"]
                    and entry["chunk_hashes"] == [digest(group) for group in groups]
                    and len(entry["results"]) == len(groups) and len(entry["generations"]) == len(groups),
                    "review_coverage_mismatch")
            generation_ids.extend(g["generation_id_sha256"] for g in entry["generations"])
            for result, group in zip(entry["results"], groups, strict=True):
                validate_review(result, group)
        require(len(generation_ids) == len(set(generation_ids)), "duplicate_generation_id")
        current = pr_metadata(fetch, snapshot["repository"], snapshot["pr"])
        require(identity(snapshot["repository"], snapshot["pr"], current)
                == {k: snapshot[k] for k in ("repository", "pr", "head", "base")}
                and current["base"]["repo"].get("id") == snapshot["repository_id"]
                and type(current["changed_files"]) is int  # pylint: disable=unidiomatic-typecheck
                and current["changed_files"] == len(snapshot["files"]), "stale_review")
        if all(clear_review(result) for entry in reviews for result in entry["results"]):
            state = "success"
    return state


def publish(snapshot: dict[str, Any], report: dict[str, Any] | None, policy: dict[str, Any],
            policy_sha: str, fetch: Fetch, post: Callable[[str, Any], Any], jobs_ok: bool) -> str:
    require(snapshot.get("snapshot_kind") == "installed_pr_review_v1"
            and snapshot.get("publication_capability") == "status"
            and snapshot.get("caller_repository") == snapshot["repository"]
            and type(snapshot.get("repository_id")) is int and snapshot["repository_id"] > 0  # pylint: disable=unidiomatic-typecheck
            and type(snapshot.get("caller_repository_id")) is int  # pylint: disable=unidiomatic-typecheck
            and snapshot["caller_repository_id"] == snapshot["repository_id"], "status_capability_required")
    state = assess_report(snapshot, report, policy, policy_sha, fetch, jobs_ok)
    post(f"repos/{snapshot['repository']}/statuses/{snapshot['head']}",
         {"context": CONTEXT, "state": state,
          "description": "Both reviews complete; inspect findings and CI" if state == "success"
          else "Review findings or incomplete review; inspect this run"})
    return state


def render_summary(report: dict[str, Any]) -> str:
    lines = ["## Independent PR review", "", "Advisory result. CI and human review remain required.", ""]
    for entry in report.get("reviews", []):
        lines.extend([f"### {html.escape(entry['model'])}", ""])
        for result in entry["results"]:
            lines.append(f"{html.escape(result['merge_recommendation'])}; risk: {html.escape(result['risk_level'])}")
            for issue in result["key_issues_to_review"]:
                lines.extend(["", "<pre>" + html.escape(
                    f"{issue['issue_header']} {issue['relevant_file']}:{issue['start_line']}\n{issue['issue_content']}"
                ) + "</pre>"])
            lines.extend(["", "<pre>Security: " + html.escape(result["security_concerns"]) + "</pre>", ""])
    return "\n".join(lines)


def failed_status(minimal: dict[str, Any], repo: str, post: Callable[[str, Any], Any]) -> None:
    require(minimal["repository"] == repo and _REPOSITORY.fullmatch(repo)
            and _FULL_SHA.fullmatch(minimal["head"])
            and minimal.get("snapshot_kind") == "installed_pr_review_v1"
            and minimal.get("publication_capability") == "status"
            and minimal.get("caller_repository") == repo
            and type(minimal.get("repository_id")) is int and minimal["repository_id"] > 0  # pylint: disable=unidiomatic-typecheck
            and type(minimal.get("caller_repository_id")) is int  # pylint: disable=unidiomatic-typecheck
            and minimal["caller_repository_id"] == minimal["repository_id"], "invalid_failure_identity")
    post(f"repos/{repo}/statuses/{minimal['head']}",
         {"state": "failure", "context": CONTEXT, "description": "Review failed or was cancelled; inspect this run"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("capture", "review", "publish", "fail"))
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--policy-sha", required=True)
    parser.add_argument("--snapshot", type=Path, default=Path("snapshot.json"))
    parser.add_argument("--report", type=Path, default=Path("review.json"))
    parser.add_argument("--pr", type=int)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--gitleaks", default="gitleaks")
    parser.add_argument("--jobs-ok", action="store_true")
    parser.add_argument("--publish-pending", action="store_true")
    args = parser.parse_args(argv)
    try:
        attempt = os.environ.get("GITHUB_RUN_ATTEMPT")
        require(attempt == "1" or (attempt is None and os.environ.get("GITHUB_ACTIONS") != "true"),
                "workflow_rerun_requires_fresh_dispatch")
        policy = read_json(args.policy)
        validate_policy(policy)
        if args.command == "fail":
            failed_status(read_json(args.snapshot.with_suffix(".identity.json")), args.repo, github)
        elif args.command == "capture":
            number = args.pr
            if number is None:
                number = resolve_event(read_json(Path(os.environ["GITHUB_EVENT_PATH"])),
                                       os.environ["GITHUB_EVENT_NAME"], args.repo, github)
            pr = pr_metadata(github, args.repo, number)
            require(type(pr["base"]["repo"].get("id")) is int and pr["base"]["repo"]["id"] > 0,  # pylint: disable=unidiomatic-typecheck
                    "invalid_repository_id")
            minimal = identity(args.repo, number, pr)
            minimal.update(snapshot_kind="installed_pr_review_v1", publication_capability="status",
                           caller_repository=args.repo, repository_id=pr["base"]["repo"]["id"],
                           caller_repository_id=pr["base"]["repo"]["id"])
            # Persist the captured head before any operation that may fail.
            write_json(args.snapshot.with_suffix(".identity.json"), minimal)
            if args.publish_pending:
                github(f"repos/{args.repo}/statuses/{pr['head']['sha']}",
                       {"state": "pending", "context": CONTEXT, "description": "Two independent reviews requested"})
            snapshot = collect(github, args.repo, number, args.policy_sha, policy)
            require({key: snapshot[key] for key in minimal} == minimal, "revision_changed_before_capture")
            scan_context(snapshot, args.gitleaks)
            write_json(args.snapshot, snapshot)
        elif args.command == "review":
            snapshot = read_json(args.snapshot)
            verify_snapshot(snapshot, policy, args.policy_sha)
            from pr_review_agent import review_snapshot
            write_report(args.report, review_snapshot(snapshot, policy,
                                                    receipt_path=args.report.with_suffix(".receipts.json")))
        else:
            snapshot = read_json(args.snapshot)
            report = read_json(args.report) if args.report.exists() else None
            state = publish(snapshot, report, policy, args.policy_sha, github, github, args.jobs_ok)
            if report is not None:
                summary = os.environ.get("GITHUB_STEP_SUMMARY")
                if summary:
                    Path(summary).write_text(render_summary(report), encoding="utf-8")
            print(f"Independent PR review: {state} (advisory)")
            return 0 if state == "success" else 1
        return 0
    except Exception as error:
        code = str(error) if isinstance(error, ReviewError) else "invalid_input_or_runtime"
        print(f"Independent PR review failed: {code}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    # The adapter imports this module; share its sanitised exception type.
    import pr_review

    raise SystemExit(pr_review.main())
