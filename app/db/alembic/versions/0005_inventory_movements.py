"""add inventory movements ledger

Revision ID: 0005_inventory_movements
Revises: 0004_shipping_listing
Create Date: 2026-03-23
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0005_inventory_movements"
down_revision: Union[str, None] = "0004_shipping_listing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "inventory_movements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("movement_type", sa.String(length=64), nullable=False),
        sa.Column("quantity_delta", sa.Integer(), nullable=False),
        sa.Column("quantity_before", sa.Integer(), nullable=False),
        sa.Column("quantity_after", sa.Integer(), nullable=False),
        sa.Column("unit_cost", sa.Numeric(12, 2), nullable=True),
        sa.Column("reference_type", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("reference_id", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_inventory_movements_product_id", "inventory_movements", ["product_id"], unique=False)
    op.create_index(
        "ix_inventory_movements_movement_type",
        "inventory_movements",
        ["movement_type"],
        unique=False,
    )
    op.create_index("ix_inventory_movements_reference_id", "inventory_movements", ["reference_id"], unique=False)
    op.create_index("ix_inventory_movements_occurred_at", "inventory_movements", ["occurred_at"], unique=False)
    op.create_index("ix_inventory_movements_created_at", "inventory_movements", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_inventory_movements_created_at", table_name="inventory_movements")
    op.drop_index("ix_inventory_movements_occurred_at", table_name="inventory_movements")
    op.drop_index("ix_inventory_movements_reference_id", table_name="inventory_movements")
    op.drop_index("ix_inventory_movements_movement_type", table_name="inventory_movements")
    op.drop_index("ix_inventory_movements_product_id", table_name="inventory_movements")
    op.drop_table("inventory_movements")
