from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from app.config import settings
from app.services.runtime_settings import get_runtime_str, get_runtime_value


WORKFLOW_PROMPT_KEYS: dict[str, tuple[str, ...]] = {
    "comp": (
        "comp_llm_system_message",
        "comp_llm_instruction_template",
    ),
    "listing": (
        "listing_wizard_ai_system_message",
        "listing_wizard_ai_instruction_template",
        "listing_wizard_ai_seed_default",
    ),
}


def _registry_runtime_key(workflow: str) -> str:
    return f"ai_prompt_registry_{str(workflow or '').strip().lower()}_json"


def _active_version_runtime_key(workflow: str) -> str:
    return f"ai_prompt_active_version_{str(workflow or '').strip().lower()}"


def _normalize_workflow(workflow: str) -> str:
    normalized = str(workflow or "").strip().lower()
    if normalized not in WORKFLOW_PROMPT_KEYS:
        raise ValueError(f"Unsupported prompt workflow: {workflow}")
    return normalized


def prompt_registry_runtime_keys(workflow: str) -> tuple[str, str]:
    normalized = _normalize_workflow(workflow)
    return _registry_runtime_key(normalized), _active_version_runtime_key(normalized)


def prompt_keys_for_workflow(workflow: str) -> tuple[str, ...]:
    return WORKFLOW_PROMPT_KEYS[_normalize_workflow(workflow)]


def current_prompt_values(repo, workflow: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for key in prompt_keys_for_workflow(workflow):
        values[key] = str(get_runtime_str(repo, key, "") or "").strip()
    return values


def _parse_registry_rows(raw: str) -> list[dict[str, Any]]:
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except Exception:
        return []
    if not isinstance(payload, list):
        return []
    rows: list[dict[str, Any]] = []
    for row in payload:
        if isinstance(row, dict):
            rows.append(dict(row))
    return rows


def list_prompt_versions(repo, workflow: str, *, limit: int = 50) -> list[dict[str, Any]]:
    normalized = _normalize_workflow(workflow)
    registry_key, _ = prompt_registry_runtime_keys(normalized)
    raw_value = get_runtime_value(repo, registry_key, [])
    if isinstance(raw_value, list):
        rows = [dict(row) for row in raw_value if isinstance(row, dict)]
    else:
        raw = str(get_runtime_str(repo, registry_key, "[]") or "[]")
        rows = _parse_registry_rows(raw)
    rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    return rows[: max(1, int(limit))]


def active_prompt_version(repo, workflow: str) -> str:
    normalized = _normalize_workflow(workflow)
    _, active_key = prompt_registry_runtime_keys(normalized)
    return str(get_runtime_str(repo, active_key, "") or "").strip()


def _version_id_from_values(values: dict[str, str]) -> str:
    raw = json.dumps(values, sort_keys=True, ensure_ascii=True)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{digest}"


def create_prompt_version(
    repo,
    workflow: str,
    *,
    actor: str,
    note: str = "",
    set_active: bool = True,
    max_versions: int = 60,
) -> dict[str, Any]:
    normalized = _normalize_workflow(workflow)
    values = current_prompt_values(repo, normalized)
    version_id = _version_id_from_values(values)
    row = {
        "version_id": version_id,
        "workflow": normalized,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by": str(actor or "system").strip() or "system",
        "note": str(note or "").strip(),
        "prompt_values": values,
    }
    existing = list_prompt_versions(repo, normalized, limit=max_versions * 2)
    existing = [r for r in existing if str(r.get("version_id") or "") != version_id]
    merged = [row, *existing][: max(1, int(max_versions))]
    _save_registry(repo, normalized, merged, actor=actor)
    if set_active:
        set_active_prompt_version(repo, normalized, version_id, actor=actor)
    return row


def set_active_prompt_version(repo, workflow: str, version_id: str, *, actor: str) -> None:
    normalized = _normalize_workflow(workflow)
    _, active_key = prompt_registry_runtime_keys(normalized)
    repo.upsert_runtime_setting(
        environment=settings.app_env,
        key=active_key,
        value=str(version_id or "").strip(),
        value_type="str",
        description=f"Active prompt registry version id for `{normalized}` workflow.",
        is_active=True,
        actor=str(actor or "system").strip() or "system",
    )


def _save_registry(repo, workflow: str, rows: list[dict[str, Any]], *, actor: str) -> None:
    normalized = _normalize_workflow(workflow)
    registry_key, _ = prompt_registry_runtime_keys(normalized)
    repo.upsert_runtime_setting(
        environment=settings.app_env,
        key=registry_key,
        value=json.dumps(rows, ensure_ascii=True, default=str),
        value_type="json",
        description=f"Prompt registry versions for `{normalized}` workflow.",
        is_active=True,
        actor=str(actor or "system").strip() or "system",
    )


def restore_prompt_version(
    repo,
    workflow: str,
    *,
    version_id: str,
    actor: str,
    set_active: bool = True,
) -> dict[str, Any] | None:
    normalized = _normalize_workflow(workflow)
    target = str(version_id or "").strip()
    if not target:
        return None
    rows = list_prompt_versions(repo, normalized, limit=500)
    selected = next((r for r in rows if str(r.get("version_id") or "") == target), None)
    if not isinstance(selected, dict):
        return None
    prompt_values = selected.get("prompt_values")
    if not isinstance(prompt_values, dict):
        prompt_values = {}
    for key in prompt_keys_for_workflow(normalized):
        repo.upsert_runtime_setting(
            environment=settings.app_env,
            key=key,
            value=str(prompt_values.get(key) or "").strip(),
            value_type="str",
            description=f"Prompt value (`{key}`) restored from `{normalized}` prompt registry.",
            is_active=True,
            actor=str(actor or "system").strip() or "system",
        )
    if set_active:
        set_active_prompt_version(repo, normalized, target, actor=actor)
    return selected
