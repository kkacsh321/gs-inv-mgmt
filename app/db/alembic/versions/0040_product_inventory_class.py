"""add inventory class to products

Revision ID: 0040_product_inventory_class
Revises: 0039_acquisition_shipping_handling
Create Date: 2026-04-01
"""

from alembic import op
import sqlalchemy as sa


revision = "0040_product_inventory_class"
down_revision = "0039_acq_ship_handle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("inventory_class", sa.String(length=32), nullable=False, server_default="sellable"),
    )
    op.create_index("ix_products_inventory_class", "products", ["inventory_class"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_products_inventory_class", table_name="products")
    op.drop_column("products", "inventory_class")
