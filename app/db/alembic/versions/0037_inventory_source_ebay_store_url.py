"""add ebay seller store url to inventory sources

Revision ID: 0037_source_ebay_store
Revises: 0036_product_ai_comp
Create Date: 2026-04-01
"""

from alembic import op
import sqlalchemy as sa


revision = "0037_source_ebay_store"
down_revision = "0036_product_ai_comp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "inventory_sources",
        sa.Column("ebay_store_url", sa.String(length=512), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("inventory_sources", "ebay_store_url")

