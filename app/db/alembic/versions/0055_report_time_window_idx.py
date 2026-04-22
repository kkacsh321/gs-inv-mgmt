"""add report time-window indexes

Revision ID: 0055_report_time_window_idx
Revises: 0054_returns_listing_date_idx
Create Date: 2026-04-20
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "0055_report_time_window_idx"
down_revision = "0054_returns_listing_date_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_products_acquired_at_id",
        "products",
        ["acquired_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_product_lot_assignments_acq_id",
        "product_lot_assignments",
        ["acquired_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_inventory_movements_occ_at_id",
        "inventory_movements",
        ["occurred_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_inventory_movements_occ_at_id", table_name="inventory_movements")
    op.drop_index("ix_product_lot_assignments_acq_id", table_name="product_lot_assignments")
    op.drop_index("ix_products_acquired_at_id", table_name="products")
