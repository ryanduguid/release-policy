"""Bind attribution statuses to one current PR snapshot and completed scan."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

SHA = re.compile(r"[0-9a-f]{40}\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


def snapshot(payload: object, repository: str, number: int) -> dict[str, object]:
    if (
        not isinstance(payload, dict)
        or type(payload.get("number")) is not int
        or payload.get("number") != number
    ):
        raise ValueError("unreadable PR identity")
    result: dict[str, object] = {"number": number}
    for side in ("head", "base"):
        ref = payload.get(side)
        if not isinstance(ref, dict) or not isinstance(ref.get("repo"), dict):
            raise ValueError("unreadable PR revision")
        sha, name = ref.get("sha"), ref["repo"].get("full_name")
        if not isinstance(sha, str) or SHA.fullmatch(sha) is None:
            raise ValueError("invalid PR revision")
        if not isinstance(name, str) or REPOSITORY.fullmatch(name) is None:
            raise ValueError("invalid PR repository")
        result[f"{side}_sha"] = sha
        result[f"{side}_repository"] = name.casefold()
    if result["base_repository"] != repository.casefold():
        raise ValueError("wrong base repository")
    base_ref = payload["base"].get("ref")
    title, body = payload.get("title"), payload.get("body")
    if not isinstance(base_ref, str) or not base_ref or not isinstance(title, str):
        raise ValueError("unreadable PR metadata")
    if body is not None and not isinstance(body, str):
        raise ValueError("unreadable PR body")
    result.update(base_ref=base_ref, title=title, body=body or "")
    return result


def fingerprint(value: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def prepare(
    repository: str,
    number: int,
    path: Path,
    fetch: Callable[[str], object],
    post: Callable[[str, str], None],
) -> None:
    value = snapshot(fetch(f"repos/{repository}/pulls/{number}"), repository, number)
    # The runner owns this private file; PR files never supply it.
    with os.fdopen(
        os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8"
    ) as handle:
        json.dump(value, handle)
    post(str(value["head_sha"]), "pending")


def finish(
    repository: str,
    number: int,
    path: Path,
    result_path: Path,
    job_status: str,
    fetch: Callable[[str], object],
    post: Callable[[str, str], None],
) -> bool:
    value = json.loads(path.read_text(encoding="utf-8"))
    head = value.get("head_sha") if isinstance(value, dict) else None
    if not isinstance(head, str) or SHA.fullmatch(head) is None:
        raise ValueError("unreadable owned snapshot")
    clean = False
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
        current = snapshot(fetch(f"repos/{repository}/pulls/{number}"), repository, number)
        clean = (
            job_status == "success"
            and current == value
            and isinstance(result, dict)
            and result.get("clean") is True
            and result.get("head_sha") == head
            and result.get("fingerprint") == fingerprint(value)
        )
    except (OSError, ValueError, RuntimeError):
        # Close an owned pending status even when the scan or freshness read failed.
        pass
    post(head, "success" if clean else "failure")
    return clean


def gh_json(endpoint: str) -> object:
    result = subprocess.run(["gh", "api", endpoint], capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError("GitHub read failed")
    try:
        return json.loads(result.stdout)
    except ValueError as error:
        raise RuntimeError("GitHub response was unreadable") from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "finish"))
    parser.add_argument("--repository", required=True)
    parser.add_argument("--number", required=True, type=int)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--job-status", default="failure")
    parser.add_argument("--run-url", required=True)
    args = parser.parse_args()
    if REPOSITORY.fullmatch(args.repository) is None or args.number < 1:
        print("Invalid policy identity", file=sys.stderr)
        return 1

    def post(head: str, state: str) -> None:
        result = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{args.repository}/statuses/{head}",
                "--method",
                "POST",
                "-f",
                f"state={state}",
                "-f",
                "context=Attribution policy",
                "-f",
                "description=PR snapshot and attribution validation",
                "-f",
                f"target_url={args.run_url}",
                "--silent",
            ],
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise RuntimeError("GitHub status write failed")

    try:
        if args.operation == "prepare":
            prepare(args.repository, args.number, args.snapshot, gh_json, post)
        elif not finish(
            args.repository, args.number, args.snapshot, args.result, args.job_status, gh_json, post
        ):
            print("Attribution scan or PR freshness validation failed", file=sys.stderr)
            return 1
    except (OSError, ValueError, RuntimeError):
        print("Attribution status validation could not complete", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
