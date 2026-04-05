"""initial schema

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-03-23
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


product_status_enum = postgresql.ENUM(
    "active", "archived", name="product_status_enum", create_type=False
)
listing_status_enum = postgresql.ENUM(
    "draft", "active", "ended", name="listing_status_enum", create_type=False
)
media_type_enum = postgresql.ENUM(
    "image", "video", "other", name="media_type_enum", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    product_status_enum.create(bind, checkfirst=True)
    listing_status_enum.create(bind, checkfirst=True)
    media_type_enum.create(bind, checkfirst=True)

    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False, server_default="bullion"),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("metal_type", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("weight_oz", sa.Numeric(10, 4), nullable=True),
        sa.Column("acquisition_cost", sa.Numeric(12, 2), nullable=True),
        sa.Column("current_quantity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", product_status_enum, nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_products_sku", "products", ["sku"], unique=True)

    op.create_table(
        "marketplace_listings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("marketplace", sa.String(length=64), nullable=False),
        sa.Column("external_listing_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("listing_title", sa.String(length=255), nullable=False),
        sa.Column("listing_price", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("listing_status", listing_status_enum, nullable=False, server_default="draft"),
        sa.Column("quantity_listed", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("marketplace", "external_listing_id", name="uq_marketplace_listing"),
    )
    op.create_index(
        "ix_marketplace_listings_marketplace", "marketplace_listings", ["marketplace"], unique=False
    )

    op.create_table(
        "sales",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="SET NULL"), nullable=True),
        sa.Column(
            "listing_id",
            sa.Integer(),
            sa.ForeignKey("marketplace_listings.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("marketplace", sa.String(length=64), nullable=False),
        sa.Column("external_order_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("sold_price", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("fees", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("shipping_cost", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("quantity_sold", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_sales_marketplace", "sales", ["marketplace"], unique=False)

    op.create_table(
        "media_assets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="SET NULL"), nullable=True),
        sa.Column(
            "listing_id",
            sa.Integer(),
            sa.ForeignKey("marketplace_listings.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("media_type", media_type_enum, nullable=False, server_default="image"),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column(
            "content_type", sa.String(length=128), nullable=False, server_default="application/octet-stream"
        ),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("s3_bucket", sa.String(length=255), nullable=False),
        sa.Column("s3_key", sa.String(length=512), nullable=False),
        sa.Column("s3_url", sa.Text(), nullable=False),
        sa.Column("uploaded_by", sa.String(length=128), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_unique_constraint("uq_media_assets_s3_key", "media_assets", ["s3_key"])


def downgrade() -> None:
    op.drop_constraint("uq_media_assets_s3_key", "media_assets", type_="unique")
    op.drop_table("media_assets")

    op.drop_index("ix_sales_marketplace", table_name="sales")
    op.drop_table("sales")

    op.drop_index("ix_marketplace_listings_marketplace", table_name="marketplace_listings")
    op.drop_table("marketplace_listings")

    op.drop_index("ix_products_sku", table_name="products")
    op.drop_table("products")

    bind = op.get_bind()
    media_type_enum.drop(bind, checkfirst=True)
    listing_status_enum.drop(bind, checkfirst=True)
    product_status_enum.drop(bind, checkfirst=True)
