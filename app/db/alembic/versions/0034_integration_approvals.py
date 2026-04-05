"""add integration automation approvals

Revision ID: 0034_integration_approvals
Revises: 0033_integration_automation
Create Date: 2026-03-29 17:55:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0034_integration_approvals"
down_revision = "0033_integration_automation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "integration_automation_approvals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("environment", sa.String(length=32), nullable=False, server_default="local"),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("queue_job_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="approved"),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("approved_by", sa.String(length=128), nullable=False, server_default="system"),
        sa.Column("approved_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["queue_job_id"], ["integration_queue_jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["rule_id"], ["integration_automation_rules.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_integration_automation_approvals_environment",
        "integration_automation_approvals",
        ["environment"],
        unique=False,
    )
    op.create_index(
        "ix_integration_automation_approvals_rule_id",
        "integration_automation_approvals",
        ["rule_id"],
        unique=False,
    )
    op.create_index(
        "ix_integration_automation_approvals_queue_job_id",
        "integration_automation_approvals",
        ["queue_job_id"],
        unique=False,
    )
    op.create_index(
        "ix_integration_automation_approvals_status",
        "integration_automation_approvals",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_integration_automation_approvals_approved_by",
        "integration_automation_approvals",
        ["approved_by"],
        unique=False,
    )
    op.create_index(
        "ix_integration_automation_approvals_approved_at",
        "integration_automation_approvals",
        ["approved_at"],
        unique=False,
    )
    op.create_index(
        "ix_integration_automation_approvals_expires_at",
        "integration_automation_approvals",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_integration_automation_approvals_is_active",
        "integration_automation_approvals",
        ["is_active"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_integration_automation_approvals_is_active", table_name="integration_automation_approvals")
    op.drop_index("ix_integration_automation_approvals_expires_at", table_name="integration_automation_approvals")
    op.drop_index("ix_integration_automation_approvals_approved_at", table_name="integration_automation_approvals")
    op.drop_index("ix_integration_automation_approvals_approved_by", table_name="integration_automation_approvals")
    op.drop_index("ix_integration_automation_approvals_status", table_name="integration_automation_approvals")
    op.drop_index("ix_integration_automation_approvals_queue_job_id", table_name="integration_automation_approvals")
    op.drop_index("ix_integration_automation_approvals_rule_id", table_name="integration_automation_approvals")
    op.drop_index("ix_integration_automation_approvals_environment", table_name="integration_automation_approvals")
    op.drop_table("integration_automation_approvals")
