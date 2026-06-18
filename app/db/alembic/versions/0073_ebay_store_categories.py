"""Add eBay store category cache/management table.

Revision ID: 0073_ebay_store_categories
Revises: 0072_customer_chat_defaults
Create Date: 2026-06-15
"""

from alembic import op
import sqlalchemy as sa


revision = "0073_ebay_store_categories"
down_revision = "0072_customer_chat_defaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ebay_store_categories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("environment", sa.String(length=32), nullable=False, server_default="local"),
        sa.Column("marketplace_id", sa.String(length=32), nullable=False, server_default="EBAY_US"),
        sa.Column("category_name", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("category_path", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("parent_path", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("external_category_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(length=128), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "environment",
            "marketplace_id",
            "category_path",
            name="uq_ebay_store_category_env_market_path",
        ),
    )
    op.create_index(
        "ix_ebay_store_category_lookup",
        "ebay_store_categories",
        ["environment", "marketplace_id", "is_active", "sort_order"],
    )
    op.create_index("ix_ebay_store_categories_environment", "ebay_store_categories", ["environment"])
    op.create_index("ix_ebay_store_categories_marketplace_id", "ebay_store_categories", ["marketplace_id"])
    op.create_index("ix_ebay_store_categories_category_name", "ebay_store_categories", ["category_name"])
    op.create_index("ix_ebay_store_categories_category_path", "ebay_store_categories", ["category_path"])
    op.create_index(
        "ix_ebay_store_categories_external_category_id",
        "ebay_store_categories",
        ["external_category_id"],
    )
    op.create_index("ix_ebay_store_categories_sort_order", "ebay_store_categories", ["sort_order"])
    op.create_index("ix_ebay_store_categories_is_active", "ebay_store_categories", ["is_active"])


def downgrade() -> None:
    op.drop_index("ix_ebay_store_categories_is_active", table_name="ebay_store_categories")
    op.drop_index("ix_ebay_store_categories_sort_order", table_name="ebay_store_categories")
    op.drop_index("ix_ebay_store_categories_external_category_id", table_name="ebay_store_categories")
    op.drop_index("ix_ebay_store_categories_category_path", table_name="ebay_store_categories")
    op.drop_index("ix_ebay_store_categories_category_name", table_name="ebay_store_categories")
    op.drop_index("ix_ebay_store_categories_marketplace_id", table_name="ebay_store_categories")
    op.drop_index("ix_ebay_store_categories_environment", table_name="ebay_store_categories")
    op.drop_index("ix_ebay_store_category_lookup", table_name="ebay_store_categories")
    op.drop_table("ebay_store_categories")
