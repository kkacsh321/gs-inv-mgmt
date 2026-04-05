"""allow blank external listing IDs without uniqueness collisions

Revision ID: 0016_listing_partial_unique
Revises: 0015_sync_trace_metrics
Create Date: 2026-03-24 16:18:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0016_listing_partial_unique"
down_revision: Union[str, None] = "0015_sync_trace_metrics"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("uq_marketplace_listing", "marketplace_listings", type_="unique")
    op.create_index(
        "uq_marketplace_listing_nonblank",
        "marketplace_listings",
        ["marketplace", "external_listing_id"],
        unique=True,
        postgresql_where=sa.text("external_listing_id <> ''"),
    )


def downgrade() -> None:
    op.drop_index("uq_marketplace_listing_nonblank", table_name="marketplace_listings")
    op.create_unique_constraint(
        "uq_marketplace_listing",
        "marketplace_listings",
        ["marketplace", "external_listing_id"],
    )

