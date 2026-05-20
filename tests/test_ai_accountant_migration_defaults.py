from pathlib import Path


def test_ai_accountant_default_on_migration_revision_and_keys():
    root = Path(__file__).resolve().parents[1]
    migration = root / "app" / "db" / "alembic" / "versions" / "0060_ai_accountant_default_on.py"
    text = migration.read_text()

    assert 'revision = "0060_ai_accountant_default_on"' in text
    assert 'down_revision = "0059_ai_accountant_permission"' in text
    for key in [
        "ai_accountant_monitor_enabled",
        "ai_accountant_monitor_slack_enabled",
        "ai_accountant_monitor_llm_review_enabled",
        "ai_accountant_chat_ai_enabled",
        "ai_accountant_web_research_enabled",
        "notification_route_ai_accountant_monitor",
    ]:
        assert key in text


def test_notification_outbox_default_on_migration_revision_and_keys():
    root = Path(__file__).resolve().parents[1]
    migration = root / "app" / "db" / "alembic" / "versions" / "0061_notification_outbox_default_on.py"
    text = migration.read_text()

    assert 'revision = "0061_outbox_default_on"' in text
    assert 'down_revision = "0060_ai_accountant_default_on"' in text
    for key in [
        "notification_outbox_runner_enabled",
        "notification_outbox_runner_limit",
        "notification_outbox_backoff_base_seconds",
        "notification_outbox_backoff_max_seconds",
        "notification_outbox_cleanup_enabled",
    ]:
        assert key in text


def test_slack_notifications_default_on_migration_revision_and_key():
    root = Path(__file__).resolve().parents[1]
    migration = root / "app" / "db" / "alembic" / "versions" / "0062_slack_notifications_default_on.py"
    text = migration.read_text()

    assert 'revision = "0062_slack_default_on"' in text
    assert 'down_revision = "0061_outbox_default_on"' in text
    assert "slack_notifications_enabled" in text


def test_ai_accountant_review_context_limits_migration_revision_and_keys():
    root = Path(__file__).resolve().parents[1]
    migration = root / "app" / "db" / "alembic" / "versions" / "0063_ai_accountant_review_context_limits.py"
    text = migration.read_text()

    assert 'revision = "0063_ai_acct_ctx_limits"' in text
    assert 'down_revision = "0062_slack_default_on"' in text
    assert "ai_accountant_monitor_review_max_rows" in text
    assert "ai_accountant_monitor_review_max_exception_rows" in text


def test_ai_accountant_workflow_profile_migration_revision_and_key():
    root = Path(__file__).resolve().parents[1]
    migration = root / "app" / "db" / "alembic" / "versions" / "0064_ai_accountant_workflow_profile.py"
    text = migration.read_text()

    assert 'revision = "0064_ai_acct_workflow_profile"' in text
    assert 'down_revision = "0063_ai_acct_ctx_limits"' in text
    assert "ai_workflow_profile_accounting" in text


def test_ai_workflow_profile_defaults_migration_revision_and_keys():
    root = Path(__file__).resolve().parents[1]
    migration = root / "app" / "db" / "alembic" / "versions" / "0065_ai_workflow_profile_defaults.py"
    text = migration.read_text()

    assert 'revision = "0065_ai_workflow_profiles"' in text
    assert 'down_revision = "0064_ai_acct_workflow_profile"' in text
    for key in [
        "ai_workflow_profile_listing",
        "ai_workflow_profile_intake",
        "ai_workflow_profile_comp",
        "ai_workflow_profile_risk",
        "ai_workflow_profile_accounting",
    ]:
        assert key in text


def test_system_health_alerts_default_on_migration_revision_and_keys():
    root = Path(__file__).resolve().parents[1]
    migration = root / "app" / "db" / "alembic" / "versions" / "0066_system_health_alerts_default_on.py"
    text = migration.read_text()

    assert 'revision = "0066_health_alerts_default_on"' in text
    assert 'down_revision = "0065_ai_workflow_profiles"' in text
    for key in [
        "health_auto_alert_critical_enabled",
        "health_auto_alert_cooldown_minutes",
        "slack_notify_system_health_critical",
        "notification_route_system_health_critical",
        "slack_channel_system_health_critical",
    ]:
        assert key in text


def test_listing_wizard_recent_product_limit_migration_revision_and_key():
    root = Path(__file__).resolve().parents[1]
    migration = root / "app" / "db" / "alembic" / "versions" / "0067_listing_wizard_recent_product_limit.py"
    text = migration.read_text()

    assert 'revision = "0067_lw_recent_products"' in text
    assert 'down_revision = "0066_health_alerts_default_on"' in text
    assert "listing_wizard_recent_product_limit" in text
    assert "'75'" in text
