"""add account id and payment method to inventory sources

Revision ID: 0020_source_account_payment
Revises: 0019_saved_filter_shared_default
Create Date: 2026-03-24 19:10:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0020_source_account_payment"
down_revision: Union[str, None] = "0019_saved_filter_shared_default"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "inventory_sources",
        sa.Column("account_id", sa.String(length=128), nullable=False, server_default=""),
    )
    op.add_column(
        "inventory_sources",
        sa.Column("payment_method", sa.String(length=64), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("inventory_sources", "payment_method")
    op.drop_column("inventory_sources", "account_id")
