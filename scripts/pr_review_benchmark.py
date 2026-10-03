"""Prepare public benchmark snapshots, or run the approved pair for human adjudication."""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from pr_review import (
    _FULL_SHA,
    _REPOSITORY,
    PR_AGENT_SHA,
    REVIEW_SCHEMA,
    canonical,
    collect,
    digest,
    github,
    read_json,
    require,
    scan_context,
    validate_policy,
    verify_snapshot,
    write_json,
    write_report,
)


def save_manifest(path: Path, manifest: dict[str, Any]) -> None:
    """Keep the last complete projection if a later write is interrupted."""
    descriptor, name = tempfile.mkstemp(prefix=".batch-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical(manifest) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


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
    snapshot = collect(fetch, repo, number, policy_sha, policy)
    snapshot.update(snapshot_kind="benchmark_v1", publication_capability="none")
    snapshot["context_hash"] = digest({key: value for key, value in snapshot.items()
                                       if key != "context_hash"})
    return snapshot


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
    manifest: dict[str, Any] | None = None
    current: dict[str, Any] | None = None
    stage = "selection"
    try:
        args.output.mkdir(parents=True, exist_ok=True)
        manifest_path = args.output / "manifest.json"
        previous = read_json(manifest_path) if manifest_path.exists() else None
        require(previous is None or previous.get("state") == "prepared",
                "benchmark_requires_fresh_output")
        draft = {"schema": "benchmark_batch_v2", "batch_id": uuid.uuid4().hex,
                    "state": "selecting", "finished": False, "records": [], "cases": [],
                    "paid_calls_requested": args.run, "adjudication": "pending_human_review",
                    "stop_after_first_failure": True, "automatic_resume": False}
        if previous is None:
            manifest = draft
        policy = read_json(args.policy)
        validate_policy(policy)
        require(0 < args.limit <= 200 and args.offset >= 0, "invalid_benchmark_range")
        case_manifest = read_json(args.cases)
        cases = case_manifest["cases"][args.offset:args.offset + args.limit]
        require(cases, "empty_benchmark_range")
        for case in cases:
            require(isinstance(case["id"], str) and case["id"].isascii()
                    and case["id"].isalnum(), "invalid_benchmark_case_id")
            require(_REPOSITORY.fullmatch(case["repository"])
                    and type(case["pr"]) is int and case["pr"] > 0  # pylint: disable=unidiomatic-typecheck
                    and _FULL_SHA.fullmatch(case["head"]) and _FULL_SHA.fullmatch(case["base"]),
                    "invalid_benchmark_identity")
        require(len({case["id"] for case in cases}) == len(cases), "duplicate_benchmark_case_id")
        candidate = {"policy": policy, "schema": REVIEW_SCHEMA, "engine_sha": PR_AGENT_SHA,
                     "source_sha256": {name: hashlib.sha256(
                         Path(__file__).with_name(name).read_bytes()).hexdigest()
                         for name in ("pr_review.py", "pr_review_agent.py", "pr_review_receipts.py")}}
        draft.update(state="running", policy_sha=args.policy_sha,
                        candidate_sha256=digest(candidate), benchmark_sha256=digest(case_manifest),
                        qualification="local_only", models=policy["models"],
                        records=[{**{key: case[key] for key in ("id", "repository", "pr", "base", "head")},
                                  "state": "unstarted"} for case in cases])
        if previous is not None:
            require(previous["candidate_sha256"] == draft["candidate_sha256"]
                    and previous["benchmark_sha256"] == draft["benchmark_sha256"]
                    and previous["policy_sha"] == args.policy_sha
                    and [record["id"] for record in previous["records"]] == [case["id"] for case in cases],
                    "benchmark_configuration_changed")
        manifest = draft
        save_manifest(manifest_path, manifest)
        for case, current in zip(cases, manifest["records"], strict=True):
            stage = "preparation"
            current["state"] = "preparing"
            save_manifest(manifest_path, manifest)
            path = args.output / f"{case['id']}.snapshot.json"
            # Case IDs are data, not filesystem paths.
            if path.exists():
                snap = read_json(path)
            else:
                snap = prepare_case(case, policy, args.policy_sha)
                scan_context(snap, args.gitleaks)
                write_json(path, snap)
            verify_snapshot(snap, policy, args.policy_sha)
            require(snap["repository"] == case["repository"] and snap["pr"] == case["pr"]
                    and snap["head"] == case["head"] and snap["base"] == case["base"], "benchmark_case_mismatch")
            current.update(state="prepared", context_hash=snap["context_hash"])
            save_manifest(manifest_path, manifest)
            if args.run:
                from pr_review_agent import review_snapshot
                from pr_review_receipts import generation_metadata
                stage = "model"
                current["state"] = "reviewing"
                save_manifest(manifest_path, manifest)
                report = review_snapshot(snap, policy,
                                         receipt_path=args.output / f"{case['id']}.review.receipts.json",
                                         metadata_lookup=generation_metadata)
                stage = "report"
                write_report(args.output / f"{case['id']}.review.json", report)
                current.update(state="complete_report", report_sha256=digest(report))
            manifest["cases"].append(case["id"])
            save_manifest(manifest_path, manifest)
        manifest.update(state="complete" if args.run else "prepared", finished=True)
        save_manifest(manifest_path, manifest)
        print(f"Prepared {len(manifest['cases'])} cases; human adjudication pending")
        return 0
    except Exception:
        if manifest is not None:
            manifest.update(state="failed", finished=False, stop_stage=stage)
            if current is not None:
                current["state"] = {"preparation": "preparation_failed", "model": "model_failed",
                                    "report": "report_failed"}[stage]
            try:
                save_manifest(args.output / "manifest.json", manifest)
            except Exception:
                # A prior durable 'preparing' or 'reviewing' checkpoint remains
                # interrupted evidence; storage failure cannot become success.
                print("Batch evidence write failed; prior checkpoint is incomplete", file=sys.stderr)
        print("Benchmark stopped; incomplete cases cannot count as passed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
