"""default ai accountant automation on

Revision ID: 0060_ai_accountant_default_on
Revises: 0059_ai_accountant_permission
Create Date: 2026-05-08
"""

from alembic import op


revision = "0060_ai_accountant_default_on"
down_revision = "0059_ai_accountant_permission"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        WITH envs AS (
            SELECT DISTINCT environment FROM runtime_settings
            UNION
            SELECT 'local'
        ),
        defaults(key, value, value_type, description) AS (
            VALUES
                ('ai_accountant_monitor_enabled', 'true', 'bool', 'Enable sync-runner scheduled AI Accountant monitor pass.'),
                ('ai_accountant_monitor_schedule_mode', 'interval', 'str', 'AI Accountant monitor schedule mode.'),
                ('ai_accountant_monitor_interval_hours', '6', 'int', 'Run the AI Accountant monitor every N hours in interval mode.'),
                ('ai_accountant_monitor_timezone', 'America/Denver', 'str', 'IANA timezone used by the scheduled AI Accountant monitor.'),
                ('ai_accountant_monitor_local_time', '08:30', 'str', 'Local HH:MM trigger for daily AI Accountant monitor mode.'),
                ('ai_accountant_monitor_lookback_days', '30', 'int', 'Lookback window in days for scheduled AI Accountant monitor checks.'),
                ('ai_accountant_monitor_min_severity', 'P1', 'str', 'Minimum severity to record/alert from scheduled AI Accountant monitor.'),
                ('ai_accountant_monitor_slack_enabled', 'true', 'bool', 'Queue Slack notification-outbox messages for scheduled AI Accountant monitor findings.'),
                ('notification_route_ai_accountant_monitor', 'slack', 'str', 'Notification route for scheduled AI Accountant monitor alerts.'),
                ('ai_accountant_monitor_record_empty', 'false', 'bool', 'Record an in-app AI Accountant monitor message even when no actionable findings exist.'),
                ('ai_accountant_monitor_llm_review_enabled', 'true', 'bool', 'Run and audit a read-only AI Accountant LLM review during scheduled monitor passes.'),
                ('ai_accountant_chat_ai_enabled', 'true', 'bool', 'Enable the AI Accountant identity/system prompt for accounting/tax chat questions.'),
                ('ai_accountant_web_research_enabled', 'true', 'bool', 'Allow AI Accountant chat/Slack answers to fetch external web-research context for tax/accounting questions.'),
                ('ai_accountant_web_research_limit', '5', 'int', 'Maximum external web-search rows attached to AI Accountant research context.'),
                ('ai_accountant_web_research_timeout_seconds', '10', 'int', 'HTTP timeout for optional AI Accountant external web research.')
        )
        INSERT INTO runtime_settings (
            environment,
            key,
            value,
            value_type,
            description,
            updated_by,
            is_active,
            created_at,
            updated_at
        )
        SELECT
            envs.environment,
            defaults.key,
            defaults.value,
            defaults.value_type,
            defaults.description,
            'migration_0060_ai_accountant_default_on',
            true,
            CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP
        FROM envs
        CROSS JOIN defaults
        ON CONFLICT (environment, key) DO UPDATE
        SET
            value = EXCLUDED.value,
            value_type = EXCLUDED.value_type,
            description = EXCLUDED.description,
            updated_by = EXCLUDED.updated_by,
            is_active = true,
            updated_at = CURRENT_TIMESTAMP
        WHERE runtime_settings.key IN (
            'ai_accountant_monitor_enabled',
            'ai_accountant_monitor_slack_enabled',
            'ai_accountant_monitor_llm_review_enabled',
            'ai_accountant_chat_ai_enabled',
            'ai_accountant_web_research_enabled',
            'notification_route_ai_accountant_monitor'
        )
           OR runtime_settings.value IS NULL
           OR btrim(runtime_settings.value) = ''
        """
    )


def downgrade() -> None:
    # Do not disable automation on downgrade; these runtime rows may have existed
    # before the migration and may have been intentionally edited afterward.
    pass
