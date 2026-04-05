"""add product cost and ebay purchase fields to products and lots

Revision ID: 0035_product_lot_ebay
Revises: 0034_integration_approvals
Create Date: 2026-04-01 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0035_product_lot_ebay"
down_revision = "0034_integration_approvals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("products", sa.Column("product_cost", sa.Numeric(12, 2), nullable=True))
    op.add_column(
        "products",
        sa.Column("ebay_purchase", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "products",
        sa.Column("ebay_purchase_item_id", sa.String(length=128), nullable=False, server_default=""),
    )
    op.add_column(
        "products",
        sa.Column("ebay_purchase_url", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_products_ebay_purchase", "products", ["ebay_purchase"], unique=False)

    op.add_column(
        "purchase_lots",
        sa.Column("ebay_purchase", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "purchase_lots",
        sa.Column("ebay_purchase_item_id", sa.String(length=128), nullable=False, server_default=""),
    )
    op.add_column(
        "purchase_lots",
        sa.Column("ebay_purchase_url", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_purchase_lots_ebay_purchase", "purchase_lots", ["ebay_purchase"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_purchase_lots_ebay_purchase", table_name="purchase_lots")
    op.drop_column("purchase_lots", "ebay_purchase_url")
    op.drop_column("purchase_lots", "ebay_purchase_item_id")
    op.drop_column("purchase_lots", "ebay_purchase")

    op.drop_index("ix_products_ebay_purchase", table_name="products")
    op.drop_column("products", "ebay_purchase_url")
    op.drop_column("products", "ebay_purchase_item_id")
    op.drop_column("products", "ebay_purchase")
    op.drop_column("products", "product_cost")
