"""add shipping/tracking fields to orders

Revision ID: 0045_order_ship_tracking
Revises: 0044_workflow_state
Create Date: 2026-04-11
"""

from alembic import op
import sqlalchemy as sa


revision = "0045_order_ship_tracking"
down_revision = "0044_workflow_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("orders") as batch_op:
        batch_op.add_column(
            sa.Column("shipping_provider", sa.String(length=64), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("shipping_service", sa.String(length=128), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("tracking_number", sa.String(length=128), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("tracking_status", sa.String(length=64), nullable=False, server_default="")
        )
        batch_op.add_column(sa.Column("shipped_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("delivered_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("orders") as batch_op:
        batch_op.drop_column("delivered_at")
        batch_op.drop_column("shipped_at")
        batch_op.drop_column("tracking_status")
        batch_op.drop_column("tracking_number")
        batch_op.drop_column("shipping_service")
        batch_op.drop_column("shipping_provider")
