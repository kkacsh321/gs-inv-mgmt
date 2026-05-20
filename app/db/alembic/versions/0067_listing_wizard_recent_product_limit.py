"""default listing wizard recent product limit

Revision ID: 0067_lw_recent_products
Revises: 0066_health_alerts_default_on
Create Date: 2026-05-11
"""

from alembic import op


revision = "0067_lw_recent_products"
down_revision = "0066_health_alerts_default_on"
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
            'listing_wizard_recent_product_limit',
            '75',
            'int',
            'Maximum recent products shown in Listing Wizard Step 1 before product search is used.',
            'migration_0067_listing_wizard_recent_product_limit',
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
    # Preserve runtime tuning on downgrade; operators may have intentionally
    # changed the Listing Wizard dropdown limit after this migration applied.
    pass
