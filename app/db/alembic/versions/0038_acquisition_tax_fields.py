"""add acquisition tax fields for products and lots

Revision ID: 0038_acquisition_tax
Revises: 0037_source_ebay_store
Create Date: 2026-04-01
"""

from alembic import op
import sqlalchemy as sa


revision = "0038_acquisition_tax"
down_revision = "0037_source_ebay_store"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("products", sa.Column("acquisition_tax_paid", sa.Numeric(12, 2), nullable=True))
    op.add_column("purchase_lots", sa.Column("total_tax_paid", sa.Numeric(12, 2), nullable=True))
    op.add_column("product_lot_assignments", sa.Column("unit_tax_paid", sa.Numeric(12, 2), nullable=True))
    op.add_column("product_lot_assignments", sa.Column("allocated_tax_paid", sa.Numeric(12, 2), nullable=True))


def downgrade() -> None:
    op.drop_column("product_lot_assignments", "allocated_tax_paid")
    op.drop_column("product_lot_assignments", "unit_tax_paid")
    op.drop_column("purchase_lots", "total_tax_paid")
    op.drop_column("products", "acquisition_tax_paid")

