"""Sanitised diagnostic receipts. These never authorise a report or another call."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from pr_review import MODELS, ReviewError, canonical, digest, request_json, require

MAX_JOURNAL_BYTES = 131072
_ID = re.compile(r"gen-[0-9A-Za-z-]+", re.ASCII)
_FINISH = {"stop", "length", "content_filter", "error", "tool_calls"}
_CALL_FIELDS = {"model", "provider", "chunk_hash", "transport_state", "output_state", "diagnostic_category",
                "generation_id_sha256", "generation_id_valid", "model_matches", "provider_matches",
                "finish_reason", "usage"}
_REJECTIONS = {"completion_contract", "json_syntax", "duplicate_keys", "root_or_nesting", "strict_schema"}
_LOOKUP_FIELDS = {"state", "total_cost_usd", "provider_latency_ms", "provider_generation_time_ms"}
_LOOKUP_STATES = {"awaiting_response", "not_eligible", "lookup_intended",
                  "attempted_unavailable", "attempted_rejected", "verified"}
MAX_TIMING_MS = 86_400_000
LOOKUP_TIMEOUT_SECONDS = 15


def money(value: Any) -> bool:
    if isinstance(value, int) and not isinstance(value, bool):
        return 0 <= value < 10**24
    return (isinstance(value, Decimal) and value.is_finite()
            and 0 <= value <= Decimal("1.7976931348623157e308")
            and int(value.as_tuple().exponent) >= -324 and len(value.as_tuple().digits) <= 350)


def empty_lookup(state: str) -> dict[str, Any]:
    return {"state": state, "total_cost_usd": None,
            "provider_latency_ms": None, "provider_generation_time_ms": None}


def validate_lookup(call: dict[str, Any]) -> None:
    data = call["generation_metadata"]
    require(isinstance(data, dict) and set(data) == _LOOKUP_FIELDS
            and isinstance(data["state"], str) and data["state"] in _LOOKUP_STATES,
            "invalid_measurement_input")
    state = data["state"]
    if state == "verified":
        cost = data["total_cost_usd"]
        require(isinstance(cost, str) and len(cost) <= 700
                and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]{1,4})?", cost)
                and money(Decimal(cost))
                and all(value is None or (isinstance(value, (int, float, Decimal))
                        and not isinstance(value, bool) and money(Decimal(str(value)))
                        and value <= MAX_TIMING_MS)
                        for key, value in data.items() if key.startswith("provider_")),
                "invalid_measurement_input")
    else:
        require(all(value is None for key, value in data.items() if key != "state"),
                "invalid_measurement_input")
    if state != "awaiting_response":
        require(call["transport_state"] == "response_received", "invalid_measurement_input")
    if state in ("lookup_intended", "attempted_unavailable", "attempted_rejected", "verified"):
        require(call["generation_id_valid"] and call["generation_id_sha256"] is not None,
                "invalid_measurement_input")
    if state == "not_eligible":
        require(not call["generation_id_valid"] and call["generation_id_sha256"] is None,
                "invalid_measurement_input")


def lookup_metadata(identifier: str, model: str, provider: str, key: str,
                    send: Callable[..., Any]) -> dict[str, Any]:
    """One content-free GET, after the original receipt; no inference retry."""
    try:
        result = send("https://openrouter.ai/api/v1/generation?id=" + quote(identifier, safe=""),
                      key, timeout=10, response_limit=65536, decimal_numbers=True)
    except ReviewError as error:
        rejected = str(error) in {"oversized_api_response", "duplicate_api_json_key",
                                 "non_finite_api_json_number"}
        return empty_lookup("attempted_rejected" if rejected else "attempted_unavailable")
    except Exception:
        return empty_lookup("attempted_unavailable")
    result = result.get("data") if isinstance(result, dict) else None
    if (not isinstance(result, dict) or result.get("id") != identifier
            or result.get("model") != model or result.get("provider_name") != provider
            or not {"latency", "generation_time"} <= result.keys()
            or not money(result.get("total_cost"))):
        return empty_lookup("attempted_rejected")
    timings = [result.get(field) for field in ("latency", "generation_time")]
    if any(value is not None and (not money(value) or value > MAX_TIMING_MS) for value in timings):
        return empty_lookup("attempted_rejected")
    return {"state": "verified", "total_cost_usd": str(result["total_cost"]),
            "provider_latency_ms": None if timings[0] is None else float(timings[0]),
            "provider_generation_time_ms": None if timings[1] is None else float(timings[1])}


def generation_metadata(response: Any, model: str, provider: str, key: str, *,
                        send: Callable[..., Any] | None = None) -> dict[str, Any]:
    data = response if isinstance(response, dict) else {}
    identifier = generation_id(data.get("id"))
    if identifier is None:
        return empty_lookup("not_eligible")
    if (("model" in data and data["model"] != model)
            or ("provider" in data and data["provider"] != provider)):
        return empty_lookup("attempted_rejected")
    if send is not None:
        return lookup_metadata(identifier, model, provider, key, send)
    # Run the fixed trusted helper with credentials only on its private stdin.
    # The subprocess deadline includes connection and complete body consumption.
    try:
        process = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--lookup"],
            input=canonical({"id": identifier, "model": model, "provider": provider, "key": key}),
            capture_output=True, timeout=LOOKUP_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
        require(process.returncode == 0 and len(process.stdout) <= 4096, "invalid_lookup_result")
        result = json.loads(process.stdout)
        require(isinstance(result, dict) and result.get("state") in
                ("attempted_unavailable", "attempted_rejected", "verified"), "invalid_lookup_result")
        validate_lookup({"generation_metadata": result, "transport_state": "response_received",
                         "generation_id_valid": True, "generation_id_sha256": digest(identifier)})
        return result
    except Exception:
        return empty_lookup("attempted_unavailable")


def lookup_worker() -> None:
    """The worker emits only the bounded projection, never an error body."""
    try:
        data = json.loads(sys.stdin.buffer.read(1025))
        require(set(data) == {"id", "model", "provider", "key"}
                and generation_id(data["id"]) is not None
                and data["model"] in MODELS
                and data["provider"] == ("Parasail" if data["model"] == MODELS[0] else "Xiaomi")
                and isinstance(data["key"], str) and 0 < len(data["key"]) <= 256,
                "invalid_lookup_input")
        result = lookup_metadata(data["id"], data["model"], data["provider"], data["key"], request_json)
    except Exception:
        result = empty_lookup("attempted_unavailable")
    sys.stdout.buffer.write(canonical(result) + b"\n")


def generation_id(value: Any) -> str | None:
    if (isinstance(value, str) and len(value) <= 128 and _ID.fullmatch(value)
            and "sk-" not in value):
        return value
    return None


def metadata(response: Any, model: str, provider: str, *, output_tokens: int | None = None) -> dict[str, Any]:
    data = response if isinstance(response, dict) else {}
    usage = data.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    numbers: dict[str, Any] = {}
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(field)
        numbers[field] = value if type(value) is int and 0 <= value <= 1_000_000_000 else None  # pylint: disable=unidiomatic-typecheck
    details = usage.get("completion_tokens_details")
    reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
    completion = numbers["completion_tokens"]
    numbers["reasoning_tokens"] = (reasoning if isinstance(reasoning, int) and not isinstance(reasoning, bool)
                                   and output_tokens is not None
                                   and completion is not None and 0 <= reasoning <= min(completion, output_tokens)
                                   else None)
    cost = usage.get("cost")
    # Bound integer representation; retain finite float bills even above the
    # spending ceiling. Diagnostic metadata never releases a reservation.
    numbers["cost"] = cost if ((type(cost) is int and 0 <= cost < 10**24)  # pylint: disable=unidiomatic-typecheck
                               or (type(cost) is float and math.isfinite(cost) and cost >= 0)) else None  # pylint: disable=unidiomatic-typecheck
    choices = data.get("choices")
    finish = choices[0].get("finish_reason") if (isinstance(choices, list) and len(choices) == 1
                                               and isinstance(choices[0], dict)) else None
    identifier = generation_id(data.get("id"))
    return {"generation_id_sha256": hashlib.sha256(identifier.encode("ascii")).hexdigest() if identifier else None,
            "generation_id_valid": identifier is not None,
            "model_matches": data.get("model") == model if "model" in data else None,
            "provider_matches": data.get("provider") == provider if "provider" in data else None,
            "finish_reason": finish if isinstance(finish, str) and finish in _FINISH else None,
            "usage": numbers}


class ReceiptJournal:
    def __init__(self, path: Path | None, snapshot: dict[str, Any], groups: list[list[dict[str, Any]]],
                 policy: dict[str, Any], reservation: float, *, lookup_enabled: bool = False):
        self.path = path
        self.data: dict[str, Any] = {"schema": "receipt_journal_v3",
                                     "context_hash": snapshot["context_hash"],
                                     "reservation_usd": reservation, "finished": False,
                                     "calls": [{"model": model, "provider": policy["routes"][model]["name"],
                                                "chunk_hash": digest(group), "transport_state": "not_started",
                                                "output_state": "not_checked", "diagnostic_category": None,
                                                **metadata(None, model, "")}
                                               for model in MODELS for group in groups]}
        if lookup_enabled:
            self.data["schema"] = "receipt_journal_v4"
            for call in self.data["calls"]:
                call["generation_metadata"] = empty_lookup("awaiting_response")
        require(len(canonical(self.data)) + 1 <= MAX_JOURNAL_BYTES, "receipt_plan_too_large")
        self.save()

    def save(self) -> None:
        fields = _CALL_FIELDS | ({"generation_metadata"} if self.data["schema"] == "receipt_journal_v4" else set())
        require(all(set(call) == fields for call in self.data["calls"]), "invalid_receipt_fields")
        for call in self.data["calls"]:
            if self.data["schema"] == "receipt_journal_v4":
                validate_lookup(call)
            state, category = call["output_state"], call["diagnostic_category"]
            require((state in ("not_checked", "valid") and category is None)
                    or (call["transport_state"] == "response_received" and isinstance(category, str)
                        and ((state == "invalid" and category in _REJECTIONS)
                             or (state == "indeterminate" and category == "unexpected_runtime"))),
                    "invalid_receipt_diagnostic")
        if self.path is None:
            return
        raw = canonical(self.data) + b"\n"
        require(len(raw) <= MAX_JOURNAL_BYTES, "receipt_journal_too_large")
        descriptor, name = tempfile.mkstemp(prefix=".receipt-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            # Windows has no directory fsync; production runs on Linux.
            if hasattr(os, "O_DIRECTORY"):
                directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            Path(name).unlink(missing_ok=True)

    def update(self, index: int, **fields: Any) -> None:
        self.data["calls"][index].update(fields)
        self.data["updated_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.save()

    def finish(self) -> None:
        self.data["finished"] = True
        self.save()


if __name__ == "__main__":
    lookup_worker()
