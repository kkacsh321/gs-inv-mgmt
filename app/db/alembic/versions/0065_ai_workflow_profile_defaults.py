"""default ai workflow profile route keys

Revision ID: 0065_ai_workflow_profiles
Revises: 0064_ai_acct_workflow_profile
Create Date: 2026-05-09
"""

from alembic import op


revision = "0065_ai_workflow_profiles"
down_revision = "0064_ai_acct_workflow_profile"
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
        defaults(key, description) AS (
            VALUES
                (
                    'ai_workflow_profile_listing',
                    'Preferred AI runtime profile id for listing workflow calls (blank uses default chain order).'
                ),
                (
                    'ai_workflow_profile_intake',
                    'Preferred AI runtime profile id for intake workflow calls (blank uses default chain order).'
                ),
                (
                    'ai_workflow_profile_comp',
                    'Preferred AI runtime profile id for comp workflow calls (blank uses default chain order).'
                ),
                (
                    'ai_workflow_profile_risk',
                    'Preferred AI runtime profile id for risk workflow calls (blank uses default chain order).'
                ),
                (
                    'ai_workflow_profile_accounting',
                    'Preferred AI runtime profile id for accounting/AI Accountant workflow calls (blank uses default chain order).'
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
            '',
            'str',
            defaults.description,
            'migration_0065_ai_workflow_profile_defaults',
            true,
            CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP
        FROM envs
        CROSS JOIN defaults
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
    # Preserve workflow profile settings on downgrade; operators may have
    # selected workflow-specific model profiles after this migration applied.
    pass
