from dataclasses import dataclass
from typing import Any

from app.services.llm_runtime import (
    LLMRuntimeConfig,
    generate_comp_ai_summary_with_fallback,
    generate_multimodal_ai_markdown_with_fallback,
    resolve_comp_llm_runtime_chain,
)
from app.services.grading_standards import (
    build_coin_grading_rules_context_from_web,
    build_comp_rules_context_from_web,
)
from app.services.runtime_settings import get_runtime_str


DEFAULT_COIN_GRADING_RULES_CONTEXT = (
    "Use major third-party grading standards as reference context (PCGS, NGC, ANACS, ICG). "
    "Evaluate coin condition with practical numismatic criteria: wear/friction, luster/cartwheel, "
    "strike quality, surface preservation (marks/hairlines), eye appeal, toning, and damage/cleaning signs. "
    "When uncertain, grade conservatively and state confidence clearly. Distinguish clearly between "
    "estimated raw grade and certified/holdered grade outcomes."
)

DEFAULT_COMP_REFERENCE_RULES_CONTEXT = (
    "For comp analysis, prioritize sold comparables and clearly separate certified vs raw coins. "
    "When certified comps are present, compare within same grading service tier (PCGS/NGC/ANACS/ICG) "
    "and nearby grade bands; avoid mixing unlike grade populations without an explicit adjustment note. "
    "Call out when outliers, altered/cleaned coins, or weak title matches may distort pricing."
)


@dataclass(frozen=True)
class AIExecutionResult:
    text: str
    used_config: LLMRuntimeConfig
    fallback_errors: list[str]
    citation: dict[str, Any]


def _build_citation(
    *,
    tool_name: str,
    used_config: LLMRuntimeConfig,
    fallback_errors: list[str],
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "tool_name": str(tool_name or "").strip(),
        "provider": str(used_config.provider or "").strip(),
        "text_model": str(used_config.model or "").strip(),
        "multimodal_model": str(used_config.multimodal_model or "").strip(),
        "endpoint_type": str(used_config.endpoint_type or "").strip(),
        "source": str(used_config.source or "").strip(),
        "fallback_attempts": len(fallback_errors or []),
        "fallback_errors": list(fallback_errors or []),
        "context": dict(context or {}),
    }


def _append_prompt_context(base_text: str, *, label: str, context_text: str) -> str:
    body = str(base_text or "").strip()
    extra = str(context_text or "").strip()
    if not extra:
        return body
    if not body:
        return f"{label}:\n{extra}"
    return f"{body}\n\n{label}:\n{extra}"


def execute_comp_summary(
    repo: Any,
    *,
    query: str,
    ebay_rows: list[dict],
    web_rows: list[dict],
    spot_context: dict[str, Any] | None,
    system_message: str,
    instruction: str,
    workflow: str = "comp",
) -> AIExecutionResult:
    comp_rules_context = get_runtime_str(
        repo,
        "comp_reference_rules_context",
        "",
    ).strip()
    if not comp_rules_context:
        comp_rules_context = build_comp_rules_context_from_web()
    if not comp_rules_context:
        comp_rules_context = DEFAULT_COMP_REFERENCE_RULES_CONTEXT
    instruction = _append_prompt_context(
        instruction,
        label="Comp Rules Context",
        context_text=comp_rules_context,
    )
    chain = resolve_comp_llm_runtime_chain(repo, workflow=workflow)
    text, used_cfg, fallback_errors = generate_comp_ai_summary_with_fallback(
        chain,
        query=query,
        ebay_rows=ebay_rows,
        web_rows=web_rows,
        spot_context=spot_context,
        system_message=system_message,
        instruction=instruction,
    )
    citation = _build_citation(
        tool_name="comp_summary",
        used_config=used_cfg,
        fallback_errors=fallback_errors,
        context={
            "query": query,
            "ebay_rows": len(ebay_rows or []),
            "web_rows": len(web_rows or []),
            "workflow": str(workflow or "comp").strip().lower(),
        },
    )
    return AIExecutionResult(text=text, used_config=used_cfg, fallback_errors=fallback_errors, citation=citation)


def execute_multimodal_task(
    repo: Any,
    *,
    tool_name: str,
    system_message: str,
    instruction: str,
    image_bytes: bytes | None,
    image_content_type: str = "image/jpeg",
    additional_images: list[tuple[bytes, str]] | None = None,
    max_output_tokens_override: int | None = None,
    context: dict[str, Any] | None = None,
    workflow: str = "comp",
) -> AIExecutionResult:
    lowered_tool = str(tool_name or "").strip().lower()
    if "grader" in lowered_tool:
        grading_rules_context = get_runtime_str(
            repo,
            "coin_grading_rules_context",
            "",
        ).strip()
        if not grading_rules_context:
            grading_rules_context = build_coin_grading_rules_context_from_web()
        if not grading_rules_context:
            grading_rules_context = DEFAULT_COIN_GRADING_RULES_CONTEXT
        instruction = _append_prompt_context(
            instruction,
            label="Grading Rules Context",
            context_text=grading_rules_context,
        )
    chain = resolve_comp_llm_runtime_chain(repo, workflow=workflow)
    text, used_cfg, fallback_errors = generate_multimodal_ai_markdown_with_fallback(
        chain,
        system_message=system_message,
        instruction=instruction,
        image_bytes=image_bytes,
        image_content_type=image_content_type,
        additional_images=additional_images,
        max_output_tokens_override=max_output_tokens_override,
    )
    citation = _build_citation(
        tool_name=tool_name,
        used_config=used_cfg,
        fallback_errors=fallback_errors,
        context={**dict(context or {}), "workflow": str(workflow or "comp").strip().lower()},
    )
    return AIExecutionResult(text=text, used_config=used_cfg, fallback_errors=fallback_errors, citation=citation)
