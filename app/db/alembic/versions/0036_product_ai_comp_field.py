"""add product ai_comp field

Revision ID: 0036_product_ai_comp
Revises: 0035_product_lot_ebay
Create Date: 2026-04-01
"""

from alembic import op
import sqlalchemy as sa


revision = "0036_product_ai_comp"
down_revision = "0035_product_lot_ebay"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("products", sa.Column("ai_comp", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("products", "ai_comp")

