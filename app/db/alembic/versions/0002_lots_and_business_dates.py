"""add lots and business dates

Revision ID: 0002_lots_and_business_dates
Revises: 0001_initial_schema
Create Date: 2026-03-23
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0002_lots_and_business_dates"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("acquired_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.add_column(
        "marketplace_listings",
        sa.Column("listed_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.add_column(
        "sales",
        sa.Column("sold_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )

    op.create_table(
        "purchase_lots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("lot_code", sa.String(length=64), nullable=False),
        sa.Column("vendor", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("purchase_date", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("total_cost", sa.Numeric(12, 2), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_purchase_lots_lot_code", "purchase_lots", ["lot_code"], unique=True)

    op.create_table(
        "product_lot_assignments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("lot_id", sa.Integer(), sa.ForeignKey("purchase_lots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("quantity_acquired", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("unit_cost", sa.Numeric(12, 2), nullable=True),
        sa.Column("allocated_cost", sa.Numeric(12, 2), nullable=True),
        sa.Column("acquired_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("product_id", "lot_id", name="uq_product_lot_assignment"),
    )


def downgrade() -> None:
    op.drop_table("product_lot_assignments")
    op.drop_index("ix_purchase_lots_lot_code", table_name="purchase_lots")
    op.drop_table("purchase_lots")

    op.drop_column("sales", "sold_at")
    op.drop_column("marketplace_listings", "listed_at")
    op.drop_column("products", "acquired_at")
