import json
import base64
from dataclasses import dataclass
from typing import Any

import requests

from app.config import settings


@dataclass(frozen=True)
class LLMRuntimeConfig:
    source: str
    enabled: bool
    provider: str
    model: str
    multimodal_model: str
    base_url: str
    endpoint_type: str
    api_key: str
    temperature: float
    max_output_tokens: int
    timeout_seconds: int


DEFAULT_LLM_BASE_URL = "https://api.openai.com/v1"
DEFAULT_COMP_SYSTEM_MESSAGE = "You are a resale pricing analyst. Provide concise markdown."
DEFAULT_COMP_INSTRUCTION = (
    "Summarize likely fair-market pricing for resale. "
    "Return concise markdown with: confidence level, suggested listing range, "
    "key comparables notes, and outlier warnings. "
    "If spot_context indicates precious-metal bullion/coin relevance, include "
    "spot-anchored commentary (melt-floor framing) and explicitly separate "
    "numismatic premium versus melt-driven valuation."
)


def _safe_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except Exception:
        return fallback


def _safe_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except Exception:
        return fallback


def resolve_comp_llm_runtime_config(repo: Any) -> LLMRuntimeConfig:
    row = None
    try:
        row = repo.get_default_ai_provider_config(environment=settings.app_env)
    except Exception:
        row = None

    if row is not None:
        return LLMRuntimeConfig(
            source="db",
            enabled=bool(row.is_active),
            provider=(row.provider or "openai").strip().lower(),
            model=(row.model or "gpt-4o-mini").strip(),
            multimodal_model=((row.multimodal_model or "").strip() or (row.model or "gpt-4o-mini").strip()),
            base_url=((row.base_url or DEFAULT_LLM_BASE_URL).strip().rstrip("/")),
            endpoint_type=(row.endpoint_type or "responses").strip().lower(),
            api_key=(row.api_key or "").strip(),
            temperature=_safe_float(row.temperature, 0.2),
            max_output_tokens=max(1, _safe_int(row.max_output_tokens, 16000)),
            timeout_seconds=max(5, _safe_int(row.timeout_seconds, 60)),
        )

    return LLMRuntimeConfig(
        source="env",
        enabled=bool(settings.comp_llm_enabled),
        provider=(settings.comp_llm_provider or "openai").strip().lower(),
        model=(settings.comp_llm_model or "gpt-4o-mini").strip(),
        multimodal_model=(settings.comp_llm_model or "gpt-4o-mini").strip(),
        base_url=((settings.comp_llm_base_url or DEFAULT_LLM_BASE_URL).strip().rstrip("/")),
        endpoint_type=(settings.comp_llm_endpoint_type or "responses").strip().lower(),
        api_key=(settings.openai_api_key or "").strip(),
        temperature=_safe_float(settings.comp_llm_temperature, 0.2),
        max_output_tokens=max(1, _safe_int(settings.comp_llm_max_output_tokens, 16000)),
        timeout_seconds=max(5, _safe_int(settings.comp_llm_timeout_seconds, 60)),
    )


def _runtime_bool_from_repo(repo: Any, key: str, fallback: bool) -> bool:
    try:
        row = repo.get_runtime_setting(environment=settings.app_env, key=key, active_only=True)
    except Exception:
        row = None
    if row is None:
        return fallback
    raw = str(row.value or "").strip().lower()
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    return fallback


def _runtime_int_from_repo(repo: Any, key: str, fallback: int) -> int:
    try:
        row = repo.get_runtime_setting(environment=settings.app_env, key=key, active_only=True)
    except Exception:
        row = None
    if row is None:
        return fallback
    try:
        return int(str(row.value).strip())
    except Exception:
        return fallback


def _runtime_str_from_repo(repo: Any, key: str, fallback: str = "") -> str:
    try:
        row = repo.get_runtime_setting(environment=settings.app_env, key=key, active_only=True)
    except Exception:
        row = None
    if row is None:
        return str(fallback or "")
    return str(row.value or "").strip()


def _build_llm_runtime_config_from_row(row: Any) -> LLMRuntimeConfig:
    return LLMRuntimeConfig(
        source="db",
        enabled=bool(row.is_active),
        provider=(row.provider or "openai").strip().lower(),
        model=(row.model or "gpt-4o-mini").strip(),
        multimodal_model=((row.multimodal_model or "").strip() or (row.model or "gpt-4o-mini").strip()),
        base_url=((row.base_url or DEFAULT_LLM_BASE_URL).strip().rstrip("/")),
        endpoint_type=(row.endpoint_type or "responses").strip().lower(),
        api_key=(row.api_key or "").strip(),
        temperature=_safe_float(row.temperature, 0.2),
        max_output_tokens=max(1, _safe_int(row.max_output_tokens, 16000)),
        timeout_seconds=max(5, _safe_int(row.timeout_seconds, 60)),
    )


def _workflow_profile_selector(repo: Any, workflow: str) -> tuple[int | None, str]:
    normalized = str(workflow or "").strip().lower()
    if normalized not in {"listing", "intake", "comp", "risk", "accounting"}:
        return None, ""
    selector_raw = _runtime_str_from_repo(repo, f"ai_workflow_profile_{normalized}", "").strip()
    selector_id: int | None = None
    selector_name = ""
    if selector_raw:
        try:
            selector_id = int(selector_raw)
        except Exception:
            selector_name = selector_raw.lower()
    return selector_id, selector_name


def resolve_comp_llm_runtime_chain(repo: Any, workflow: str = "comp") -> list[LLMRuntimeConfig]:
    primary = resolve_comp_llm_runtime_config(repo)
    fallback_enabled = _runtime_bool_from_repo(repo, "ai_fallback_enabled", True)
    max_profiles = max(1, min(8, _runtime_int_from_repo(repo, "ai_fallback_max_profiles", 3)))
    if not fallback_enabled:
        return [primary]
    if primary.source != "db":
        return [primary]

    try:
        rows = repo.list_ai_provider_configs(environment=settings.app_env, active_only=True)
    except Exception:
        return [primary]
    if not rows:
        return [primary]

    selector_id, selector_name = _workflow_profile_selector(repo, workflow)
    prioritized_rows = list(rows)
    selected_ids: set[Any] = set()
    if selector_id is not None or selector_name:
        selected_rows = []
        for row in rows:
            row_id = getattr(row, "id", None)
            row_name = str(getattr(row, "name", "") or "").strip().lower()
            if (selector_id is not None and row_id == selector_id) or (selector_name and row_name == selector_name):
                selected_rows.append(row)
        if selected_rows:
            selected_ids = {getattr(r, "id", None) for r in selected_rows}
            prioritized_rows = selected_rows + [r for r in rows if getattr(r, "id", None) not in selected_ids]

    default_rows = [
        row
        for row in prioritized_rows
        if bool(row.is_default) and getattr(row, "id", None) not in selected_ids
    ]
    ordered_rows = (
        [row for row in prioritized_rows if getattr(row, "id", None) in selected_ids]
        + default_rows
        + [
            row
            for row in prioritized_rows
            if not bool(row.is_default) and getattr(row, "id", None) not in selected_ids
        ]
    )
    chain: list[LLMRuntimeConfig] = []
    seen: set[tuple[str, str, str, str]] = set()
    for row in ordered_rows:
        cfg = _build_llm_runtime_config_from_row(row)
        signature = (cfg.provider, cfg.base_url, cfg.endpoint_type, cfg.model)
        if signature in seen:
            continue
        seen.add(signature)
        chain.append(cfg)
        if len(chain) >= max_profiles:
            break
    return chain or [primary]


def describe_llm_runtime_chain(repo: Any, workflow: str = "comp") -> list[dict[str, Any]]:
    normalized_workflow = str(workflow or "comp").strip().lower() or "comp"
    selected_profile = ""
    if normalized_workflow in {"listing", "intake", "comp", "risk", "accounting"}:
        selected_profile = _runtime_str_from_repo(repo, f"ai_workflow_profile_{normalized_workflow}", "").strip()
    try:
        chain = resolve_comp_llm_runtime_chain(repo, workflow=normalized_workflow)
    except Exception as exc:
        return [
            {
                "order": 1,
                "workflow": normalized_workflow,
                "status": "error",
                "source": "",
                "provider": "",
                "model": "",
                "endpoint_type": "",
                "base_url": "",
                "enabled": False,
                "api_key": "",
                "max_output_tokens": "",
                "timeout_seconds": "",
                "profile_selector": selected_profile or "(default chain)",
                "error": str(exc)[:300],
            }
        ]
    rows: list[dict[str, Any]] = []
    for idx, cfg in enumerate(chain, start=1):
        rows.append(
            {
                "order": idx,
                "workflow": normalized_workflow,
                "status": "ready" if bool(cfg.enabled) else "disabled",
                "source": str(cfg.source or "").strip(),
                "provider": str(cfg.provider or "").strip(),
                "model": str(cfg.model or "").strip(),
                "endpoint_type": str(cfg.endpoint_type or "").strip(),
                "base_url": str(cfg.base_url or "").strip(),
                "enabled": bool(cfg.enabled),
                "api_key": "present" if str(cfg.api_key or "").strip() else "missing",
                "max_output_tokens": int(cfg.max_output_tokens),
                "timeout_seconds": int(cfg.timeout_seconds),
                "profile_selector": selected_profile or "(default chain)",
                "error": "",
            }
        )
    return rows


def generate_comp_ai_summary(
    config: LLMRuntimeConfig,
    *,
    query: str,
    ebay_rows: list[dict],
    web_rows: list[dict],
    spot_context: dict[str, Any] | None = None,
    system_message: str = DEFAULT_COMP_SYSTEM_MESSAGE,
    instruction: str = DEFAULT_COMP_INSTRUCTION,
) -> str:
    if not config.enabled:
        raise RuntimeError("LLM comps are disabled in runtime settings.")

    if config.provider == "openai" and not config.api_key:
        raise RuntimeError("OpenAI provider requires an API key.")

    endpoint_type = config.endpoint_type if config.endpoint_type in {"responses", "chat_completions"} else "responses"
    target_model = (config.model or "").strip()
    if not target_model:
        raise RuntimeError("Text model is required.")
    input_payload = {
        "query": query,
        "ebay_comps": ebay_rows[:30],
        "web_comps": web_rows[:30],
        "spot_context": spot_context or {},
        "instruction": (instruction or DEFAULT_COMP_INSTRUCTION).strip(),
    }

    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"

    if endpoint_type == "chat_completions":
        endpoint = f"{config.base_url}/chat/completions"
        body = {
            "model": target_model,
            "temperature": config.temperature,
            "max_tokens": config.max_output_tokens,
            "messages": [
                {
                    "role": "system",
                    "content": (system_message or DEFAULT_COMP_SYSTEM_MESSAGE).strip(),
                },
                {
                    "role": "user",
                    "content": json.dumps(input_payload),
                },
            ],
        }
        response = requests.post(endpoint, headers=headers, json=body, timeout=config.timeout_seconds)
        response.raise_for_status()
        payload = response.json() or {}
        choices = payload.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            content = str(message.get("content") or "").strip()
            if content:
                return content
        raise RuntimeError("AI response did not contain chat completion text.")

    endpoint = f"{config.base_url}/responses"
    body = {
        "model": target_model,
        "input": json.dumps(input_payload),
        "max_output_tokens": config.max_output_tokens,
        "temperature": config.temperature,
    }
    response = requests.post(endpoint, headers=headers, json=body, timeout=config.timeout_seconds)
    response.raise_for_status()
    payload = response.json() or {}
    text = str(payload.get("output_text") or "").strip()
    if text:
        return text

    output = payload.get("output") or []
    if isinstance(output, list):
        parts: list[str] = []
        for item in output:
            for content in item.get("content", []) or []:
                value = content.get("text")
                if value:
                    parts.append(str(value))
        text = "\n".join(parts).strip()
    if not text:
        raise RuntimeError("AI response did not contain text output.")
    return text


def generate_comp_ai_summary_with_fallback(
    configs: list[LLMRuntimeConfig],
    *,
    query: str,
    ebay_rows: list[dict],
    web_rows: list[dict],
    spot_context: dict[str, Any] | None = None,
    system_message: str = DEFAULT_COMP_SYSTEM_MESSAGE,
    instruction: str = DEFAULT_COMP_INSTRUCTION,
) -> tuple[str, LLMRuntimeConfig, list[str]]:
    enabled_profiles = [cfg for cfg in configs if bool(cfg.enabled)]
    attempts = [cfg for cfg in enabled_profiles if not (cfg.provider == "openai" and not (cfg.api_key or "").strip())]
    if not attempts:
        if enabled_profiles:
            raise RuntimeError(
                "No executable AI runtime profiles available. "
                "OpenAI profiles require an API key; set one in Admin > AI Runtime or disable those profiles."
            )
        raise RuntimeError("No enabled AI runtime profiles available.")
    errors: list[str] = []
    for cfg in attempts:
        try:
            text = generate_comp_ai_summary(
                cfg,
                query=query,
                ebay_rows=ebay_rows,
                web_rows=web_rows,
                spot_context=spot_context,
                system_message=system_message,
                instruction=instruction,
            )
            return text, cfg, errors
        except Exception as exc:
            errors.append(f"{cfg.provider}:{cfg.model} -> {exc}")
            continue
    raise RuntimeError("All AI runtime fallback attempts failed. " + " | ".join(errors))


def validate_llm_runtime_config(config: LLMRuntimeConfig) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"

    endpoint_type = config.endpoint_type if config.endpoint_type in {"responses", "chat_completions"} else "responses"
    if endpoint_type == "chat_completions":
        endpoint = f"{config.base_url}/chat/completions"
        body = {
            "model": config.model,
            "messages": [
                {"role": "system", "content": "Respond with OK."},
                {"role": "user", "content": "Connectivity test."},
            ],
            "max_tokens": 16,
            "temperature": 0,
        }
    else:
        endpoint = f"{config.base_url}/responses"
        body = {
            "model": config.model,
            "input": "Connectivity test. Respond with OK.",
            "max_output_tokens": 32,
            "temperature": 0,
        }

    response = requests.post(endpoint, headers=headers, json=body, timeout=config.timeout_seconds)
    response.raise_for_status()
    payload = response.json() or {}
    return {
        "source": config.source,
        "provider": config.provider,
        "endpoint_type": endpoint_type,
        "status_code": response.status_code,
        "response_id": payload.get("id", ""),
    }


def _models_endpoint_candidates(base_url: str) -> list[str]:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return []
    candidates = [f"{base}/models"]
    if base.endswith("/v1"):
        candidates.append(f"{base[:-3]}/models".rstrip("/"))
    else:
        candidates.append(f"{base}/v1/models")
    out: list[str] = []
    for item in candidates:
        if item and item not in out:
            out.append(item)
    return out


def fetch_available_models(
    *,
    base_url: str,
    api_key: str = "",
    timeout_seconds: int = 30,
) -> list[str]:
    headers: dict[str, str] = {}
    token = (api_key or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    last_error: Exception | None = None
    for endpoint in _models_endpoint_candidates(base_url):
        try:
            response = requests.get(endpoint, headers=headers, timeout=max(5, int(timeout_seconds)))
            response.raise_for_status()
            payload = response.json() or {}
            rows: list[Any] = []
            if isinstance(payload, dict):
                if isinstance(payload.get("data"), list):
                    rows = payload.get("data") or []
                elif isinstance(payload.get("models"), list):
                    rows = payload.get("models") or []
                elif isinstance(payload.get("model_list"), list):
                    rows = payload.get("model_list") or []
            elif isinstance(payload, list):
                rows = payload
            model_ids: list[str] = []
            for row in rows:
                model_id = ""
                if isinstance(row, str):
                    model_id = row.strip()
                elif isinstance(row, dict):
                    model_id = str(
                        row.get("id")
                        or row.get("name")
                        or row.get("model")
                        or row.get("model_name")
                        or ""
                    ).strip()
                if model_id and model_id not in model_ids:
                    model_ids.append(model_id)
            if model_ids:
                return sorted(model_ids)
            raise RuntimeError("`/models` returned no model ids.")
        except Exception as exc:
            last_error = exc
            continue

    if last_error is not None:
        raise RuntimeError("Unable to load models from endpoint. Check base URL/API key/provider.") from last_error
    raise RuntimeError("Unable to load models: base URL is not configured.")


def _extract_text_from_llm_payload(payload: dict[str, Any]) -> str:
    text = str(payload.get("output_text") or "").strip()
    if text:
        return text
    output = payload.get("output") or []
    if isinstance(output, list):
        parts: list[str] = []
        for item in output:
            for content in item.get("content", []) or []:
                value = content.get("text")
                if value:
                    parts.append(str(value))
        joined = "\n".join(parts).strip()
        if joined:
            return joined
    choices = payload.get("choices") or []
    if choices:
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            out = content.strip()
            if out:
                return out
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    t = item.get("text")
                    if t:
                        parts.append(str(t))
            out = "\n".join(parts).strip()
            if out:
                return out
    return ""


def _looks_like_no_vision_capability_response(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    markers = [
        "cannot process or analyze images",
        "can't process or analyze images",
        "i cannot analyze images",
        "i can't analyze images",
        "unable to view images",
        "cannot view images",
        "can't view images",
        "provide a detailed description of the image",
        "please describe the image",
    ]
    return any(marker in t for marker in markers)


def _is_transient_multimodal_exception(exc: Exception) -> bool:
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    if isinstance(exc, requests.HTTPError):
        status = int(getattr(getattr(exc, "response", None), "status_code", 0) or 0)
        return status in {408, 409, 425, 429, 500, 502, 503, 504}
    return False


def generate_multimodal_ai_markdown(
    config: LLMRuntimeConfig,
    *,
    system_message: str,
    instruction: str,
    image_bytes: bytes | None = None,
    image_content_type: str = "image/jpeg",
    additional_images: list[tuple[bytes, str]] | None = None,
    max_output_tokens_override: int | None = None,
) -> str:
    if not config.enabled:
        raise RuntimeError("LLM runtime is disabled in settings.")
    if config.provider == "openai" and not config.api_key:
        raise RuntimeError("OpenAI provider requires an API key.")

    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"

    endpoint_type = config.endpoint_type if config.endpoint_type in {"responses", "chat_completions"} else "responses"
    if config.provider == "localai" and endpoint_type != "chat_completions":
        raise RuntimeError(
            "LocalAI multimodal flows should use `chat_completions` endpoint type. "
            "Update the AI runtime profile in Admin."
        )
    target_model = (config.multimodal_model or config.model or "").strip()
    if not target_model:
        raise RuntimeError("Multimodal model is required.")
    sys_msg = (system_message or "").strip()
    user_msg = (instruction or "").strip()
    if not user_msg:
        raise RuntimeError("Instruction is required.")
    resolved_max_tokens = (
        int(max_output_tokens_override)
        if max_output_tokens_override is not None and int(max_output_tokens_override) > 0
        else int(config.max_output_tokens)
    )

    data_urls: list[str] = []
    if image_bytes:
        mime = (image_content_type or "image/jpeg").strip()
        b64 = base64.b64encode(image_bytes).decode("ascii")
        data_urls.append(f"data:{mime};base64,{b64}")
    for blob, mime in (additional_images or []):
        if not blob:
            continue
        resolved_mime = (mime or "image/jpeg").strip()
        b64 = base64.b64encode(blob).decode("ascii")
        data_urls.append(f"data:{resolved_mime};base64,{b64}")

    if endpoint_type == "chat_completions":
        endpoint = f"{config.base_url}/chat/completions"
        content_blocks: list[dict[str, Any]] = [{"type": "text", "text": user_msg}]
        for data_url in data_urls:
            content_blocks.append({"type": "image_url", "image_url": {"url": data_url}})
        body = {
            "model": target_model,
            "temperature": config.temperature,
            "max_tokens": resolved_max_tokens,
            "messages": [
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": content_blocks},
            ],
        }
        response = requests.post(endpoint, headers=headers, json=body, timeout=config.timeout_seconds)
        response.raise_for_status()
        payload = response.json() or {}
        out = _extract_text_from_llm_payload(payload)
        if out:
            if _looks_like_no_vision_capability_response(out):
                localai_hint = (
                    " For LocalAI, use a vision-capable model (for example a GLM vision variant) "
                    "and set endpoint type to `chat_completions`."
                    if config.provider == "localai"
                    else ""
                )
                raise RuntimeError(
                    "Selected multimodal model appears to not support image analysis. "
                    "Set a vision-capable `multimodal_model` in Admin AI Runtime profile."
                    + localai_hint
                )
            return out
        raise RuntimeError("AI response did not contain text output.")

    endpoint = f"{config.base_url}/responses"
    user_content: list[dict[str, Any]] = [{"type": "input_text", "text": user_msg}]
    for data_url in data_urls:
        user_content.append({"type": "input_image", "image_url": data_url})
    body = {
        "model": target_model,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": sys_msg}]},
            {"role": "user", "content": user_content},
        ],
        "max_output_tokens": resolved_max_tokens,
        "temperature": config.temperature,
    }
    response = requests.post(endpoint, headers=headers, json=body, timeout=config.timeout_seconds)
    response.raise_for_status()
    payload = response.json() or {}
    out = _extract_text_from_llm_payload(payload)
    if out:
        if _looks_like_no_vision_capability_response(out):
            localai_hint = (
                " For LocalAI, use a vision-capable model (for example a GLM vision variant) "
                "and set endpoint type to `chat_completions`."
                if config.provider == "localai"
                else ""
            )
            raise RuntimeError(
                "Selected multimodal model appears to not support image analysis. "
                "Set a vision-capable `multimodal_model` in Admin AI Runtime profile."
                + localai_hint
            )
        return out
    raise RuntimeError("AI response did not contain text output.")


def generate_multimodal_ai_markdown_with_fallback(
    configs: list[LLMRuntimeConfig],
    *,
    system_message: str,
    instruction: str,
    image_bytes: bytes | None = None,
    image_content_type: str = "image/jpeg",
    additional_images: list[tuple[bytes, str]] | None = None,
    max_output_tokens_override: int | None = None,
) -> tuple[str, LLMRuntimeConfig, list[str]]:
    enabled_profiles = [cfg for cfg in configs if bool(cfg.enabled)]
    attempts = [cfg for cfg in enabled_profiles if not (cfg.provider == "openai" and not (cfg.api_key or "").strip())]
    if not attempts:
        if enabled_profiles:
            raise RuntimeError(
                "No executable multimodal runtime profiles available. "
                "OpenAI profiles require an API key; set one in Admin > AI Runtime or disable those profiles."
            )
        raise RuntimeError("No enabled AI runtime profiles available.")
    errors: list[str] = []
    for cfg in attempts:
        model_label = cfg.multimodal_model or cfg.model
        transient_retry_exception: Exception | None = None
        for attempt_no in range(2):
            try:
                text = generate_multimodal_ai_markdown(
                    cfg,
                    system_message=system_message,
                    instruction=instruction,
                    image_bytes=image_bytes,
                    image_content_type=image_content_type,
                    additional_images=additional_images,
                    max_output_tokens_override=max_output_tokens_override,
                )
                if transient_retry_exception is not None:
                    errors.append(
                        f"{cfg.provider}:{model_label} -> recovered after transient error retry: "
                        f"{transient_retry_exception}"
                    )
                return text, cfg, errors
            except Exception as exc:
                if attempt_no == 0 and _is_transient_multimodal_exception(exc):
                    transient_retry_exception = exc
                    continue
                if transient_retry_exception is not None:
                    errors.append(
                        f"{cfg.provider}:{model_label} -> transient retry failed "
                        f"(first: {transient_retry_exception}; final: {exc})"
                    )
                else:
                    errors.append(f"{cfg.provider}:{model_label} -> {exc}")
                break
    raise RuntimeError("All multimodal fallback attempts failed. " + " | ".join(errors))
