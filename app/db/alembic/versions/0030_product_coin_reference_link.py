"""add product coin reference link

Revision ID: 0030_product_coin_ref
Revises: 0029_coin_reference_catalog
Create Date: 2026-03-26
"""

from alembic import op
import sqlalchemy as sa


revision = "0030_product_coin_ref"
down_revision = "0029_coin_reference_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("products", sa.Column("coin_reference_id", sa.Integer(), nullable=True))
    op.create_index("ix_products_coin_reference_id", "products", ["coin_reference_id"], unique=False)
    op.create_foreign_key(
        "fk_products_coin_reference_id",
        "products",
        "coin_reference_catalog",
        ["coin_reference_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_products_coin_reference_id", "products", type_="foreignkey")
    op.drop_index("ix_products_coin_reference_id", table_name="products")
    op.drop_column("products", "coin_reference_id")
