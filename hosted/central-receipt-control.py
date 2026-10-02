"""Model-free hosted control for the real review CLI's failure journal."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

root = Path(sys.argv[1]).resolve()
output = Path(sys.argv[2]).resolve()
sys.path.insert(0, str(root / "tests"))
sys.path.insert(0, str(root / "scripts"))
from test_pr_review import POLICY, POLICY_SHA, agent, completion, review, snapshot  # noqa: E402

output.mkdir(parents=True, exist_ok=True)
policy_path, snap_path = output / "policy.json", output / "snapshot.json"
report_path = output / "review.json"
review.write_json(policy_path, POLICY)
review.write_json(snap_path, snapshot())
original = agent.review_snapshot
calls = []


def send(url, key, body=None, *, response_observer=None):
    review.require(key == "synthetic-control", "fixture_key_required")
    if url == "https://openrouter.ai/api/v1/key":
        return {"data": {"limit": None, "limit_remaining": None}}
    review.require(url == "https://openrouter.ai/api/v1/chat/completions", "fixture_origin_required")
    response_observer()
    calls.append(body["model"])
    response = completion(body["model"], id=f"gen-fixture-{len(calls)}")
    response["usage"]["cost"] = 0
    if len(calls) == 2:
        response["choices"][0]["message"]["content"] = "malformed synthetic content"
    return response


def run_snapshot(snap, policy, *, receipt_path=None):
    prompts = {model: [("system", "synthetic context")] for model in review.MODELS}
    return original(snap, policy, send, lambda *args: prompts,
                    lambda text: json.loads(text)["review"], receipt_path=receipt_path)


with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "synthetic-control", "GITHUB_RUN_ATTEMPT": "1"}), \
        mock.patch.object(agent, "review_snapshot", side_effect=run_snapshot):
    result = review.main(["review", "--policy", str(policy_path), "--policy-sha", POLICY_SHA,
                          "--snapshot", str(snap_path), "--report", str(report_path)])
data = review.read_json(report_path.with_suffix(".receipts.json"))
review.require(result == 1 and calls == list(review.MODELS) and not report_path.exists()
               and data["finished"] is False
               and [call["output_state"] for call in data["calls"]] == ["valid", "invalid"]
               and [call["usage"]["cost"] for call in data["calls"]] == [0, 0]
               and all(call["generation_id_valid"] for call in data["calls"])
               and all(f"gen-fixture-{i}" not in report_path.with_suffix(".receipts.json").read_text()
                       for i in (1, 2)), "fixture_failed")
print("Expected malformed completion: two retained synthetic receipts, zero network calls, no full report")
raise SystemExit(result)
