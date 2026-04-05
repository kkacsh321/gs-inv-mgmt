"""add sync run/event/error tables

Revision ID: 0013_sync_runs_events_errors
Revises: 0012_app_user_passwords
Create Date: 2026-03-23 15:10:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013_sync_runs_events_errors"
down_revision: Union[str, None] = "0012_app_user_passwords"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("job_name", sa.String(length=128), nullable=False),
        sa.Column("direction", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("records_processed", sa.Integer(), nullable=False),
        sa.Column("records_created", sa.Integer(), nullable=False),
        sa.Column("records_updated", sa.Integer(), nullable=False),
        sa.Column("records_failed", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sync_runs_provider", "sync_runs", ["provider"], unique=False)
    op.create_index("ix_sync_runs_job_name", "sync_runs", ["job_name"], unique=False)
    op.create_index("ix_sync_runs_direction", "sync_runs", ["direction"], unique=False)
    op.create_index("ix_sync_runs_status", "sync_runs", ["status"], unique=False)
    op.create_index("ix_sync_runs_started_at", "sync_runs", ["started_at"], unique=False)

    op.create_table(
        "sync_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sync_run_id", sa.Integer(), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["sync_run_id"], ["sync_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sync_events_sync_run_id", "sync_events", ["sync_run_id"], unique=False)
    op.create_index("ix_sync_events_entity_type", "sync_events", ["entity_type"], unique=False)
    op.create_index("ix_sync_events_entity_id", "sync_events", ["entity_id"], unique=False)
    op.create_index("ix_sync_events_status", "sync_events", ["status"], unique=False)
    op.create_index("ix_sync_events_created_at", "sync_events", ["created_at"], unique=False)

    op.create_table(
        "sync_errors",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sync_run_id", sa.Integer(), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("context_json", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["sync_run_id"], ["sync_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sync_errors_sync_run_id", "sync_errors", ["sync_run_id"], unique=False)
    op.create_index("ix_sync_errors_severity", "sync_errors", ["severity"], unique=False)
    op.create_index("ix_sync_errors_code", "sync_errors", ["code"], unique=False)
    op.create_index("ix_sync_errors_occurred_at", "sync_errors", ["occurred_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_sync_errors_occurred_at", table_name="sync_errors")
    op.drop_index("ix_sync_errors_code", table_name="sync_errors")
    op.drop_index("ix_sync_errors_severity", table_name="sync_errors")
    op.drop_index("ix_sync_errors_sync_run_id", table_name="sync_errors")
    op.drop_table("sync_errors")

    op.drop_index("ix_sync_events_created_at", table_name="sync_events")
    op.drop_index("ix_sync_events_status", table_name="sync_events")
    op.drop_index("ix_sync_events_entity_id", table_name="sync_events")
    op.drop_index("ix_sync_events_entity_type", table_name="sync_events")
    op.drop_index("ix_sync_events_sync_run_id", table_name="sync_events")
    op.drop_table("sync_events")

    op.drop_index("ix_sync_runs_started_at", table_name="sync_runs")
    op.drop_index("ix_sync_runs_status", table_name="sync_runs")
    op.drop_index("ix_sync_runs_direction", table_name="sync_runs")
    op.drop_index("ix_sync_runs_job_name", table_name="sync_runs")
    op.drop_index("ix_sync_runs_provider", table_name="sync_runs")
    op.drop_table("sync_runs")
