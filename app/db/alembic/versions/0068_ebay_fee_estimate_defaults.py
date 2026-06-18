"""default ebay fee estimate settings

Revision ID: 0068_ebay_fee_defaults
Revises: 0067_lw_recent_products
Create Date: 2026-05-21
"""

from alembic import op


revision = "0068_ebay_fee_defaults"
down_revision = "0067_lw_recent_products"
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
                    'ebay_fee_estimate_final_value_rate_percent',
                    '13.25',
                    'float',
                    'Default eBay final value fee percentage for pre-listing estimates.'
                ),
                (
                    'ebay_fee_estimate_final_value_fixed_per_order_usd',
                    '0.30',
                    'float',
                    'Default eBay fixed per-order fee for pre-listing estimates.'
                ),
                (
                    'ebay_fee_estimate_payment_rate_percent',
                    '0.0',
                    'float',
                    'Optional additional eBay percentage surcharge for pre-listing estimates; default 0.'
                ),
                (
                    'ebay_fee_estimate_payment_fixed_per_order_usd',
                    '0.0',
                    'float',
                    'Optional additional eBay fixed surcharge for pre-listing estimates; default 0.'
                ),
                (
                    'ebay_fee_estimate_promoted_rate_percent',
                    '0.0',
                    'float',
                    'Default promoted listing percentage for pre-listing estimates.'
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
            'migration_0068_ebay_fee_defaults',
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
    # Preserve runtime tuning on downgrade; operators may have calibrated these
    # fee assumptions after this migration applied.
    pass
