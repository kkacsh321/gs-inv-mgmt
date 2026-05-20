"""add ebay category aspects cache table

Revision ID: 0056_ebay_category_aspects_cache
Revises: 0055_report_time_window_idx
Create Date: 2026-04-26
"""

from alembic import op
import sqlalchemy as sa


revision = "0056_ebay_category_aspects_cache"
down_revision = "0055_report_time_window_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ebay_category_aspects",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("environment", sa.String(length=32), nullable=False, server_default="local"),
        sa.Column("marketplace_id", sa.String(length=32), nullable=False, server_default="EBAY_US"),
        sa.Column("category_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("aspects_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("required_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_count", sa.Integer(), nullable=False, server_default="0"),
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
            "category_id",
            name="uq_ebay_category_aspect_env_market_category",
        ),
    )
    op.create_index(
        "ix_ebay_category_aspects_environment",
        "ebay_category_aspects",
        ["environment"],
        unique=False,
    )
    op.create_index(
        "ix_ebay_category_aspects_marketplace_id",
        "ebay_category_aspects",
        ["marketplace_id"],
        unique=False,
    )
    op.create_index(
        "ix_ebay_category_aspects_category_id",
        "ebay_category_aspects",
        ["category_id"],
        unique=False,
    )
    op.create_index(
        "ix_ebay_category_aspects_last_seen_at",
        "ebay_category_aspects",
        ["last_seen_at"],
        unique=False,
    )
    op.create_index(
        "ix_ebay_category_aspect_lookup",
        "ebay_category_aspects",
        ["environment", "marketplace_id", "category_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ebay_category_aspect_lookup", table_name="ebay_category_aspects")
    op.drop_index("ix_ebay_category_aspects_last_seen_at", table_name="ebay_category_aspects")
    op.drop_index("ix_ebay_category_aspects_category_id", table_name="ebay_category_aspects")
    op.drop_index("ix_ebay_category_aspects_marketplace_id", table_name="ebay_category_aspects")
    op.drop_index("ix_ebay_category_aspects_environment", table_name="ebay_category_aspects")
    op.drop_table("ebay_category_aspects")
