"""add shipping label artifact fields to sales

Revision ID: 0032_shipping_label_fields
Revises: 0031_integration_queue
Create Date: 2026-03-29
"""

from alembic import op
import sqlalchemy as sa


revision = "0032_shipping_label_fields"
down_revision = "0031_integration_queue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sales",
        sa.Column("shipping_label_id", sa.String(length=128), nullable=False, server_default=""),
    )
    op.add_column(
        "sales",
        sa.Column("shipping_label_url", sa.String(length=512), nullable=False, server_default=""),
    )
    op.add_column(
        "sales",
        sa.Column("shipping_label_cost", sa.Numeric(precision=12, scale=2), nullable=True),
    )
    op.add_column(
        "sales",
        sa.Column("shipping_label_currency", sa.String(length=8), nullable=False, server_default="USD"),
    )
    op.add_column(
        "sales",
        sa.Column("shipping_label_purchased_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_sales_shipping_label_id", "sales", ["shipping_label_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_sales_shipping_label_id", table_name="sales")
    op.drop_column("sales", "shipping_label_purchased_at")
    op.drop_column("sales", "shipping_label_currency")
    op.drop_column("sales", "shipping_label_cost")
    op.drop_column("sales", "shipping_label_url")
    op.drop_column("sales", "shipping_label_id")
