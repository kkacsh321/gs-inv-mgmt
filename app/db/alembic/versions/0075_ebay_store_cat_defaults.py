"""Default eBay store category scheduled sync settings.

Revision ID: 0075_ebay_store_cat_defaults
Revises: 0074_ebay_store_cat_sync
Create Date: 2026-06-15
"""

from alembic import op


revision = "0075_ebay_store_cat_defaults"
down_revision = "0074_ebay_store_cat_sync"
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
                    'sync_job_ebay_store_categories_sync_enabled',
                    'true',
                    'bool',
                    'Enable sync-runner scheduled eBay store category hierarchy sync.'
                ),
                (
                    'sync_job_ebay_store_categories_sync_interval_hours',
                    '24',
                    'int',
                    'Minimum hours between scheduled eBay store category hierarchy syncs.'
                ),
                (
                    'sync_job_ebay_store_categories_sync_deactivate_missing',
                    'false',
                    'bool',
                    'Deactivate previously eBay-synced local store categories missing from the latest GetStore response.'
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
            'migration_0075_ebay_store_category_sync_defaults',
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
        WHERE runtime_settings.key = 'sync_job_ebay_store_categories_sync_enabled'
           OR runtime_settings.value IS NULL
           OR btrim(runtime_settings.value) = ''
        """
    )


def downgrade() -> None:
    # Preserve runtime settings on downgrade; operators may have edited these.
    pass
