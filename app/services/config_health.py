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
    "listing_wizard_ai_system_message",
    "listing_wizard_ai_seed_default",
    "listing_wizard_ai_instruction_template",
    "listing_wizard_ai_include_quick_comp_context",
    "listing_wizard_ai_quick_comp_limit",
    "ai_fallback_enabled",
    "ai_fallback_max_profiles",
    "ai_workflow_profile_listing",
    "ai_workflow_profile_intake",
    "ai_workflow_profile_comp",
    "ai_workflow_profile_risk",
    "ai_quality_title_min_words",
    "ai_quality_title_min_chars",
    "ai_quality_listing_details_min_words",
    "ai_quality_listing_details_min_chars",
    "ai_quality_intake_min_words",
    "ai_quality_intake_min_chars",
    "ai_quality_forbidden_terms_csv",
    "ai_prompt_active_version_comp",
    "ai_prompt_active_version_listing",
    "ai_prompt_registry_comp_json",
    "ai_prompt_registry_listing_json",
    "chat_ai_refine_enabled",
    "shipping_queue_enabled",
    "shipping_label_purchase_enabled",
    "slack_notify_integration_queue_failures",
    "slack_notify_system_health_critical",
    "slack_notify_order_imports",
    "backup_policy_enabled",
    "app_default_timezone",
    "backup_policy_runner_enabled",
    "backup_policy_schedule_timezone",
    "backup_policy_schedule_local_time",
    "backup_policy_cadence_hours",
    "backup_policy_retention_days",
    "backup_policy_upload_to_s3",
    "backup_restore_drill_interval_days",
    "backup_restore_rto_target_minutes",
    "slack_daily_report_enabled",
    "slack_daily_report_timezone",
    "slack_daily_report_local_time",
    "slack_daily_report_channel",
    "notification_route_daily_report",
    "notification_route_backup_events",
    "notification_route_business_reports",
    "slack_notify_backup_success",
    "slack_notify_backup_failures",
    "notification_outbox_runner_enabled",
    "notification_outbox_runner_limit",
    "notification_outbox_backoff_base_seconds",
    "notification_outbox_backoff_max_seconds",
    "notification_outbox_retain_sent_days",
    "notification_outbox_retain_failed_days",
    "notification_outbox_cleanup_enabled",
    "notification_outbox_cleanup_timezone",
    "notification_outbox_cleanup_local_time",
    "slack_ops_enabled",
    "slack_ops_runner_enabled",
    "slack_app_token",
    "slack_bot_token",
    "slack_ops_default_role",
    "slack_ops_user_role_map",
    "slack_ops_allowed_channels",
    "slack_ops_allowed_users",
    "slack_ops_write_actions_require_approval",
    "slack_ops_ai_assist_enabled",
    "slack_ops_ai_auto_reply_enabled",
    "slack_ops_process_queue_enabled",
    "slack_ops_process_queue_limit",
    "slack_ops_poll_interval_seconds",
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
