"""add notification outbox due and dedupe composite indexes

Revision ID: 0052_outbox_claim_dedupe_idx
Revises: 0051_order_finance_entries
Create Date: 2026-04-20
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "0052_outbox_claim_dedupe_idx"
down_revision = "0051_order_finance_entries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_notification_outbox_env_status_due_id",
        "notification_outbox",
        ["environment", "status", "next_attempt_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_notification_outbox_env_channel_dedupe_status",
        "notification_outbox",
        ["environment", "channel", "dedupe_key", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_notification_outbox_env_channel_dedupe_status", table_name="notification_outbox")
    op.drop_index("ix_notification_outbox_env_status_due_id", table_name="notification_outbox")
