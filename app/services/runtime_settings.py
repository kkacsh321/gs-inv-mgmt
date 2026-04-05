import json
from typing import Any

from app.config import settings

DEFAULT_AI_DOMAIN_ENABLED: dict[str, bool] = {
    "chat": True,
    "comp_tool": True,
    "coin_grader": True,
    "coin_identifier": True,
}


def _to_bool(value: Any, fallback: bool) -> bool:
    if value is None:
        return fallback
    raw = str(value).strip().lower()
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    return fallback


def _to_int(value: Any, fallback: int) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return fallback


def _to_float(value: Any, fallback: float) -> float:
    try:
        return float(str(value).strip())
    except Exception:
        return fallback


def get_runtime_value(repo: Any, key: str, default: Any) -> Any:
    try:
        row = repo.get_runtime_setting(environment=settings.app_env, key=key, active_only=True)
    except Exception:
        row = None
    if row is None:
        return default

    raw = row.value
    value_type = (row.value_type or "str").strip().lower()
    if value_type == "bool":
        return _to_bool(raw, bool(default))
    if value_type == "int":
        return _to_int(raw, int(default))
    if value_type == "float":
        return _to_float(raw, float(default))
    if value_type == "json":
        try:
            return json.loads(raw)
        except Exception:
            return default
    return raw


def get_runtime_bool(repo: Any, key: str, default: bool) -> bool:
    value = get_runtime_value(repo, key, default)
    return _to_bool(value, default)


def get_runtime_int(repo: Any, key: str, default: int) -> int:
    value = get_runtime_value(repo, key, default)
    return _to_int(value, default)


def get_runtime_float(repo: Any, key: str, default: float) -> float:
    value = get_runtime_value(repo, key, default)
    return _to_float(value, default)


def get_runtime_str(repo: Any, key: str, default: str) -> str:
    value = get_runtime_value(repo, key, default)
    if value is None:
        return default
    return str(value)


def is_ai_domain_enabled(repo: Any, domain: str) -> bool:
    normalized = str(domain or "").strip().lower()
    fallback = bool(DEFAULT_AI_DOMAIN_ENABLED.get(normalized, True))
    return get_runtime_bool(repo, f"ai_domain_{normalized}_enabled", fallback)
