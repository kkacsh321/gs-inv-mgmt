"""add order buyer/ship-to fields and raw marketplace payload

Revision ID: 0048_order_party_payload
Revises: 0047_order_shipping_label_costs
Create Date: 2026-04-13
"""

from alembic import op
import sqlalchemy as sa


revision = "0048_order_party_payload"
down_revision = "0047_order_shipping_label_costs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("orders") as batch_op:
        batch_op.add_column(sa.Column("buyer_username", sa.String(length=128), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("buyer_name", sa.String(length=255), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("buyer_email", sa.String(length=255), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("ship_to_city", sa.String(length=128), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("ship_to_state", sa.String(length=64), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("ship_to_postal_code", sa.String(length=32), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("ship_to_country", sa.String(length=8), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("marketplace_payload_json", sa.Text(), nullable=False, server_default="{}"))

    op.create_index("ix_orders_buyer_username", "orders", ["buyer_username"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_orders_buyer_username", table_name="orders")
    with op.batch_alter_table("orders") as batch_op:
        batch_op.drop_column("marketplace_payload_json")
        batch_op.drop_column("ship_to_country")
        batch_op.drop_column("ship_to_postal_code")
        batch_op.drop_column("ship_to_state")
        batch_op.drop_column("ship_to_city")
        batch_op.drop_column("buyer_email")
        batch_op.drop_column("buyer_name")
        batch_op.drop_column("buyer_username")
