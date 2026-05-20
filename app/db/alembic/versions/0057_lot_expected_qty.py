"""add purchase lot expected quantity

Revision ID: 0057_lot_expected_qty
Revises: 0056_ebay_category_aspects_cache
Create Date: 2026-04-26
"""

from alembic import op
import sqlalchemy as sa


revision = "0057_lot_expected_qty"
down_revision = "0056_ebay_category_aspects_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("purchase_lots", sa.Column("expected_total_quantity", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("purchase_lots", "expected_total_quantity")
