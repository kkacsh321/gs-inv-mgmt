"""add ebay category suggestions cache table

Revision ID: 0043_ebay_category_cache
Revises: 0042_purchase_documents
Create Date: 2026-04-10
"""

from alembic import op
import sqlalchemy as sa


revision = "0043_ebay_category_cache"
down_revision = "0042_purchase_documents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ebay_category_suggestions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("environment", sa.String(length=32), nullable=False, server_default="local"),
        sa.Column("marketplace_id", sa.String(length=32), nullable=False, server_default="EBAY_US"),
        sa.Column("query_raw", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("query_norm", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("category_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("category_name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("path", sa.Text(), nullable=False, server_default=""),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="ebay_taxonomy"),
        sa.Column("hit_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "environment",
            "marketplace_id",
            "query_norm",
            "category_id",
            name="uq_ebay_category_suggestion_env_market_query_category",
        ),
    )
    op.create_index(
        "ix_ebay_category_suggestions_environment",
        "ebay_category_suggestions",
        ["environment"],
        unique=False,
    )
    op.create_index(
        "ix_ebay_category_suggestions_marketplace_id",
        "ebay_category_suggestions",
        ["marketplace_id"],
        unique=False,
    )
    op.create_index(
        "ix_ebay_category_suggestions_query_norm",
        "ebay_category_suggestions",
        ["query_norm"],
        unique=False,
    )
    op.create_index(
        "ix_ebay_category_suggestions_category_id",
        "ebay_category_suggestions",
        ["category_id"],
        unique=False,
    )
    op.create_index(
        "ix_ebay_category_suggestions_last_seen_at",
        "ebay_category_suggestions",
        ["last_seen_at"],
        unique=False,
    )
    op.create_index(
        "ix_ebay_category_suggestion_lookup",
        "ebay_category_suggestions",
        ["environment", "marketplace_id", "query_norm"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ebay_category_suggestion_lookup", table_name="ebay_category_suggestions")
    op.drop_index("ix_ebay_category_suggestions_last_seen_at", table_name="ebay_category_suggestions")
    op.drop_index("ix_ebay_category_suggestions_category_id", table_name="ebay_category_suggestions")
    op.drop_index("ix_ebay_category_suggestions_query_norm", table_name="ebay_category_suggestions")
    op.drop_index("ix_ebay_category_suggestions_marketplace_id", table_name="ebay_category_suggestions")
    op.drop_index("ix_ebay_category_suggestions_environment", table_name="ebay_category_suggestions")
    op.drop_table("ebay_category_suggestions")
