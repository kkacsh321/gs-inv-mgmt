"""default slack notifications on

Revision ID: 0062_slack_default_on
Revises: 0061_outbox_default_on
Create Date: 2026-05-08
"""

from alembic import op


revision = "0062_slack_default_on"
down_revision = "0061_outbox_default_on"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        WITH envs AS (
            SELECT DISTINCT environment FROM runtime_settings
            UNION
            SELECT 'local'
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
            'slack_notifications_enabled',
            'true',
            'bool',
            'Master toggle for Slack notifications.',
            'migration_0062_slack_notifications_default_on',
            true,
            CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP
        FROM envs
        ON CONFLICT (environment, key) DO UPDATE
        SET
            value = EXCLUDED.value,
            value_type = EXCLUDED.value_type,
            description = EXCLUDED.description,
            updated_by = EXCLUDED.updated_by,
            is_active = true,
            updated_at = CURRENT_TIMESTAMP
        WHERE runtime_settings.key = 'slack_notifications_enabled'
           OR runtime_settings.value IS NULL
           OR btrim(runtime_settings.value) = ''
        """
    )


def downgrade() -> None:
    # Preserve runtime settings on downgrade; operators may have edited this
    # after the migration applied.
    pass
