"""default notification outbox runner on

Revision ID: 0061_outbox_default_on
Revises: 0060_ai_accountant_default_on
Create Date: 2026-05-08
"""

from alembic import op


revision = "0061_outbox_default_on"
down_revision = "0060_ai_accountant_default_on"
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
                ('notification_outbox_runner_enabled', 'true', 'bool', 'Enable sync-runner outbox processor.'),
                ('notification_outbox_runner_limit', '50', 'int', 'Max queued outbox rows to process per sync-runner pass.'),
                ('notification_outbox_backoff_base_seconds', '60', 'int', 'Base retry backoff seconds for notification outbox.'),
                ('notification_outbox_backoff_max_seconds', '3600', 'int', 'Max retry backoff seconds for notification outbox.'),
                ('notification_outbox_retain_sent_days', '14', 'int', 'Retention window for sent outbox rows.'),
                ('notification_outbox_retain_failed_days', '30', 'int', 'Retention window for failed outbox rows.'),
                ('notification_outbox_cleanup_enabled', 'true', 'bool', 'Enable daily outbox retention cleanup.'),
                ('notification_outbox_cleanup_timezone', 'America/Denver', 'str', 'IANA timezone for outbox cleanup schedule.'),
                ('notification_outbox_cleanup_local_time', '03:15', 'str', 'Local HH:MM time for outbox cleanup schedule.')
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
            'migration_0061_notification_outbox_default_on',
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
            'notification_outbox_runner_enabled',
            'notification_outbox_cleanup_enabled'
        )
           OR runtime_settings.value IS NULL
           OR btrim(runtime_settings.value) = ''
        """
    )


def downgrade() -> None:
    # Preserve runtime settings on downgrade; operators may have edited these
    # after the migration applied.
    pass
