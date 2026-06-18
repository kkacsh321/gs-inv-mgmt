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


def test_ebay_fee_estimate_defaults_migration_revision_and_keys():
    root = Path(__file__).resolve().parents[1]
    migration = root / "app" / "db" / "alembic" / "versions" / "0068_ebay_fee_estimate_defaults.py"
    text = migration.read_text()

    assert 'revision = "0068_ebay_fee_defaults"' in text
    assert 'down_revision = "0067_lw_recent_products"' in text
    for key in [
        "ebay_fee_estimate_final_value_rate_percent",
        "ebay_fee_estimate_final_value_fixed_per_order_usd",
        "ebay_fee_estimate_payment_rate_percent",
        "ebay_fee_estimate_payment_fixed_per_order_usd",
        "ebay_fee_estimate_promoted_rate_percent",
    ]:
        assert key in text


def test_goldie_ai_accountant_identity_migration_revision_and_keys():
    root = Path(__file__).resolve().parents[1]
    migration = root / "app" / "db" / "alembic" / "versions" / "0069_goldie_ai_accountant_identity.py"
    text = migration.read_text()

    assert 'revision = "0069_goldie_ai_accountant"' in text
    assert 'down_revision = "0068_ebay_fee_defaults"' in text
    assert "Goldie" in text
    for key in [
        "accountant_llm_system_message",
        "ai_accountant_chat_instruction",
        "ai_accountant_monitor_review_instruction",
    ]:
        assert key in text


def test_business_agents_migration_revision_and_keys():
    root = Path(__file__).resolve().parents[1]
    migration = root / "app" / "db" / "alembic" / "versions" / "0070_business_agents_wizard_defaults.py"
    text = migration.read_text()

    assert 'revision = "0070_business_agents"' in text
    assert 'down_revision = "0069_goldie_ai_accountant"' in text
    for key in [
        "business_chat_room_enabled",
        "business_chat_room_agents_csv",
        "slack_ops_intent_listing_enabled",
        "slack_ops_listing_system_message",
        "slack_ops_intake_system_message",
    ]:
        assert key in text


def test_customer_chat_defaults_migration_revision_and_keys():
    root = Path(__file__).resolve().parents[1]
    migration = root / "app" / "db" / "alembic" / "versions" / "0072_customer_chat_runtime_defaults.py"
    text = migration.read_text()

    assert 'revision = "0072_customer_chat_defaults"' in text
    assert 'down_revision = "0071_customers"' in text
    for key in [
        "chat_allowed_domains_ops_csv",
        "chat_allowed_domains_admin_csv",
        "slack_ops_intent_customer_enabled",
        "slack_ops_customer_system_message",
        "slack_ops_customer_instruction",
    ]:
        assert key in text
    assert "rtrim(value, ', ') || ',customers'" in text


def test_ebay_store_categories_migration_revision_and_table():
    root = Path(__file__).resolve().parents[1]
    migration = root / "app" / "db" / "alembic" / "versions" / "0073_ebay_store_categories.py"
    text = migration.read_text()

    assert 'revision = "0073_ebay_store_categories"' in text
    assert 'down_revision = "0072_customer_chat_defaults"' in text
    assert "ebay_store_categories" in text
    assert "store category" in text.lower()


def test_ebay_store_category_sync_status_migration_revision_and_fields():
    root = Path(__file__).resolve().parents[1]
    migration = root / "app" / "db" / "alembic" / "versions" / "0074_ebay_store_cat_sync.py"
    text = migration.read_text()

    assert 'revision = "0074_ebay_store_cat_sync"' in text
    assert 'down_revision = "0073_ebay_store_categories"' in text
    assert "last_synced_at" in text
    assert "last_sync_status" in text
    assert "last_sync_message" in text


def test_ebay_store_category_sync_defaults_migration_revision_and_keys():
    root = Path(__file__).resolve().parents[1]
    migration = root / "app" / "db" / "alembic" / "versions" / "0075_ebay_store_cat_defaults.py"
    text = migration.read_text()

    assert 'revision = "0075_ebay_store_cat_defaults"' in text
    assert 'down_revision = "0074_ebay_store_cat_sync"' in text
    assert "sync_job_ebay_store_categories_sync_enabled" in text
    assert "sync_job_ebay_store_categories_sync_interval_hours" in text
    assert "sync_job_ebay_store_categories_sync_deactivate_missing" in text
