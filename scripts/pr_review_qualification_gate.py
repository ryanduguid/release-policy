"""Read-only admission for one explicitly approved installed-mode qualification."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pr_review as review

REPO = "ryanduguid/release-policy"
REPO_ID = 1336599635
ACTOR_ID = 152749594
PR = 141
MAIN = "b8d917c2f8ee069d0520daeddfee488a56d2e128"
RUNTIME = "9a6f092cc41fce4044040426bf93baed9bdfa1d8"
BRANCH = "codex/python-314-review-central-q5-20261006"
ACTIVATION = "python-314-policy-141-20261006-q5"
ADMITTED_DATE = "2026-10-11"
WORKFLOW = ".github/workflows/pr-review-central.yml"
WORKFLOW_ID = 373545966
PAID_IDS = (373307813, 373545966, 373530626, 373331462)
BASELINE_HASH = "3d6fa1bd0e22e571427eda5373e7cee38f967ce8ae4f6055591b596b741104aa"
ORDINARY_HASH = "85807aca4874aa74bd87d85ac1aa2ddba105d62d61105b814628e7b8fcb00e08"
HISTORY_KEYS = ("id", "workflow_id", "event", "path", "head_sha", "head_branch",
                "run_attempt", "status", "conclusion", "created_at", "actor", "triggering_actor",
                "repository", "head_repository", "pull_requests", "referenced_workflows")
CONTEXT_KEYS = ("GITHUB_ACTIONS", "GITHUB_EVENT_NAME", "GITHUB_SERVER_URL", "GITHUB_REPOSITORY",
                "GITHUB_REPOSITORY_ID", "GITHUB_ACTOR", "GITHUB_ACTOR_ID", "GITHUB_RUN_ATTEMPT",
                "GITHUB_REF", "GITHUB_SHA", "GITHUB_WORKFLOW_SHA", "GITHUB_WORKFLOW_REF", "GITHUB_RUN_ID")


def history(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One identity projection for hosted admission and all local qualification stages."""
    # pylint: disable=unidiomatic-typecheck
    def positive(value: Any) -> int:
        review.require(type(value) is int and value > 0, "invalid_history_integer")
        return value

    def text(value: Any) -> str:
        review.require(isinstance(value, str) and bool(value), "invalid_history_string")
        return value

    def sha(value: Any) -> str:
        review.require(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{40}", value), "invalid_history_sha")
        return value

    def repository(value: Any, *, association: bool = False) -> Any:
        if value is None:
            return None
        review.require(isinstance(value, dict), "invalid_history_repository")
        full_name = (text(value["url"]).removeprefix("https://api.github.com/repos/")
                     if association else text(value["full_name"]))
        review.require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", full_name),
                       "invalid_history_repository")
        if association:
            review.require(value["url"] == "https://api.github.com/repos/" + full_name
                           and value["name"] == full_name.split("/")[1], "invalid_history_repository")
        return {"id": positive(value["id"]), "full_name": full_name}

    def associations(rows: Any) -> list[dict[str, Any]]:
        review.require(isinstance(rows, list), "invalid_history_associations")
        result = []
        for row in rows:
            record: dict[str, Any] = {"id": positive(row["id"]), "number": positive(row["number"])}
            for side in ("head", "base"):
                record[side] = {"ref": text(row[side]["ref"]), "sha": sha(row[side]["sha"]),
                                "repository": repository(row[side]["repo"], association=True)}
            result.append(record)
        review.require(len({row["id"] for row in result}) == len(result), "duplicate_history_association")
        return sorted(result, key=lambda row: (row["id"], row["number"]))

    review.require(isinstance(runs, list), "invalid_history_list")
    projected = []
    for run in runs:
        record: dict[str, Any] = {key: positive(run[key]) for key in ("id", "workflow_id", "run_attempt")}
        record.update({key: text(run[key]) for key in ("event", "path", "head_branch", "status", "created_at")})
        instant(record["created_at"])
        record["head_sha"] = sha(run["head_sha"])
        conclusion = run["conclusion"]
        review.require(conclusion is None or isinstance(conclusion, str) and bool(conclusion),
                       "invalid_history_conclusion")
        record["conclusion"] = conclusion
        for key in ("actor", "triggering_actor"):
            record[key] = {"id": positive(run[key]["id"]), "login": text(run[key]["login"])}
        record["repository"] = repository(run["repository"])
        review.require(record["repository"] is not None, "invalid_history_repository")
        record["head_repository"] = repository(run["head_repository"])
        record["pull_requests"] = associations(run["pull_requests"])
        references = run["referenced_workflows"]
        review.require(isinstance(references, list), "invalid_history_references")
        record["referenced_workflows"] = []
        for reference in references:
            ref = reference.get("ref")
            review.require(ref is None or isinstance(ref, str) and bool(ref), "invalid_history_ref")
            record["referenced_workflows"].append({"path": text(reference["path"]), "ref": ref,
                                                "sha": sha(reference["sha"])})
        record["referenced_workflows"].sort(key=lambda row: (row["path"], row["ref"] or "", row["sha"]))
        review.require(len({(row["path"], row["ref"], row["sha"]) for row in record["referenced_workflows"]})
                       == len(record["referenced_workflows"]), "duplicate_history_reference")
        projected.append(record)
    review.require(len({row["id"] for row in projected}) == len(projected), "duplicate_history_run")
    return sorted(projected, key=lambda row: row["id"])


def github(path: str) -> Any:
    review.require(path.startswith("repos/" + REPO + "/") and "://" not in path,
                   "invalid_qualification_rest_path")
    raw = subprocess.check_output(["gh", "api", "--hostname", "github.com",
                                  "-H", "X-GitHub-Api-Version: 2026-03-10", path])
    review.require(len(raw) <= review.MAX_RESPONSE_BYTES, "oversized_qualification_response")
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        review.require(len(dict(items)) == len(items), "duplicate_qualification_key")
        return dict(items)
    def constant(_: str) -> Any:
        raise review.ReviewError("nonfinite_qualification_response")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def instant(value: str) -> datetime:
    review.require(isinstance(value, str) and re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:Z|\+00:00)", value),
        "invalid_qualification_interval")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def paid_runs(fetch: review.Fetch) -> list[dict[str, Any]]:
    runs = []
    for workflow_id in PAID_IDS:
        rows = []
        total = None
        for page in range(1, 101):
            response = fetch(f"repos/{REPO}/actions/workflows/{workflow_id}/runs?per_page=100&page={page}")
            batch = response["workflow_runs"]
            count = response["total_count"]
            review.require(isinstance(batch, list) and len(batch) <= 100 and type(count) is int  # pylint: disable=unidiomatic-typecheck
                           and count >= 0 and (total is None or count == total), "invalid_paid_inventory")
            total = count
            rows.extend(batch)
            if len(batch) < 100:
                break
        else:
            raise review.ReviewError("incomplete_paid_inventory")
        review.require(len(rows) == total and all(row["workflow_id"] == workflow_id for row in rows),
                       "incomplete_paid_inventory")
        runs.extend(rows)
    ids = [row["id"] for row in runs]
    review.require(all(type(value) is int and value > 0 for value in ids)  # pylint: disable=unidiomatic-typecheck
                   and len(ids) == len(set(ids)), "invalid_paid_inventory")
    return runs


def admit(event: dict[str, Any], environment: dict[str, str], fetch: review.Fetch,
          workflow_bytes: bytes, *, now: datetime | None = None) -> dict[str, Any]:
    inputs = event["inputs"]
    review.require(set(inputs) == {"pr-number", "activation-id", "caller-sha", "intent-start", "intent-end"}
                   and inputs["pr-number"] == str(PR) and inputs["activation-id"] == ACTIVATION,
                   "unapproved_qualification_inputs")
    caller = inputs["caller-sha"]
    review.require(isinstance(caller, str) and re.fullmatch(r"[a-f0-9]{40}", caller), "invalid_qualification_caller")
    start, end = instant(inputs["intent-start"]), instant(inputs["intent-end"])
    current_time = now or datetime.now(timezone.utc)
    review.require(timedelta(0) < end - start <= timedelta(minutes=2)
                   and start.date().isoformat() == end.date().isoformat() == ADMITTED_DATE
                   and current_time.astimezone(timezone.utc).date().isoformat() == ADMITTED_DATE,
                   "invalid_qualification_interval")
    expected = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": REPO,
                "GITHUB_REPOSITORY_ID": str(REPO_ID), "GITHUB_ACTOR": "ryanduguid",
                "GITHUB_ACTOR_ID": str(ACTOR_ID), "GITHUB_RUN_ATTEMPT": "1",
                "GITHUB_REF": "refs/heads/" + BRANCH, "GITHUB_SHA": caller,
                "GITHUB_WORKFLOW_SHA": caller,
                "GITHUB_WORKFLOW_REF": REPO + "/" + WORKFLOW + "@refs/heads/" + BRANCH}
    review.require(all(environment.get(key) == value for key, value in expected.items()),
                   "invalid_qualification_context")
    raw_run_id = environment.get("GITHUB_RUN_ID", "")
    review.require(re.fullmatch(r"[1-9][0-9]{0,19}", raw_run_id), "invalid_qualification_run")
    run_id = int(raw_run_id)
    prefix = "repos/" + REPO

    def current_state() -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
        run = fetch(prefix + f"/actions/runs/{run_id}")
        review.require(type(run["id"]) is int and run["id"] == run_id  # pylint: disable=unidiomatic-typecheck
                       and run["workflow_id"] == WORKFLOW_ID and run["path"] == WORKFLOW
                       and run["event"] == "workflow_dispatch" and run["head_sha"] == caller
                       and run["head_branch"] == BRANCH and type(run["run_attempt"]) is int  # pylint: disable=unidiomatic-typecheck
                       and run["run_attempt"] == 1 and run["status"] in ("queued", "in_progress")
                       and run["conclusion"] is None, "invalid_qualification_run")
        for key in ("actor", "triggering_actor"):
            review.require(run[key]["login"] == "ryanduguid" and type(run[key]["id"]) is int  # pylint: disable=unidiomatic-typecheck
                           and run[key]["id"] == ACTOR_ID, "invalid_qualification_actor")
        review.require(run["repository"]["full_name"] == REPO and run["repository"]["id"] == REPO_ID
                       and run["repository"]["private"] is False
                       and start <= instant(run["created_at"]) <= end, "invalid_qualification_run")
        pr = review.pr_metadata(fetch, REPO, PR)
        review.require(pr["head"]["sha"] == RUNTIME and pr["base"]["sha"] == MAIN
                       and pr["base"]["ref"] == "main"
                       and all(pr[side]["repo"]["id"] == REPO_ID for side in ("head", "base")),
                       "stale_qualification_source")
        review.require(fetch(prefix + "/git/ref/heads/main")["object"]["sha"] == MAIN
                       and fetch(prefix + "/git/ref/heads/" + BRANCH)["object"]["sha"] == caller,
                       "stale_qualification_source")
        consumer = fetch(prefix + "/actions/workflows/373307813")
        review.require(consumer["id"] == 373307813
                       and consumer["path"] == ".github/workflows/pr-review-consumer.yml"
                       and consumer["state"] == "disabled_manually", "automatic_consumer_enabled")
        central = fetch(prefix + f"/actions/workflows/{WORKFLOW_ID}")
        review.require(central["id"] == WORKFLOW_ID and central["path"] == WORKFLOW
                       and central["state"] == "active", "qualification_workflow_changed")
        runs = paid_runs(fetch)
        previous = [row for row in runs if row["id"] != run_id]
        prior_history = history(previous)
        review.require(review.digest(prior_history) == BASELINE_HASH and all(row["status"] == "completed" for row in previous)
                       and len([row for row in runs if row["id"] == run_id]) == 1,
                       "paid_qualification_history_changed")
        current = next(row for row in runs if row["id"] == run_id)
        review.require(history([current]) == history([run]), "qualification_inventory_changed")
        ordinary = []
        total = None
        for page in range(1, 101):
            response = fetch(prefix + f"/actions/runs?head_sha={RUNTIME}&per_page=100&page={page}")
            batch = response["workflow_runs"]
            count = response["total_count"]
            review.require(isinstance(batch, list) and len(batch) <= 100 and type(count) is int  # pylint: disable=unidiomatic-typecheck
                           and count >= 0 and (total is None or total == count), "invalid_ordinary_inventory")
            total = count
            ordinary.extend(batch)
            if len(batch) < 100:
                break
        else:
            raise review.ReviewError("incomplete_ordinary_inventory")
        ordinary_history = history(ordinary)
        review.require(len(ordinary) == total and review.digest(ordinary_history) == ORDINARY_HASH,
                       "ordinary_qualification_history_changed")
        return run, {"paid": prior_history, "ordinary": ordinary_history}

    first_run, first_history = current_state()
    commit = fetch(prefix + "/git/commits/" + caller)
    review.require(commit["sha"] == caller and [row["sha"] for row in commit["parents"]] == [RUNTIME],
                   "invalid_qualification_parent")
    remote_workflow = review.file_content(fetch, REPO, WORKFLOW, caller).encode()
    review.require(remote_workflow == workflow_bytes
                   and workflow_bytes.count(("/.github/workflows/pr-review.yml@" + RUNTIME).encode()) == 1
                   and b"mode: installed" in workflow_bytes, "qualification_workflow_changed")
    last_run, last_history = current_state()
    review.require(first_history == last_history and history([first_run]) == history([last_run]),
                   "qualification_changed_during_admission")
    return {"schema": 1, "passed": True, "activation_id": ACTIVATION, "repository": REPO,
            "repository_id": REPO_ID, "actor_id": ACTOR_ID, "pr": PR, "head": RUNTIME,
            "main": MAIN, "runtime_sha": RUNTIME, "caller_sha": caller, "caller_branch": BRANCH,
            "workflow_id": WORKFLOW_ID, "workflow_path": WORKFLOW, "run_id": run_id, "run_attempt": 1,
            "intent_start": start.isoformat(), "intent_end": end.isoformat(),
            "created_at": last_run["created_at"], "baseline_paid_sha256": BASELINE_HASH,
            "baseline_ordinary_sha256": ORDINARY_HASH, "admitted_snapshot_date": ADMITTED_DATE,
            "caller_workflow_sha256": hashlib.sha256(workflow_bytes).hexdigest(),
            "gate_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "admitted_at": current_time.isoformat()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        event = review.read_json(Path(os.environ["GITHUB_EVENT_PATH"]))
        workflow = Path(__file__).resolve().parents[1] / WORKFLOW
        environment = {key: os.environ.get(key, "") for key in CONTEXT_KEYS}
        projection = admit(event, environment, github, workflow.read_bytes())
        review.write_json(args.output, projection)
    except (review.ReviewError, KeyError, TypeError, ValueError, OSError):
        print("Qualification admission failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
