# Copyright 2026 Ryan Duguid
# SPDX-License-Identifier: LicenseRef-Duguid-Public-Attribution-1.0

"""Sanitised diagnostic receipts. These never authorise a report or another call."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pr_review import MODELS, call_cost, canonical, digest, guard_amount, require, standard_rate

MAX_JOURNAL_BYTES = 131072
_ID = re.compile(r"msg_[0-9A-Za-z_-]+", re.ASCII)
_STOP = {"end_turn", "max_tokens", "stop_sequence", "tool_use", "pause_turn", "refusal",
         "model_context_window_exceeded"}
_CALL_FIELDS = {"model", "chunk_hash", "transport_state", "output_state", "diagnostic_category",
                "generation_id_sha256", "generation_id_valid", "model_matches", "stop_reason", "usage"}
_REJECTIONS = {"completion_contract", "json_syntax", "duplicate_keys", "root_or_nesting", "strict_schema"}


def money(value: Any) -> bool:
    if isinstance(value, int) and not isinstance(value, bool):
        return 0 <= value < 10**24
    return (isinstance(value, Decimal) and value.is_finite()
            and 0 <= value <= Decimal("1.7976931348623157e308")
            and int(value.as_tuple().exponent) >= -324 and len(value.as_tuple().digits) <= 350)


def generation_id(value: Any) -> str | None:
    if (isinstance(value, str) and len(value) <= 128 and _ID.fullmatch(value)
            and "sk-" not in value):
        return value
    return None


def metadata(response: Any, model: str, prices: dict[str, Any], *,
             output_tokens: int | None = None) -> dict[str, Any]:
    data = response if isinstance(response, dict) else {}
    usage = data.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    numbers: dict[str, Any] = {}
    for field in ("input_tokens", "output_tokens"):
        value = usage.get(field)
        numbers[field] = value if type(value) is int and 0 <= value <= 1_000_000_000 else None  # pylint: disable=unidiomatic-typecheck
    details = usage.get("output_tokens_details")
    thinking = details.get("thinking_tokens") if isinstance(details, dict) else None
    output = numbers["output_tokens"]
    numbers["thinking_tokens"] = (thinking if isinstance(thinking, int) and not isinstance(thinking, bool)
                                  and output_tokens is not None
                                  and output is not None and 0 <= thinking <= min(output, output_tokens)
                                  else None)
    # A missing count or another billing rate leaves the cost unknown, never zero.
    numbers["cost"] = (guard_amount(call_cost(numbers, prices)) if numbers["input_tokens"] is not None
                       and output is not None and standard_rate(usage) else None)
    stop = data.get("stop_reason")
    identifier = generation_id(data.get("id"))
    return {"generation_id_sha256": hashlib.sha256(identifier.encode("ascii")).hexdigest() if identifier else None,
            "generation_id_valid": identifier is not None,
            "model_matches": data.get("model") == model if "model" in data else None,
            "stop_reason": stop if isinstance(stop, str) and stop in _STOP else None,
            "usage": numbers}


class ReceiptJournal:
    def __init__(self, path: Path | None, snapshot: dict[str, Any], groups: list[list[dict[str, Any]]],
                 reservation: float):
        self.path = path
        self.data: dict[str, Any] = {"schema": "receipt_journal_v5",
                                     "context_hash": snapshot["context_hash"],
                                     "reservation_usd": reservation, "finished": False,
                                     "calls": [{"model": model, "chunk_hash": digest(group),
                                                "transport_state": "not_started",
                                                "output_state": "not_checked", "diagnostic_category": None,
                                                **metadata(None, model, {})}
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
