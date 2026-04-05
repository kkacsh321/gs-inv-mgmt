"""add coin reference catalog table

Revision ID: 0029_coin_reference_catalog
Revises: 0028_listing_review
Create Date: 2026-03-26
"""

from alembic import op
import sqlalchemy as sa


revision = "0029_coin_reference_catalog"
down_revision = "0028_listing_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "coin_reference_catalog",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("coin_name", sa.String(length=255), nullable=False),
        sa.Column("country", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("issuer", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("denomination", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("series", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("year_start", sa.Integer(), nullable=True),
        sa.Column("year_end", sa.Integer(), nullable=True),
        sa.Column("mint_mark", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("composition", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("metal_type", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("weight_grams", sa.Numeric(10, 4), nullable=True),
        sa.Column("asw_oz", sa.Numeric(10, 4), nullable=True),
        sa.Column("diameter_mm", sa.Numeric(10, 2), nullable=True),
        sa.Column("thickness_mm", sa.Numeric(10, 2), nullable=True),
        sa.Column("km_number", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("pcgs_no", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("ngc_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("mintage", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("estimated_value_low", sa.Numeric(12, 2), nullable=True),
        sa.Column("estimated_value_high", sa.Numeric(12, 2), nullable=True),
        sa.Column("price_source", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("source_url", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("tags", sa.Text(), nullable=False, server_default=""),
        sa.Column("obverse_description", sa.Text(), nullable=False, server_default=""),
        sa.Column("reverse_description", sa.Text(), nullable=False, server_default=""),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_coin_reference_catalog_coin_name", "coin_reference_catalog", ["coin_name"], unique=False)
    op.create_index("ix_coin_reference_catalog_country", "coin_reference_catalog", ["country"], unique=False)
    op.create_index("ix_coin_reference_catalog_issuer", "coin_reference_catalog", ["issuer"], unique=False)
    op.create_index("ix_coin_reference_catalog_denomination", "coin_reference_catalog", ["denomination"], unique=False)
    op.create_index("ix_coin_reference_catalog_series", "coin_reference_catalog", ["series"], unique=False)
    op.create_index("ix_coin_reference_catalog_year_start", "coin_reference_catalog", ["year_start"], unique=False)
    op.create_index("ix_coin_reference_catalog_year_end", "coin_reference_catalog", ["year_end"], unique=False)
    op.create_index("ix_coin_reference_catalog_mint_mark", "coin_reference_catalog", ["mint_mark"], unique=False)
    op.create_index("ix_coin_reference_catalog_metal_type", "coin_reference_catalog", ["metal_type"], unique=False)
    op.create_index("ix_coin_reference_catalog_km_number", "coin_reference_catalog", ["km_number"], unique=False)
    op.create_index("ix_coin_reference_catalog_pcgs_no", "coin_reference_catalog", ["pcgs_no"], unique=False)
    op.create_index("ix_coin_reference_catalog_ngc_id", "coin_reference_catalog", ["ngc_id"], unique=False)
    op.create_index("ix_coin_reference_catalog_is_active", "coin_reference_catalog", ["is_active"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_coin_reference_catalog_is_active", table_name="coin_reference_catalog")
    op.drop_index("ix_coin_reference_catalog_ngc_id", table_name="coin_reference_catalog")
    op.drop_index("ix_coin_reference_catalog_pcgs_no", table_name="coin_reference_catalog")
    op.drop_index("ix_coin_reference_catalog_km_number", table_name="coin_reference_catalog")
    op.drop_index("ix_coin_reference_catalog_metal_type", table_name="coin_reference_catalog")
    op.drop_index("ix_coin_reference_catalog_mint_mark", table_name="coin_reference_catalog")
    op.drop_index("ix_coin_reference_catalog_year_end", table_name="coin_reference_catalog")
    op.drop_index("ix_coin_reference_catalog_year_start", table_name="coin_reference_catalog")
    op.drop_index("ix_coin_reference_catalog_series", table_name="coin_reference_catalog")
    op.drop_index("ix_coin_reference_catalog_denomination", table_name="coin_reference_catalog")
    op.drop_index("ix_coin_reference_catalog_issuer", table_name="coin_reference_catalog")
    op.drop_index("ix_coin_reference_catalog_country", table_name="coin_reference_catalog")
    op.drop_index("ix_coin_reference_catalog_coin_name", table_name="coin_reference_catalog")
    op.drop_table("coin_reference_catalog")
