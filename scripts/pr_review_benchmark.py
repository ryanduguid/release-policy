"""Prepare public benchmark snapshots, or run the approved pair for human adjudication."""

from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path
from typing import Any

from pr_review import (
    _FULL_SHA,
    _REPOSITORY,
    collect,
    github,
    read_json,
    require,
    scan_context,
    validate_policy,
    verify_snapshot,
    write_json,
)


def prepare_case(case: dict[str, Any], policy: dict[str, Any], policy_sha: str) -> dict[str, Any]:
    repo, number, base, head = (case[key] for key in ("repository", "pr", "base", "head"))
    require(_REPOSITORY.fullmatch(repo) and _FULL_SHA.fullmatch(base) and _FULL_SHA.fullmatch(head),
            "invalid_benchmark_identity")
    synthetic = case.get("synthetic", False)
    if synthetic:
        comparison = {"merge_base_commit": {"sha": base}, "files": case["files"]}
    else:
        require(github(f"repos/{repo}")["private"] is False, "benchmark_requires_public_source")
        comparison = github(f"repos/{repo}/compare/{base}...{head}?per_page=1")
    require(comparison["merge_base_commit"]["sha"] == base and len(comparison["files"]) < 300,
            "unsupported_benchmark_comparison")
    files = comparison["files"]
    immutable_pr = {"number": number, "state": "open", "draft": False, "changed_files": len(files),
                    "head": {"sha": head}, "base": {"sha": base, "repo": {"full_name": repo, "private": False}}}

    def fetch(path: str) -> Any:
        if path == f"repos/{repo}/pulls/{number}":
            return immutable_pr
        if f"repos/{repo}/pulls/{number}/files?" in path:
            page = int(path.rsplit("=", 1)[1])
            return files[(page - 1) * 100:page * 100]
        if synthetic:
            if "/compare/" in path:
                return comparison
            blob = case["source"]["before" if path.endswith(base) else "after"].encode()
            return {"type": "file", "encoding": "base64", "size": len(blob),
                    "content": base64.b64encode(blob).decode()}
        return github(path)

    # Historical evidence uses the dataset's commits, independent of the PR's
    # current state. It is never published as a live PR status.
    return collect(fetch, repo, number, policy_sha, policy)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--policy-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--gitleaks", default="gitleaks")
    parser.add_argument("--run", action="store_true", help="Make paid calls under the trusted spending policy")
    args = parser.parse_args(argv)
    try:
        policy = read_json(args.policy)
        validate_policy(policy)
        require(0 < args.limit <= 200 and args.offset >= 0, "invalid_benchmark_range")
        cases = read_json(args.cases)["cases"][args.offset:args.offset + args.limit]
        require(cases, "empty_benchmark_range")
        args.output.mkdir(parents=True, exist_ok=True)
        completed = []
        for case in cases:
            path = args.output / f"{case['id']}.snapshot.json"
            # Case IDs are data, not filesystem paths.
            require(case["id"].isalnum(), "invalid_benchmark_case_id")
            if path.exists():
                snap = read_json(path)
            else:
                snap = prepare_case(case, policy, args.policy_sha)
                scan_context(snap, args.gitleaks)
                write_json(path, snap)
            verify_snapshot(snap, policy, args.policy_sha)
            require(snap["repository"] == case["repository"] and snap["pr"] == case["pr"]
                    and snap["head"] == case["head"] and snap["base"] == case["base"], "benchmark_case_mismatch")
            if args.run:
                from pr_review_agent import review_snapshot
                report = review_snapshot(snap, policy)
                write_json(args.output / f"{case['id']}.review.json", report)
            completed.append(case["id"])
        write_json(args.output / "manifest.json", {"cases": completed, "paid_calls_requested": args.run,
                                                    "adjudication": "pending_human_review"})
        print(f"Prepared {len(completed)} cases; human adjudication pending")
        return 0
    except Exception:
        print("Benchmark stopped; incomplete cases cannot count as passed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
