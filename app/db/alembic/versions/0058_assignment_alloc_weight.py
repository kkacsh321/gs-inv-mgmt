"""add product lot assignment allocation weight

Revision ID: 0058_assignment_alloc_weight
Revises: 0057_lot_expected_qty
Create Date: 2026-04-26
"""

from alembic import op
import sqlalchemy as sa


revision = "0058_assignment_alloc_weight"
down_revision = "0057_lot_expected_qty"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("product_lot_assignments", sa.Column("allocation_weight", sa.Numeric(12, 4), nullable=True))


def downgrade() -> None:
    op.drop_column("product_lot_assignments", "allocation_weight")
