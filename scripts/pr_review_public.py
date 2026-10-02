"""Capture and report Ryan's public PRs without authenticated GitHub requests."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any

from pr_review import (
    _FULL_SHA,
    Fetch,
    ReviewError,
    assess_report,
    collect,
    digest,
    pr_metadata,
    read_json,
    render_summary,
    request_json,
    require,
    scan_context,
    validate_policy,
    verify_snapshot,
    write_json,
)

POLICY_REPOSITORY = "ryanduguid/release-policy"
POLICY_REPOSITORY_ID = 1336599635
OWNER_ID = 152749594
MODE = "central_public_report_v1"
WORKFLOW_REF = POLICY_REPOSITORY + "/.github/workflows/pr-review-central.yml@refs/heads/main"
_URL = re.compile(r"https://github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/"
                  r"([A-Za-z0-9_.-]{1,100})/pull/([1-9][0-9]{0,9})", re.ASCII)


def parse_url(url: str) -> tuple[str, int]:
    require(isinstance(url, str) and len(url) <= 512, "invalid_public_pr_url")
    match = _URL.fullmatch(url)
    if match is None or match[2] in (".", "..") or int(match[3]) > 2147483647:
        raise ReviewError("invalid_public_pr_url")
    return f"{match[1]}/{match[2]}", int(match[3])


def caller_context() -> dict[str, str]:
    expected = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_REPOSITORY": POLICY_REPOSITORY,
                "GITHUB_REPOSITORY_ID": str(POLICY_REPOSITORY_ID),
                "GITHUB_REPOSITORY_OWNER_ID": str(OWNER_ID), "GITHUB_ACTOR_ID": str(OWNER_ID),
                "GITHUB_REF": "refs/heads/main", "GITHUB_RUN_ATTEMPT": "1",
                "GITHUB_WORKFLOW_REF": WORKFLOW_REF}
    require(all(os.environ.get(key) == value for key, value in expected.items()),
            "untrusted_central_dispatch")
    sha, run = os.environ.get("GITHUB_WORKFLOW_SHA", ""), os.environ.get("GITHUB_RUN_ID", "")
    require(_FULL_SHA.fullmatch(sha) and re.fullmatch(r"[1-9][0-9]{0,19}", run),
            "invalid_caller_identity")
    return {"workflow_ref": WORKFLOW_REF, "workflow_sha": sha, "run_id": run}


def anonymous(path: str) -> Any:
    # Paths are constructed locally from validated names, numbers and commits.
    # No target API request can carry the policy repository's token.
    return request_json("https://api.github.com/" + path, "")


def public_identity(fetch: Fetch, repo: str, number: int) -> dict[str, Any]:
    repository = fetch(f"repos/{repo}")
    canonical = repository["full_name"]
    require(parse_url(f"https://github.com/{canonical}/pull/{number}")[0].casefold() == repo.casefold()
            and repository["private"] is False and type(repository["id"]) is int
            and 0 < repository["id"] != POLICY_REPOSITORY_ID, "public_target_required")
    pr = pr_metadata(fetch, canonical, number)
    require(pr["number"] == number and pr["user"]["type"] == "User"
            and type(pr["user"]["id"]) is int and pr["user"]["id"] == OWNER_ID,
            "central_author_required")
    bound = {"repository": canonical, "repository_id": repository["id"], "author_id": OWNER_ID}
    for side in ("head", "base"):
        source = pr[side]["repo"]
        require(source and source["private"] is False and type(source["id"]) is int
                and source["id"] > 0, "public_source_repository_required")
        bound[side + "_repository_id"] = source["id"]
        bound[side] = pr[side]["sha"]
    require(bound["base_repository_id"] == repository["id"], "target_repository_mismatch")
    return bound


def capture(url: str, policy: dict[str, Any], policy_sha: str, scanner: str,
            fetch: Fetch = anonymous) -> dict[str, Any]:
    caller = caller_context()
    repo, number = parse_url(url)
    initial = public_identity(fetch, repo, number)
    repo = initial["repository"]
    snapshot = collect(fetch, repo, number, policy_sha, policy)
    require(initial == public_identity(fetch, repo, number)
            and all(snapshot[side] == initial[side] for side in ("head", "base")),
            "central_identity_changed")
    snapshot.update(snapshot_kind=MODE, publication_capability="report_only",
                    caller_repository=POLICY_REPOSITORY, caller=caller, target_identity=initial)
    snapshot["context_hash"] = digest({key: value for key, value in snapshot.items()
                                       if key != "context_hash"})
    scan_context(snapshot, scanner)
    return snapshot


def verify_live(snapshot: dict[str, Any], policy: dict[str, Any], policy_sha: str,
                fetch: Fetch = anonymous) -> None:
    require(snapshot.get("snapshot_kind") == MODE
            and snapshot.get("publication_capability") == "report_only"
            and snapshot.get("caller_repository") == POLICY_REPOSITORY,
            "central_report_capability_required")
    verify_snapshot(snapshot, policy, policy_sha)
    require(snapshot["caller"] == caller_context(), "central_caller_changed")
    current = public_identity(fetch, snapshot["repository"], snapshot["pr"])
    require(current == snapshot["target_identity"]
            and all(current[side] == snapshot[side] for side in ("head", "base")),
            "central_identity_changed")


def summary(snapshot: dict[str, Any], report: dict[str, Any] | None, policy: dict[str, Any],
            policy_sha: str, fetch: Fetch = anonymous) -> str:
    verify_live(snapshot, policy, policy_sha, fetch)
    url = f"https://github.com/{snapshot['repository']}/pull/{snapshot['pr']}"
    lines = [f"Public PR: [{snapshot['repository']}#{snapshot['pr']}]({url})", "",
             f"Head: `{snapshot['head']}`; base: `{snapshot['base']}`.", ""]
    if report is None:
        return "\n".join(lines + ["Review incomplete. No verdict is available.", "",
                                  "Inspect the failed job and its receipt artefact. Missing bills remain unknown."])
    state = assess_report(snapshot, report, policy, policy_sha, fetch, True)
    lines += ["Both reviews completed with no findings." if state == "success"
              else "Both reviews completed. Inspect findings and cautious verdicts.", "",
              render_summary(report)]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("gate", "capture", "summary"))
    parser.add_argument("--mode", default=MODE)
    parser.add_argument("--url", default="")
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--policy-sha", required=True)
    parser.add_argument("--snapshot", type=Path, default=Path("evidence/snapshot.json"))
    parser.add_argument("--report", type=Path, default=Path("evidence/review.json"))
    parser.add_argument("--gitleaks", default="gitleaks")
    args = parser.parse_args(argv)
    try:
        require(args.mode == MODE, "unknown_review_mode")
        caller_context()
        policy = read_json(args.policy)
        validate_policy(policy)
        require(_FULL_SHA.fullmatch(args.policy_sha), "invalid_policy_sha")
        if args.command == "summary":
            text = summary(read_json(args.snapshot), read_json(args.report) if args.report.exists() else None,
                           policy, args.policy_sha)
            Path(os.environ["GITHUB_STEP_SUMMARY"]).write_text(text, encoding="utf-8")
        else:
            parse_url(args.url)
            if args.command == "capture":
                write_json(args.snapshot, capture(args.url, policy, args.policy_sha, args.gitleaks))
        return 0
    except Exception as error:
        code = str(error) if isinstance(error, ReviewError) else "invalid_input_or_runtime"
        print(f"Public PR review failed: {code}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
