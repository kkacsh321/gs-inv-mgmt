"""add retry linkage fields to sync runs

Revision ID: 0014_sync_run_retry_linkage
Revises: 0013_sync_runs_events_errors
Create Date: 2026-03-23 15:25:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014_sync_run_retry_linkage"
down_revision: Union[str, None] = "0013_sync_runs_events_errors"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sync_runs", sa.Column("retry_of_run_id", sa.Integer(), nullable=True))
    op.add_column("sync_runs", sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"))
    op.create_index("ix_sync_runs_retry_of_run_id", "sync_runs", ["retry_of_run_id"], unique=False)
    op.create_index("ix_sync_runs_retry_count", "sync_runs", ["retry_count"], unique=False)
    op.create_foreign_key(
        "fk_sync_runs_retry_of_run_id",
        "sync_runs",
        "sync_runs",
        ["retry_of_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.alter_column("sync_runs", "retry_count", server_default=None)


def downgrade() -> None:
    op.drop_constraint("fk_sync_runs_retry_of_run_id", "sync_runs", type_="foreignkey")
    op.drop_index("ix_sync_runs_retry_count", table_name="sync_runs")
    op.drop_index("ix_sync_runs_retry_of_run_id", table_name="sync_runs")
    op.drop_column("sync_runs", "retry_count")
    op.drop_column("sync_runs", "retry_of_run_id")
