"""add notification outbox and performance indexes

Revision ID: 0046_notification_outbox
Revises: 0045_order_ship_tracking
Create Date: 2026-04-12
"""

from alembic import op
import sqlalchemy as sa


revision = "0046_notification_outbox"
down_revision = "0045_order_ship_tracking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("environment", sa.String(length=32), nullable=False, server_default="local"),
        sa.Column("channel", sa.String(length=32), nullable=False, server_default="slack"),
        sa.Column("event_type", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("entity_type", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("entity_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("dispatched_at", sa.DateTime(), nullable=True),
        sa.Column("locked_by", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("locked_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("requested_by", sa.String(length=128), nullable=False, server_default="system"),
        sa.Column("updated_by", sa.String(length=128), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_notification_outbox_environment",
        "notification_outbox",
        ["environment"],
        unique=False,
    )
    op.create_index(
        "ix_notification_outbox_channel",
        "notification_outbox",
        ["channel"],
        unique=False,
    )
    op.create_index(
        "ix_notification_outbox_event_type",
        "notification_outbox",
        ["event_type"],
        unique=False,
    )
    op.create_index(
        "ix_notification_outbox_entity_type",
        "notification_outbox",
        ["entity_type"],
        unique=False,
    )
    op.create_index(
        "ix_notification_outbox_entity_id",
        "notification_outbox",
        ["entity_id"],
        unique=False,
    )
    op.create_index(
        "ix_notification_outbox_dedupe_key",
        "notification_outbox",
        ["dedupe_key"],
        unique=False,
    )
    op.create_index(
        "ix_notification_outbox_status",
        "notification_outbox",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_notification_outbox_next_attempt_at",
        "notification_outbox",
        ["next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "ix_notification_outbox_dispatched_at",
        "notification_outbox",
        ["dispatched_at"],
        unique=False,
    )
    op.create_index(
        "ix_notification_outbox_locked_by",
        "notification_outbox",
        ["locked_by"],
        unique=False,
    )
    op.create_index(
        "ix_notification_outbox_locked_at",
        "notification_outbox",
        ["locked_at"],
        unique=False,
    )
    op.create_index(
        "ix_notification_outbox_requested_by",
        "notification_outbox",
        ["requested_by"],
        unique=False,
    )
    op.create_index(
        "ix_notification_outbox_dispatch",
        "notification_outbox",
        ["status", "next_attempt_at", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_notification_outbox_lock",
        "notification_outbox",
        ["status", "locked_at"],
        unique=False,
    )

    op.create_index(
        "ix_sync_runs_job_status_started",
        "sync_runs",
        ["job_name", "status", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_marketplace_listings_marketplace_status_updated",
        "marketplace_listings",
        ["marketplace", "listing_status", "updated_at"],
        unique=False,
    )
    op.create_index(
        "ix_workflow_drafts_recent",
        "workflow_drafts",
        ["environment", "workflow_key", "status", "updated_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_logs_entity_created",
        "audit_logs",
        ["entity_type", "entity_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_audit_logs_entity_created", table_name="audit_logs")
    op.drop_index(
        "ix_workflow_drafts_recent",
        table_name="workflow_drafts",
    )
    op.drop_index(
        "ix_marketplace_listings_marketplace_status_updated",
        table_name="marketplace_listings",
    )
    op.drop_index(
        "ix_sync_runs_job_status_started",
        table_name="sync_runs",
    )

    op.drop_index("ix_notification_outbox_lock", table_name="notification_outbox")
    op.drop_index("ix_notification_outbox_dispatch", table_name="notification_outbox")
    op.drop_index("ix_notification_outbox_requested_by", table_name="notification_outbox")
    op.drop_index("ix_notification_outbox_locked_at", table_name="notification_outbox")
    op.drop_index("ix_notification_outbox_locked_by", table_name="notification_outbox")
    op.drop_index("ix_notification_outbox_dispatched_at", table_name="notification_outbox")
    op.drop_index("ix_notification_outbox_next_attempt_at", table_name="notification_outbox")
    op.drop_index("ix_notification_outbox_status", table_name="notification_outbox")
    op.drop_index("ix_notification_outbox_dedupe_key", table_name="notification_outbox")
    op.drop_index("ix_notification_outbox_entity_id", table_name="notification_outbox")
    op.drop_index("ix_notification_outbox_entity_type", table_name="notification_outbox")
    op.drop_index("ix_notification_outbox_event_type", table_name="notification_outbox")
    op.drop_index("ix_notification_outbox_channel", table_name="notification_outbox")
    op.drop_index("ix_notification_outbox_environment", table_name="notification_outbox")
    op.drop_table("notification_outbox")
