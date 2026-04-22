"""add order-level shipping label spend fields

Revision ID: 0047_order_shipping_label_costs
Revises: 0046_notification_outbox
Create Date: 2026-04-13
"""

from alembic import op
import sqlalchemy as sa


revision = "0047_order_shipping_label_costs"
down_revision = "0046_notification_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("orders") as batch_op:
        batch_op.add_column(sa.Column("shipping_label_cost", sa.Numeric(12, 2), nullable=True))
        batch_op.add_column(
            sa.Column("shipping_label_currency", sa.String(length=8), nullable=False, server_default="USD")
        )

    op.execute(
        sa.text(
            """
            UPDATE orders o
            SET shipping_label_cost = src.total_label_cost
            FROM (
                SELECT order_id, SUM(COALESCE(shipping_label_cost, 0)) AS total_label_cost
                FROM sales
                WHERE order_id IS NOT NULL
                GROUP BY order_id
            ) AS src
            WHERE o.id = src.order_id
            """
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("orders") as batch_op:
        batch_op.drop_column("shipping_label_currency")
        batch_op.drop_column("shipping_label_cost")
