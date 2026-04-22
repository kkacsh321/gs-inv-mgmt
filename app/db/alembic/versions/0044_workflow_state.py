"""add workflow draft and workflow event tables

Revision ID: 0044_workflow_state
Revises: 0043_ebay_category_cache
Create Date: 2026-04-11
"""

from alembic import op
import sqlalchemy as sa


revision = "0044_workflow_state"
down_revision = "0043_ebay_category_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workflow_drafts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("environment", sa.String(length=32), nullable=False, server_default="local"),
        sa.Column("workflow_key", sa.String(length=64), nullable=False),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("scope_key", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("schema_version", sa.String(length=16), nullable=False, server_default="v1"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("draft_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("autosave_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_step", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("resumed_at", sa.DateTime(), nullable=True),
        sa.Column("cleared_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by", sa.String(length=128), nullable=False, server_default="system"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "environment",
            "workflow_key",
            "username",
            "scope_key",
            name="uq_workflow_draft_env_key_user_scope",
        ),
    )
    op.create_index("ix_workflow_drafts_environment", "workflow_drafts", ["environment"], unique=False)
    op.create_index("ix_workflow_drafts_workflow_key", "workflow_drafts", ["workflow_key"], unique=False)
    op.create_index("ix_workflow_drafts_username", "workflow_drafts", ["username"], unique=False)
    op.create_index("ix_workflow_drafts_scope_key", "workflow_drafts", ["scope_key"], unique=False)
    op.create_index("ix_workflow_drafts_status", "workflow_drafts", ["status"], unique=False)
    op.create_index("ix_workflow_drafts_expires_at", "workflow_drafts", ["expires_at"], unique=False)
    op.create_index("ix_workflow_drafts_is_active", "workflow_drafts", ["is_active"], unique=False)
    op.create_index(
        "ix_workflow_draft_lookup",
        "workflow_drafts",
        ["environment", "workflow_key", "username", "scope_key"],
        unique=False,
    )

    op.create_table(
        "workflow_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("draft_id", sa.Integer(), nullable=True),
        sa.Column("environment", sa.String(length=32), nullable=False, server_default="local"),
        sa.Column("workflow_key", sa.String(length=64), nullable=False),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("scope_key", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="ok"),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_by", sa.String(length=128), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["draft_id"], ["workflow_drafts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_workflow_events_draft_id", "workflow_events", ["draft_id"], unique=False)
    op.create_index("ix_workflow_events_environment", "workflow_events", ["environment"], unique=False)
    op.create_index("ix_workflow_events_workflow_key", "workflow_events", ["workflow_key"], unique=False)
    op.create_index("ix_workflow_events_username", "workflow_events", ["username"], unique=False)
    op.create_index("ix_workflow_events_scope_key", "workflow_events", ["scope_key"], unique=False)
    op.create_index("ix_workflow_events_action", "workflow_events", ["action"], unique=False)
    op.create_index("ix_workflow_events_status", "workflow_events", ["status"], unique=False)
    op.create_index("ix_workflow_events_created_at", "workflow_events", ["created_at"], unique=False)
    op.create_index(
        "ix_workflow_event_lookup",
        "workflow_events",
        ["environment", "workflow_key", "username", "scope_key", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_workflow_event_lookup", table_name="workflow_events")
    op.drop_index("ix_workflow_events_created_at", table_name="workflow_events")
    op.drop_index("ix_workflow_events_status", table_name="workflow_events")
    op.drop_index("ix_workflow_events_action", table_name="workflow_events")
    op.drop_index("ix_workflow_events_scope_key", table_name="workflow_events")
    op.drop_index("ix_workflow_events_username", table_name="workflow_events")
    op.drop_index("ix_workflow_events_workflow_key", table_name="workflow_events")
    op.drop_index("ix_workflow_events_environment", table_name="workflow_events")
    op.drop_index("ix_workflow_events_draft_id", table_name="workflow_events")
    op.drop_table("workflow_events")

    op.drop_index("ix_workflow_draft_lookup", table_name="workflow_drafts")
    op.drop_index("ix_workflow_drafts_is_active", table_name="workflow_drafts")
    op.drop_index("ix_workflow_drafts_expires_at", table_name="workflow_drafts")
    op.drop_index("ix_workflow_drafts_status", table_name="workflow_drafts")
    op.drop_index("ix_workflow_drafts_scope_key", table_name="workflow_drafts")
    op.drop_index("ix_workflow_drafts_username", table_name="workflow_drafts")
    op.drop_index("ix_workflow_drafts_workflow_key", table_name="workflow_drafts")
    op.drop_index("ix_workflow_drafts_environment", table_name="workflow_drafts")
    op.drop_table("workflow_drafts")
