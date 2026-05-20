"""default ai accountant workflow profile route

Revision ID: 0064_ai_acct_workflow_profile
Revises: 0063_ai_acct_ctx_limits
Create Date: 2026-05-09
"""

from alembic import op


revision = "0064_ai_acct_workflow_profile"
down_revision = "0063_ai_acct_ctx_limits"
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
            'ai_workflow_profile_accounting',
            '',
            'str',
            'Preferred AI runtime profile id for accounting/AI Accountant workflow calls (blank uses default chain order).',
            'migration_0064_ai_accountant_workflow_profile',
            true,
            CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP
        FROM envs
        ON CONFLICT (environment, key) DO UPDATE
        SET
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
    # Preserve runtime settings on downgrade; operators may have selected a
    # dedicated accounting profile after this migration applied.
    pass
