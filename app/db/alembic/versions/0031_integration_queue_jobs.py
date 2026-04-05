"""add integration queue jobs table

Revision ID: 0031_integration_queue
Revises: 0030_product_coin_ref
Create Date: 2026-03-27
"""

from alembic import op
import sqlalchemy as sa


revision = "0031_integration_queue"
down_revision = "0030_product_coin_ref"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "integration_queue_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("environment", sa.String(length=32), nullable=False),
        sa.Column("integration", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("max_retries", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("updated_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_integration_queue_jobs_environment", "integration_queue_jobs", ["environment"], unique=False)
    op.create_index("ix_integration_queue_jobs_integration", "integration_queue_jobs", ["integration"], unique=False)
    op.create_index("ix_integration_queue_jobs_action", "integration_queue_jobs", ["action"], unique=False)
    op.create_index("ix_integration_queue_jobs_status", "integration_queue_jobs", ["status"], unique=False)
    op.create_index("ix_integration_queue_jobs_next_attempt_at", "integration_queue_jobs", ["next_attempt_at"], unique=False)
    op.create_index("ix_integration_queue_jobs_requested_by", "integration_queue_jobs", ["requested_by"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_integration_queue_jobs_requested_by", table_name="integration_queue_jobs")
    op.drop_index("ix_integration_queue_jobs_next_attempt_at", table_name="integration_queue_jobs")
    op.drop_index("ix_integration_queue_jobs_status", table_name="integration_queue_jobs")
    op.drop_index("ix_integration_queue_jobs_action", table_name="integration_queue_jobs")
    op.drop_index("ix_integration_queue_jobs_integration", table_name="integration_queue_jobs")
    op.drop_index("ix_integration_queue_jobs_environment", table_name="integration_queue_jobs")
    op.drop_table("integration_queue_jobs")
