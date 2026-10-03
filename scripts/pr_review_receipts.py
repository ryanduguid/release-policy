"""Sanitised diagnostic receipts. These never authorise a report or another call."""

from __future__ import annotations

import hashlib
import math
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pr_review import MODELS, canonical, digest, require

MAX_JOURNAL_BYTES = 131072
_ID = re.compile(r"gen-[0-9A-Za-z-]+", re.ASCII)
_FINISH = {"stop", "length", "content_filter", "error", "tool_calls"}
_CALL_FIELDS = {"model", "provider", "chunk_hash", "transport_state", "output_state", "diagnostic_category",
                "generation_id_sha256", "generation_id_valid", "model_matches", "provider_matches",
                "finish_reason", "usage"}
_REJECTIONS = {"completion_contract", "json_syntax", "duplicate_keys", "root_or_nesting", "strict_schema"}


def generation_id(value: Any) -> str | None:
    if (isinstance(value, str) and len(value) <= 128 and _ID.fullmatch(value)
            and "sk-" not in value):
        return value
    return None


def metadata(response: Any, model: str, provider: str) -> dict[str, Any]:
    data = response if isinstance(response, dict) else {}
    usage = data.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    numbers: dict[str, Any] = {}
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(field)
        numbers[field] = value if type(value) is int and 0 <= value <= 1_000_000_000 else None  # pylint: disable=unidiomatic-typecheck
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
                 policy: dict[str, Any], reservation: float):
        self.path = path
        self.data: dict[str, Any] = {"schema": "receipt_journal_v2",
                                     "context_hash": snapshot["context_hash"],
                                     "reservation_usd": reservation, "finished": False,
                                     "calls": [{"model": model, "provider": policy["routes"][model]["name"],
                                                "chunk_hash": digest(group), "transport_state": "not_started",
                                                "output_state": "not_checked", "diagnostic_category": None,
                                                **metadata(None, model, "")}
                                               for model in MODELS for group in groups]}
        require(len(canonical(self.data)) + 1 <= MAX_JOURNAL_BYTES, "receipt_plan_too_large")
        self.save()

    def save(self) -> None:
        require(all(set(call) == _CALL_FIELDS for call in self.data["calls"]), "invalid_receipt_fields")
        for call in self.data["calls"]:
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
