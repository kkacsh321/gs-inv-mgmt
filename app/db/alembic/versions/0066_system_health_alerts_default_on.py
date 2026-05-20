"""default system health critical alerts on

Revision ID: 0066_health_alerts_default_on
Revises: 0065_ai_workflow_profiles
Create Date: 2026-05-10
"""

from alembic import op


revision = "0066_health_alerts_default_on"
down_revision = "0065_ai_workflow_profiles"
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
                (
                    'health_auto_alert_critical_enabled',
                    'true',
                    'bool',
                    'Enable automatic System Health critical-signal alert dispatch.'
                ),
                (
                    'health_auto_alert_cooldown_minutes',
                    '60',
                    'int',
                    'Cooldown minutes before repeating identical System Health critical alerts.'
                ),
                (
                    'slack_notify_system_health_critical',
                    'true',
                    'bool',
                    'Send Slack notifications when System Health critical-signal thresholds are breached.'
                ),
                (
                    'notification_route_system_health_critical',
                    'slack',
                    'str',
                    'Notification route for system-health critical events (`slack`, `email`, `both`, `disabled`).'
                ),
                (
                    'slack_channel_system_health_critical',
                    '',
                    'str',
                    'Optional channel override for System Health critical alerts.'
                )
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
            'migration_0066_system_health_alerts_default_on',
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
            'health_auto_alert_critical_enabled',
            'slack_notify_system_health_critical'
        )
           OR runtime_settings.value IS NULL
           OR btrim(runtime_settings.value) = ''
        """
    )


def downgrade() -> None:
    # Preserve alert settings on downgrade; operators may have intentionally
    # changed routing or alert policy after this migration applied.
    pass
