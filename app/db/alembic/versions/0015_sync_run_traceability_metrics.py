"""add sync run traceability metric columns

Revision ID: 0015_sync_trace_metrics
Revises: 0014_sync_run_retry_linkage
Create Date: 2026-03-23 15:40:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015_sync_trace_metrics"
down_revision: Union[str, None] = "0014_sync_run_retry_linkage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sync_runs",
        sa.Column("line_items_with_listing_link", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "sync_runs",
        sa.Column("line_items_unmapped_sku", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "sync_runs",
        sa.Column("auto_listings_created", sa.Integer(), nullable=False, server_default="0"),
    )
    op.alter_column("sync_runs", "line_items_with_listing_link", server_default=None)
    op.alter_column("sync_runs", "line_items_unmapped_sku", server_default=None)
    op.alter_column("sync_runs", "auto_listings_created", server_default=None)


def downgrade() -> None:
    op.drop_column("sync_runs", "auto_listings_created")
    op.drop_column("sync_runs", "line_items_unmapped_sku")
    op.drop_column("sync_runs", "line_items_with_listing_link")
