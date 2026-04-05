"""add returns workflow domain

Revision ID: 0008_returns_workflow
Revises: 0007_orders_domain
Create Date: 2026-03-23
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0008_returns_workflow"
down_revision: Union[str, None] = "0007_orders_domain"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "returns",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sale_id", sa.Integer(), nullable=True),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("marketplace", sa.String(length=64), nullable=False),
        sa.Column("external_return_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("return_status", sa.String(length=32), nullable=False, server_default="requested"),
        sa.Column("reason", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("disposition", sa.String(length=64), nullable=False, server_default="pending"),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("refund_amount", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("refund_fees", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("refund_shipping", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("restocked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("returned_at", sa.DateTime(), nullable=False),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["sale_id"], ["sales.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_returns_sale_id", "returns", ["sale_id"], unique=False)
    op.create_index("ix_returns_order_id", "returns", ["order_id"], unique=False)
    op.create_index("ix_returns_product_id", "returns", ["product_id"], unique=False)
    op.create_index("ix_returns_marketplace", "returns", ["marketplace"], unique=False)
    op.create_index("ix_returns_restocked", "returns", ["restocked"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_returns_restocked", table_name="returns")
    op.drop_index("ix_returns_marketplace", table_name="returns")
    op.drop_index("ix_returns_product_id", table_name="returns")
    op.drop_index("ix_returns_order_id", table_name="returns")
    op.drop_index("ix_returns_sale_id", table_name="returns")
    op.drop_table("returns")
