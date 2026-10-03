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
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from pr_review import (
    MODELS,
    PR_AGENT_SHA,
    REVIEW_SCHEMA,
    ReviewError,
    canonical,
    chunks,
    digest,
    request_json,
    require,
    reserve_budget,
    validate_completion,
    validate_review,
)


def request_body(model: str, policy: dict[str, Any], system: str, user: str) -> dict[str, Any]:
    require(model in MODELS, "forbidden_model_or_fallback")
    route = policy["routes"][model]
    return {"model": model, "messages": [{"role": "system", "content": system},
                                            {"role": "user", "content": user}],
            "max_tokens": policy["output_tokens"], "temperature": 0, "stream": False,
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "pr_review", "strict": True, "schema": REVIEW_SCHEMA}}
                if model == MODELS[0] else {"type": "json_object"},
            "reasoning": {"effort": "high"} if model == MODELS[0] else {"enabled": True},
            "provider": {"only": [route["slug"]], "allow_fallbacks": False,
                         "quantizations": ["fp8"],
                         "require_parameters": True, "data_collection": "deny",
                         "max_price": {"prompt": route["prompt"], "completion": route["completion"]}}}


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
    system_template += "\nReturn exactly one JSON object with one top-level review field. The review must contain exactly key_issues_to_review, security_concerns, merge_recommendation and risk_level. Omit relevant_tests and every other optional field. No YAML or Markdown fences."
    system_template += "\nAll four fields belong inside review. Put the security result at review.security_concerns, never beside review at the root. Output nesting example (choose the values from the evidence): " + canonical({"review": {"key_issues_to_review": [], "security_concerns": "No", "merge_recommendation": "merge_with_caution", "risk_level": "medium"}}).decode()
    system_template += "\nThe JSON must satisfy this exact schema, including priority tags in every issue_header:\n" + canonical(REVIEW_SCHEMA).decode()
    user_template, user_marker, user_suffix = settings.pr_review_prompt.user.partition(
        "Response (should be a valid YAML, and nothing else):")
    require(user_marker and user_suffix.strip() == "```yaml", "unsupported_native_prompt_format")
    user_template += "Response (must be one JSON object matching the supplied schema, and nothing else):"

    class PromptCapture:
        def get_output_token_reserve(self, model: str) -> int:
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
            self.vars = {"title": f"PR #{snapshot['pr']}", "branch": snapshot["head"],
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
            native_model = "openrouter/" + model
            for field in ("model", "model_weak", "model_reasoning"):
                settings.set("config." + field, native_model)
            reviewer = FrozenReviewer(native_model)
            prompts = []
            for group in groups:
                # JSON encodes source delimiters and file names without allowing
                # them to alter the prompt template or add another message role.
                await reviewer._get_prediction(native_model, canonical(group).decode())
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
    key = os.environ.get("OPENROUTER_API_KEY", "")
    require(key, "openrouter_key_not_provisioned")
    groups = chunks(snapshot, policy)
    prompts = prompt_builder(snapshot, groups, policy)
    require(prompts[MODELS[0]] == prompts[MODELS[1]], "independent_context_mismatch")
    # Local reservation estimate: one input token per UTF-8 byte plus a fixed
    # margin and output including reasoning. Upstream token counts may differ.
    call_reservations = {
        model: [(len(canonical(request_body(model, policy, system, user))) + 1000) * policy["routes"][model]["prompt"]
                / 1_000_000 + policy["output_tokens"] * policy["routes"][model]["completion"] / 1_000_000
                for system, user in prompts[model]] for model in MODELS}
    reservation = sum(value for values in call_reservations.values() for value in values)
    reserve_budget(send("https://openrouter.ai/api/v1/key", key), policy, reservation)
    if snapshot.get("snapshot_kind") == "central_public_report_v1":
        from pr_review_public import verify_live
        verify_live(snapshot, policy, snapshot["policy_sha"])
    from pr_review_receipts import ReceiptJournal, metadata
    journal = ReceiptJournal(receipt_path, snapshot, groups, policy, reservation)
    report: dict[str, Any] = {"schema": 2, "context_hash": snapshot["context_hash"],
                              "engine_sha": PR_AGENT_SHA, "complete": False,
                              "remaining_files": [f["path"] for f in snapshot["files"]],
                              "failed_chunks": 0, "spending_mode": policy["spending_mode"],
                              "reservation_usd": reservation, "reviews": []}
    spent = 0.0
    remaining_reservation = reservation
    generation_ids: set[str] = set()
    ordinal = 0
    for model in MODELS:
        entry: dict[str, Any] = {"model": model, "provider": policy["routes"][model]["name"],
                                 "chunk_hashes": [], "results": [], "generations": []}
        for group, (system, user), call_reservation in zip(groups, prompts[model],
                                                          call_reservations[model], strict=True):
            require(spent + remaining_reservation <= reservation, "unexpected_billed_cost")
            journal.update(ordinal, transport_state="request_intended")
            response = send("https://openrouter.ai/api/v1/chat/completions", key,
                            request_body(model, policy, system, user),
                            response_observer=lambda: journal.update(ordinal, transport_state="response_observed"))
            receipt = metadata(response, model, policy["routes"][model]["name"])
            journal.update(ordinal, transport_state="response_received", **receipt)
            category = "completion_contract"
            try:
                content = validate_completion(response, model, policy["routes"][model])
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
            spent += response["usage"]["cost"]
            remaining_reservation -= call_reservation
            require(spent <= reservation and spent <= policy["max_review_usd"], "unexpected_billed_cost")
            generation_ids.add(response["id"])
            journal.update(ordinal, output_state="valid")
            entry["results"].append(review)
            entry["chunk_hashes"].append(digest(group))
            entry["generations"].append({"generation_id_sha256": receipt["generation_id_sha256"], "finish_reason": "stop",
                                         "usage": {field: response["usage"][field]
                                                   for field in ("prompt_tokens", "completion_tokens", "cost")}})
            ordinal += 1
        report["reviews"].append(entry)
    report.update(complete=True, remaining_files=[], spent_usd=spent)
    public_bytes = canonical(report)
    require(all(identifier.encode("ascii") not in public_bytes for identifier in generation_ids),
            "raw_generation_id_in_report")
    journal.finish()
    return report
