"""Summarise explicitly selected sanitised receipts without source or model calls."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from decimal import Context, Decimal
from pathlib import Path
from typing import Any

from pr_review import MODELS, ReviewError, require
from pr_review_benchmark import save_manifest as write_projection
from pr_review_receipts import _CALL_FIELDS, _FINISH, _REJECTIONS, MAX_JOURNAL_BYTES

_HASH = re.compile(r"[0-9a-f]{64}", re.ASCII)
_ATTEMPT = re.compile(r"[A-Za-z0-9_-]{1,64}", re.ASCII)
_TRANSPORT = {"not_started", "request_intended", "response_observed", "response_received"}
_MONEY = Context(prec=700)


def money(value: Any) -> bool:
    if type(value) is int:
        return 0 <= value < 10**24
    return (type(value) is Decimal and value.is_finite()
            and 0 <= value <= Decimal("1.7976931348623157e308")
            and int(value.as_tuple().exponent) >= -324 and len(value.as_tuple().digits) <= 350)


def read_receipt(path: Path) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        require(len(pairs) == len(dict(pairs)), "invalid_measurement_input")
        return dict(pairs)

    def constant(_value: str) -> Any:
        raise ReviewError("invalid_measurement_input")

    with path.open("rb") as stream:
        raw = stream.read(MAX_JOURNAL_BYTES + 1)
    require(len(raw) <= MAX_JOURNAL_BYTES, "invalid_measurement_input")
    data = json.loads(raw, parse_float=Decimal, parse_constant=constant, object_pairs_hook=unique)
    require(isinstance(data, dict) and set(data) in (
        {"schema", "context_hash", "reservation_usd", "finished", "calls"},
        {"schema", "context_hash", "reservation_usd", "finished", "calls", "updated_at"}),
        "invalid_measurement_input")
    require(data["schema"] in ("receipt_journal_v2", "receipt_journal_v3")
            and isinstance(data["context_hash"], str) and _HASH.fullmatch(data["context_hash"])
            and money(data["reservation_usd"]) and type(data["finished"]) is bool
            and isinstance(data["calls"], list) and 0 < len(data["calls"]) <= 24
            and len(data["calls"]) % 2 == 0,
            "invalid_measurement_input")
    if "updated_at" in data:
        require(isinstance(data["updated_at"], str)
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", data["updated_at"]),
                "invalid_measurement_input")
    for call in data["calls"]:
        require(isinstance(call, dict) and set(call) == _CALL_FIELDS,
                "invalid_measurement_input")
        require(call["model"] in MODELS
                and call["provider"] == ("Parasail" if call["model"] == MODELS[0] else "Xiaomi")
                and isinstance(call["chunk_hash"], str) and _HASH.fullmatch(call["chunk_hash"])
                and call["transport_state"] in _TRANSPORT
                and call["output_state"] in ("not_checked", "valid", "invalid", "indeterminate")
                and call["finish_reason"] in _FINISH | {None}, "invalid_measurement_input")
        require(type(call["generation_id_valid"]) is bool
                and all(call[field] is None or type(call[field]) is bool
                        for field in ("model_matches", "provider_matches"))
                and (call["generation_id_sha256"] is None
                     or (isinstance(call["generation_id_sha256"], str)
                         and _HASH.fullmatch(call["generation_id_sha256"]))),
                "invalid_measurement_input")
        state, category = call["output_state"], call["diagnostic_category"]
        require((state in ("not_checked", "valid") and category is None)
                or (call["transport_state"] == "response_received"
                    and ((state == "invalid" and category in _REJECTIONS)
                         or (state == "indeterminate" and category == "unexpected_runtime"))),
                "invalid_measurement_input")
        usage = call["usage"]
        fields = {"prompt_tokens", "completion_tokens", "total_tokens", "cost"}
        if data["schema"] == "receipt_journal_v3":
            fields.add("reasoning_tokens")
        require(isinstance(usage, dict) and set(usage) == fields, "invalid_measurement_input")
        require(all(value is None or (type(value) is int and 0 <= value <= 1_000_000_000)
                    for key, value in usage.items() if key != "cost")
                and (usage["cost"] is None or money(usage["cost"])), "invalid_measurement_input")
        if usage.get("reasoning_tokens") is not None:
            require(type(usage["completion_tokens"]) is int
                    and usage["reasoning_tokens"] <= usage["completion_tokens"],
                    "invalid_measurement_input")
        if call["transport_state"] == "not_started":
            require(state == "not_checked" and call["finish_reason"] is None
                    and call["generation_id_sha256"] is None
                    and not any(call[field] for field in
                                ("generation_id_valid", "model_matches", "provider_matches"))
                    and all(value is None for value in usage.values()), "invalid_measurement_input")
        if state == "valid":
            require(call["transport_state"] == "response_received"
                    and call["finish_reason"] == "stop" and call["generation_id_valid"]
                    and call["generation_id_sha256"] is not None
                    and call["model_matches"] and call["provider_matches"]
                    and all(usage[field] is not None for field in
                            ("prompt_tokens", "completion_tokens", "cost")),
                    "invalid_measurement_input")
    half = len(data["calls"]) // 2
    require(all(call["model"] == MODELS[0] for call in data["calls"][:half])
            and all(call["model"] == MODELS[1] for call in data["calls"][half:])
            and [call["chunk_hash"] for call in data["calls"][:half]]
            == [call["chunk_hash"] for call in data["calls"][half:]], "invalid_measurement_input")
    require(not data["finished"] or all(call["output_state"] == "valid" for call in data["calls"]),
            "invalid_measurement_input")
    return data


def ratio(numerator: int, denominator: int) -> dict[str, Any]:
    return {"numerator": numerator, "denominator": denominator,
            "fraction": None if denominator == 0 else numerator / denominator}


def summarise(inputs: list[tuple[str, Path]]) -> dict[str, Any]:
    attempts: dict[str, Any] = {}
    copies = 0
    for identifier, path in inputs:
        require(_ATTEMPT.fullmatch(identifier), "invalid_measurement_attempt")
        data = read_receipt(path)
        if identifier in attempts:
            require(attempts[identifier] == data, "conflicting_measurement_attempt")
            copies += 1
        else:
            attempts[identifier] = data
    require(attempts, "empty_measurement_input")
    models: dict[str, Any] = {}
    generations: Counter[str] = Counter()
    total = Decimal(0)
    unknown = 0
    for model in MODELS:
        counts: Counter[str] = Counter()
        failures: Counter[str] = Counter()
        cost = Decimal(0)
        for journal in attempts.values():
            for call in journal["calls"]:
                if call["model"] != model:
                    continue
                counts["planned"] += 1
                started = call["transport_state"] != "not_started"
                counts["attempted" if started else "unstarted"] += 1
                counts["received"] += call["transport_state"] == "response_received"
                counts[call["output_state"]] += 1
                counts["normal_stop"] += call["finish_reason"] == "stop"
                if call["diagnostic_category"] is not None:
                    failures[call["diagnostic_category"]] += 1
                bill = call["usage"]["cost"]
                if bill is not None:
                    cost = _MONEY.add(cost, Decimal(bill))
                    counts["known_bills"] += 1
                elif started:
                    counts["unknown_bills"] += 1
                identifier = call["generation_id_sha256"]
                if identifier is not None:
                    generations[identifier] += 1
        total = _MONEY.add(total, cost)
        unknown += counts["unknown_bills"]
        models[model] = {"counts": {field: counts[field] for field in (
            "planned", "attempted", "unstarted", "received", "normal_stop", "valid", "invalid",
            "indeterminate", "not_checked", "known_bills", "unknown_bills")},
            "rejections": dict(sorted(failures.items())),
            "known_billed_subtotal_usd": str(cost),
            "accepted_per_planned_call": ratio(counts["valid"], counts["planned"]),
            "accepted_per_received_call": ratio(counts["valid"], counts["received"])}
    paired = sum(set(call["model"] for call in data["calls"]) == set(MODELS)
                 and data["finished"] for data in attempts.values())
    return {"schema": "review_measurements_v1", "attempts": len(attempts),
            "duplicate_evidence_copies": copies, "models": models,
            "fully_completed_pairs": paired, "known_billed_subtotal_usd": str(total),
            "unknown_bills": unknown, "accounting_complete": unknown == 0,
            "repeated_generation_occurrences": sum(count - 1 for count in generations.values()),
            "qualification": "operational_observations_only", "human_accuracy": "not_measured"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", action="append", required=True, metavar="ATTEMPT=PATH",
                        help="One final sanitised receipt per immutable local attempt identity")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        inputs = []
        for specification in args.journal:
            identifier, separator, path = specification.partition("=")
            require(separator and path, "invalid_measurement_input")
            inputs.append((identifier, Path(path)))
        require(all(args.output.resolve() != path.resolve()
                    and (not args.output.exists() or not args.output.samefile(path))
                    for _, path in inputs),
                "measurement_output_overwrites_evidence")
        write_projection(args.output, summarise(inputs))
        print("Operational receipt summary saved; human accuracy is unmeasured")
        return 0
    except Exception:
        print("Measurement stopped; invalid or conflicting evidence", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
