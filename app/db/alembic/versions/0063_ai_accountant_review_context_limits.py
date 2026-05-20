"""default ai accountant review context limits

Revision ID: 0063_ai_acct_ctx_limits
Revises: 0062_slack_default_on
Create Date: 2026-05-09
"""

from alembic import op


revision = "0063_ai_acct_ctx_limits"
down_revision = "0062_slack_default_on"
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
                    'ai_accountant_monitor_review_max_rows',
                    '25',
                    'int',
                    'Maximum monitor rows sent to scheduled AI Accountant LLM reviews.'
                ),
                (
                    'ai_accountant_monitor_review_max_exception_rows',
                    '25',
                    'int',
                    'Maximum accounting exception rows sent to scheduled AI Accountant LLM reviews.'
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
            'migration_0063_ai_accountant_review_context_limits',
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
        WHERE runtime_settings.value IS NULL
           OR btrim(runtime_settings.value) = ''
        """
    )


def downgrade() -> None:
    # Preserve runtime settings on downgrade; operators may have edited these
    # limits after the migration applied.
    pass
