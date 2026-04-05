from __future__ import annotations


REQUIRED_ENV_KEYS: set[str] = {
    "APP_NAME",
    "APP_ENV",
    "APP_REQUIRE_PASSWORD_AUTH",
    "APP_AUTH_SIGNING_KEY",
    "APP_AUTH_REMEMBER_DAYS",
    "APP_AUTH_COOKIE_ENABLED",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
}

REQUIRED_RUNTIME_KEYS: set[str] = {
    "ai_domain_chat_enabled",
    "ai_domain_comp_tool_enabled",
    "ai_domain_coin_grader_enabled",
    "ai_domain_coin_identifier_enabled",
    "ai_fallback_enabled",
    "ai_fallback_max_profiles",
    "chat_ai_refine_enabled",
    "shipping_queue_enabled",
    "shipping_label_purchase_enabled",
    "slack_notify_integration_queue_failures",
    "slack_notify_system_health_critical",
    "backup_policy_enabled",
    "backup_policy_cadence_hours",
    "backup_policy_retention_days",
    "backup_policy_upload_to_s3",
    "backup_restore_drill_interval_days",
    "backup_restore_rto_target_minutes",
}


def required_env_keys() -> set[str]:
    return set(REQUIRED_ENV_KEYS)


def required_runtime_keys() -> set[str]:
    return set(REQUIRED_RUNTIME_KEYS)


def health_state(score_ratio: float) -> str:
    ratio = max(0.0, min(1.0, float(score_ratio)))
    if ratio >= 0.95:
        return "healthy"
    if ratio >= 0.80:
        return "warning"
    return "critical"


def env_missing_or_empty(required_keys: set[str], env_values: dict[str, str]) -> list[str]:
    return [
        key
        for key in sorted(required_keys)
        if key not in env_values or not str(env_values.get(key, "")).strip()
    ]


def runtime_missing_or_inactive(required_keys: set[str], runtime_rows: list) -> list[str]:
    by_key = {str(row.key): row for row in runtime_rows}
    return [
        key
        for key in sorted(required_keys)
        if key not in by_key or not bool(getattr(by_key[key], "is_active", False))
    ]
