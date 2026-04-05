"""add optional source url to inventory sources

Revision ID: 0021_source_url
Revises: 0020_source_account_payment
Create Date: 2026-03-24 19:25:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0021_source_url"
down_revision: Union[str, None] = "0020_source_account_payment"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "inventory_sources",
        sa.Column("source_url", sa.String(length=512), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("inventory_sources", "source_url")
