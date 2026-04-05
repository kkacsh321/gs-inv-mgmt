"""add ai fields to products

Revision ID: 0026_product_ai_fields
Revises: 0025_ai_provider_mm_model
Create Date: 2026-03-25 08:35:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0026_product_ai_fields"
down_revision: Union[str, None] = "0025_ai_provider_mm_model"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("ai_graded", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "products",
        sa.Column("ai_grading_description", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "products",
        sa.Column("ai_description", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_products_ai_graded", "products", ["ai_graded"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_products_ai_graded", table_name="products")
    op.drop_column("products", "ai_description")
    op.drop_column("products", "ai_grading_description")
    op.drop_column("products", "ai_graded")
