"""Add eBay store category sync status fields.

Revision ID: 0074_ebay_store_cat_sync
Revises: 0073_ebay_store_categories
Create Date: 2026-06-15
"""

from alembic import op
import sqlalchemy as sa


revision = "0074_ebay_store_cat_sync"
down_revision = "0073_ebay_store_categories"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ebay_store_categories", sa.Column("last_synced_at", sa.DateTime(), nullable=True))
    op.add_column(
        "ebay_store_categories",
        sa.Column("last_sync_status", sa.String(length=32), nullable=False, server_default=""),
    )
    op.add_column(
        "ebay_store_categories",
        sa.Column("last_sync_message", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_ebay_store_categories_last_synced_at", "ebay_store_categories", ["last_synced_at"])
    op.create_index("ix_ebay_store_categories_last_sync_status", "ebay_store_categories", ["last_sync_status"])


def downgrade() -> None:
    op.drop_index("ix_ebay_store_categories_last_sync_status", table_name="ebay_store_categories")
    op.drop_index("ix_ebay_store_categories_last_synced_at", table_name="ebay_store_categories")
    op.drop_column("ebay_store_categories", "last_sync_message")
    op.drop_column("ebay_store_categories", "last_sync_status")
    op.drop_column("ebay_store_categories", "last_synced_at")
