# Copyright 2026 Ryan Duguid
# SPDX-License-Identifier: LicenseRef-Duguid-Public-Attribution-1.0

"""A bounded /review adapter for the pinned PR-Agent engine.

Only PR-Agent's review prompt, token budget and structured output model are
used. Provider discovery, source capture, spending and publication are owned
by the trusted policy. No repository settings or fallback dispatcher runs.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
from decimal import Decimal, localcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from pr_review import (
    _MONEY,
    ANTHROPIC_API,
    MODELS,
    PR_AGENT_SHA,
    REVIEW_SCHEMA,
    ReviewError,
    call_cost,
    canonical,
    chunks,
    digest,
    guard_amount,
    request_json,
    require,
    validate_completion,
    validate_review,
)

# Fixed context ceiling. The byte-based input allowance is checked against
# Anthropic's token counter before any paid call and against usage after it.
CONTEXT_TOKENS = 1_000_000
# Structured outputs refuse these keywords with a 400; validate_review and the
# native model still enforce every one of them on each response.
_UNSUPPORTED_SCHEMA_KEYWORDS = {"minLength", "maxLength", "maxItems", "minimum"}


def input_token_bound(body: dict[str, Any]) -> int:
    bound = len(canonical(body)) + 1000
    require(bound + body["max_tokens"] <= CONTEXT_TOKENS, "request_exceeds_context_budget")
    return bound


def wire_schema(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: wire_schema(item) for key, item in value.items() if key not in _UNSUPPORTED_SCHEMA_KEYWORDS}
    return value


def request_body(model: str, policy: dict[str, Any], system: str, user: str) -> dict[str, Any]:
    require(model in MODELS, "forbidden_model_or_fallback")
    # "global" overrides any workspace default of US-only inference, billed at 1.1 times.
    return {"model": model, "max_tokens": policy["output_tokens"], "system": system,
            "messages": [{"role": "user", "content": user}], "thinking": {"type": "adaptive"},
            "output_config": {"effort": "high",
                              "format": {"type": "json_schema", "schema": wire_schema(REVIEW_SCHEMA)}},
            "inference_geo": "global"}


def prepare_prompts(snapshot: dict[str, Any], groups: list[list[dict[str, Any]]],
                    policy: dict[str, Any]) -> dict[str, list[tuple[str, str]]]:
    """Use the native /review budget without constructing a live GitHub provider."""
    origin: Any = getattr(importlib.util.find_spec("pr_agent"), "origin", None)
    require(isinstance(origin, str) and origin, "pr_agent_not_installed")
    root = Path(origin).parent.parent
    require(not any((root / path).exists() for path in
                    ("pr_agent/settings/.secrets.toml", "pr_agent/settings_prod/.secrets.toml")),
            "engine_secret_file_refused")
    revision = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no",
                            "--", "pr_agent", "uv.lock"], capture_output=True, text=True, check=True).stdout
    require(revision == PR_AGENT_SHA and not dirty, "engine_pin_mismatch")

    from pr_agent.algo.token_handler import TokenHandler
    from pr_agent.config_loader import get_settings
    from pr_agent.log import get_logger
    from pr_agent.tools.pr_reviewer import PRReviewer

    get_logger().remove()  # Source and provider exceptions must not reach CI logs.
    settings = get_settings()
    settings.set("config.publish_output", False)
    settings.set("config.use_repo_settings_file", False)
    settings.set("config.use_global_settings_file", False)
    settings.set("config.fallback_models", [])
    settings.set("config.model_router", {})
    settings.set("config.custom_model_max_tokens", 120000)
    settings.set("config.max_model_tokens", 120000)
    settings.set("config.model_token_count_estimate_factor", 0.25)
    settings.set("config.temperature", 0)
    definitions, example_marker, _ = settings.pr_review_prompt.system.partition("Example output:")
    require(example_marker and "The output must be a YAML object" in definitions,
            "unsupported_native_prompt_format")
    system_template = definitions.replace("The output must be a YAML object", "The output must be a JSON object")
    system_template = system_template.replace(
        "One or two word title for the issue. For example: 'Possible Bug', etc.",
        "Title starting with [P0], [P1], [P2] or [P3], then a space and a concise issue description.")
    system_template += "\nReturn exactly one JSON object with one top-level review field. The review must contain exactly key_issues_to_review, security_concerns, merge_recommendation and risk_level. Omit relevant_tests and every other optional field. No YAML or Markdown fences."
    system_template += "\nAll four fields belong inside review. Put the security result at review.security_concerns, never beside review at the root. Output nesting example (choose the values from the evidence): " + canonical({"review": {"key_issues_to_review": [], "security_concerns": "No", "merge_recommendation": "merge_with_caution", "risk_level": "medium"}}).decode()
    system_template += "\nThe JSON must satisfy this exact schema, including priority tags in every issue_header:\n" + canonical(REVIEW_SCHEMA).decode()
    user_template, user_marker, user_suffix = settings.pr_review_prompt.user.partition(
        "Response (should be a valid YAML, and nothing else):")
    require(user_marker and user_suffix.strip() == "```yaml", "unsupported_native_prompt_format")
    user_template += "Response (must be one JSON object matching the supplied schema, and nothing else):"

    class PromptCapture:
        def get_output_token_reserve(self, model: str, default_output_tokens: int) -> int:
            return policy["output_tokens"]

        async def chat_completion(self, *, model: str, temperature: float,
                                  system: str, user: str) -> tuple[str, str]:
            self.prompt = (system, user)
            return "", "stop"

    class FrozenReviewer(PRReviewer):
        def __init__(self, model: str):
            # PR-Agent's live constructor reads mutable PR metadata and history.
            # This adapter provides the frozen inputs its prediction method needs.
            self.git_provider = SimpleNamespace(pr=True)
            self.ai_handler = PromptCapture()
            self.vars = {"previous_findings": "", "title": f"PR #{snapshot['pr']}", "branch": snapshot["head"],
                         "date": snapshot["date"],
                         "description": "", "commit_messages_str": "", "language": "",
                         "diff": "", "num_pr_files": len(snapshot["files"]), "num_max_findings": 12,
                         "question_str": "", "answer_str": "", "related_tickets": [],
                         "related_tickets_omitted": 0, "skills_context": "", "repo_context": "",
                         "custom_labels": "", "enable_custom_labels": False, "is_ai_metadata": False,
                         "duplicate_prompt_examples": False,
                         "diff_hunk_format": "Each file has its path, a complete unified diff, and complete before/after source. Unified hunk headers give line numbers. Only the diff introduces changes.",
                         "extra_instructions": policy["instructions"]}
            for flag in ("score", "tests", "estimate_effort_to_review", "priority_files",
                         "estimate_contribution_time_cost", "can_be_split_review", "todo_scan"):
                self.vars["require_" + flag] = False
            for flag in ("security_review", "risk_assessment", "merge_recommendation"):
                self.vars["require_" + flag] = True
            self.token_handler = TokenHandler(True, self.vars, system_template,
                                              user_template, model=model)

    async def capture_all() -> dict[str, list[tuple[str, str]]]:
        result = {}
        original_system = settings.pr_review_prompt.system
        original_user = settings.pr_review_prompt.user
        settings.set("pr_review_prompt.system", system_template)
        settings.set("pr_review_prompt.user", user_template)
        for model in MODELS:
            for field in ("model", "model_weak", "model_reasoning"):
                settings.set("config." + field, model)
            reviewer = FrozenReviewer(model)
            prompts = []
            for group in groups:
                # JSON encodes source delimiters and file names without allowing
                # them to alter the prompt template or add another message role.
                await reviewer._get_prediction(model, canonical(group).decode())
                prompts.append(reviewer.ai_handler.prompt)
            result[model] = prompts
        settings.set("pr_review_prompt.system", original_system)
        settings.set("pr_review_prompt.user", original_user)
        return result

    return asyncio.run(capture_all())


class OutputError(ReviewError):
    """A trusted validation stage with a fixed public error code."""

    def __init__(self, category: str):
        self.category = category
        super().__init__({"json_syntax": "invalid_review_json",
                          "duplicate_keys": "duplicate_review_key",
                          "root_or_nesting": "invalid_review_root",
                          "strict_schema": "invalid_native_review_schema"}[category])


def parse_review(content: str) -> dict[str, Any]:
    from pr_agent.algo.output_models import PRReview
    from pydantic import ValidationError

    def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        if len(pairs) != len({key for key, _ in pairs}):
            raise OutputError("duplicate_keys")
        return dict(pairs)

    def invalid_constant(_value: str) -> Any:
        raise OutputError("json_syntax")

    try:
        data = json.loads(content, object_pairs_hook=unique_keys, parse_constant=invalid_constant)
    except json.JSONDecodeError:
        raise OutputError("json_syntax") from None
    if not isinstance(data, dict) or ("review" in data and not isinstance(data["review"], dict)):
        raise OutputError("root_or_nesting")
    try:
        PRReview.model_validate(data, strict=True)
    except ValidationError:
        raise OutputError("strict_schema") from None
    return data["review"]


def review_snapshot(snapshot: dict[str, Any], policy: dict[str, Any],
                    send: Callable[..., Any] = request_json,
                    prompt_builder: Callable[..., Any] = prepare_prompts,
                    parser: Callable[[str], Any] = parse_review, *,
                    receipt_path: Path | None = None) -> dict[str, Any]:
    # Only this transport receives the key; PR-Agent and its dependencies,
    # imported later, cannot read it from the environment.
    key = os.environ.pop("ANTHROPIC_API_KEY", "")
    require(key, "anthropic_key_not_provisioned")
    groups = chunks(snapshot, policy)
    prompts = prompt_builder(snapshot, groups, policy)
    require(prompts[MODELS[0]] == prompts[MODELS[1]], "independent_context_mismatch")
    bodies = {model: [request_body(model, policy, system, user) for system, user in prompts[model]]
              for model in MODELS}
    bounds = {model: [input_token_bound(body) for body in bodies[model]] for model in MODELS}
    # Each call may bill at most its input bound and output cap at list prices,
    # and usage beyond either fails the review, so no call can spend past this.
    with localcontext(_MONEY):
        reservation = sum((call_cost({"input_tokens": bound, "output_tokens": policy["output_tokens"]},
                                     policy["prices"][model]) for model in MODELS for bound in bounds[model]),
                          Decimal(0))
    require(reservation <= Decimal(str(policy["max_review_usd"])), "review_budget_exceeded")
    guarded_reservation = guard_amount(reservation)
    if snapshot.get("snapshot_kind") == "central_public_report_v1":
        from pr_review_public import verify_live
        verify_live(snapshot, policy, snapshot["policy_sha"])
    # The byte bound is not a documented token ceiling: Anthropic's own count
    # must fit it, leaving the 1,000-token margin for the count's estimate error.
    for model in MODELS:
        for body in bodies[model]:
            counted = send(ANTHROPIC_API + "messages/count_tokens", key,
                           {field: body[field] for field in body if field not in ("max_tokens", "inference_geo")})
            require(isinstance(counted, dict) and type(counted.get("input_tokens")) is int  # pylint: disable=unidiomatic-typecheck
                    and 0 < counted["input_tokens"] <= len(canonical(body)), "unbounded_input_tokens")
    from pr_review_receipts import ReceiptJournal, metadata
    journal = ReceiptJournal(receipt_path, snapshot, groups, guarded_reservation)
    report: dict[str, Any] = {"schema": 3, "context_hash": snapshot["context_hash"],
                              "engine_sha": PR_AGENT_SHA, "complete": False,
                              "remaining_files": [f["path"] for f in snapshot["files"]],
                              "failed_chunks": 0, "reservation_usd": guarded_reservation, "reviews": []}
    spent = Decimal(0)
    generation_ids: set[str] = set()
    ordinal = 0
    for model in MODELS:
        entry: dict[str, Any] = {"model": model, "chunk_hashes": [], "results": [], "generations": []}
        for group, body, bound in zip(groups, bodies[model], bounds[model], strict=True):
            journal.update(ordinal, transport_state="request_intended")
            response = send(ANTHROPIC_API + "messages", key, body, timeout=600,
                            response_observer=lambda: journal.update(ordinal, transport_state="response_observed"))
            receipt = metadata(response, model, policy["prices"][model], output_tokens=policy["output_tokens"])
            journal.update(ordinal, transport_state="response_received", **receipt)
            category = "completion_contract"
            try:
                content = validate_completion(response, model)
                require(response["usage"]["input_tokens"] <= bound
                        and response["usage"]["output_tokens"] <= policy["output_tokens"],
                        "unexpected_token_usage")
                require(receipt["generation_id_valid"] and response["id"] not in generation_ids,
                        "unusable_or_duplicate_generation_id")
                category = "strict_schema"
                review = parser(content)
                validate_review(review, group)
            except OutputError as error:
                journal.update(ordinal, output_state="invalid", diagnostic_category=error.category)
                raise
            except ReviewError:
                journal.update(ordinal, output_state="invalid", diagnostic_category=category)
                raise
            except Exception:
                journal.update(ordinal, output_state="indeterminate", diagnostic_category="unexpected_runtime")
                raise ReviewError("invalid_model_output") from None
            spent = _MONEY.add(spent, call_cost(response["usage"], policy["prices"][model]))
            generation_ids.add(response["id"])
            journal.update(ordinal, output_state="valid")
            entry["results"].append(review)
            entry["chunk_hashes"].append(digest(group))
            entry["generations"].append({"generation_id_sha256": receipt["generation_id_sha256"],
                                         "stop_reason": "end_turn",
                                         "usage": {"input_tokens": response["usage"]["input_tokens"],
                                                   "output_tokens": response["usage"]["output_tokens"],
                                                   "cost": receipt["usage"]["cost"]}})
            ordinal += 1
        report["reviews"].append(entry)
    report.update(complete=True, remaining_files=[], spent_usd=guard_amount(spent))
    public_bytes = canonical(report)
    require(all(identifier.encode("ascii") not in public_bytes for identifier in generation_ids),
            "raw_generation_id_in_report")
    journal.finish()
    return report
