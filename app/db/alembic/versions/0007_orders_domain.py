"""add orders and order items domain

Revision ID: 0007_orders_domain
Revises: 0006_inventory_sources
Create Date: 2026-03-23
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0007_orders_domain"
down_revision: Union[str, None] = "0006_inventory_sources"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("marketplace", sa.String(length=64), nullable=False),
        sa.Column("external_order_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("order_status", sa.String(length=32), nullable=False, server_default="paid"),
        sa.Column("subtotal_amount", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("fees", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("shipping_cost", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("total_amount", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("sold_at", sa.DateTime(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("marketplace", "external_order_id", name="uq_marketplace_order"),
    )
    op.create_index("ix_orders_marketplace", "orders", ["marketplace"], unique=False)

    op.create_table(
        "order_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("listing_id", sa.Integer(), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("line_fees", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("line_shipping", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("line_total", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["listing_id"], ["marketplace_listings.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_order_items_order_id", "order_items", ["order_id"], unique=False)

    op.add_column("sales", sa.Column("order_id", sa.Integer(), nullable=True))
    op.create_index("ix_sales_order_id", "sales", ["order_id"], unique=False)
    op.create_foreign_key(
        "fk_sales_order_id_orders",
        "sales",
        "orders",
        ["order_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_sales_order_id_orders", "sales", type_="foreignkey")
    op.drop_index("ix_sales_order_id", table_name="sales")
    op.drop_column("sales", "order_id")

    op.drop_index("ix_order_items_order_id", table_name="order_items")
    op.drop_table("order_items")

    op.drop_index("ix_orders_marketplace", table_name="orders")
    op.drop_table("orders")
