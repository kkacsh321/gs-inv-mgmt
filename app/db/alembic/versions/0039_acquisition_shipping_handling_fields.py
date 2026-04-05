"""add acquisition shipping/handling fields for products and lots

Revision ID: 0039_acq_ship_handle
Revises: 0038_acquisition_tax
Create Date: 2026-04-01
"""

from alembic import op
import sqlalchemy as sa


revision = "0039_acq_ship_handle"
down_revision = "0038_acquisition_tax"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("products", sa.Column("acquisition_shipping_paid", sa.Numeric(12, 2), nullable=True))
    op.add_column("products", sa.Column("acquisition_handling_paid", sa.Numeric(12, 2), nullable=True))
    op.add_column("purchase_lots", sa.Column("total_shipping_paid", sa.Numeric(12, 2), nullable=True))
    op.add_column("purchase_lots", sa.Column("total_handling_paid", sa.Numeric(12, 2), nullable=True))
    op.add_column("product_lot_assignments", sa.Column("unit_shipping_paid", sa.Numeric(12, 2), nullable=True))
    op.add_column("product_lot_assignments", sa.Column("unit_handling_paid", sa.Numeric(12, 2), nullable=True))
    op.add_column("product_lot_assignments", sa.Column("allocated_shipping_paid", sa.Numeric(12, 2), nullable=True))
    op.add_column("product_lot_assignments", sa.Column("allocated_handling_paid", sa.Numeric(12, 2), nullable=True))


def downgrade() -> None:
    op.drop_column("product_lot_assignments", "allocated_handling_paid")
    op.drop_column("product_lot_assignments", "allocated_shipping_paid")
    op.drop_column("product_lot_assignments", "unit_handling_paid")
    op.drop_column("product_lot_assignments", "unit_shipping_paid")
    op.drop_column("purchase_lots", "total_handling_paid")
    op.drop_column("purchase_lots", "total_shipping_paid")
    op.drop_column("products", "acquisition_handling_paid")
    op.drop_column("products", "acquisition_shipping_paid")

