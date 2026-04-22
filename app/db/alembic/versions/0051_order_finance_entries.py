"""add normalized order finance entries

Revision ID: 0051_order_finance_entries
Revises: 0050_listing_sold_status
Create Date: 2026-04-15
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0051_order_finance_entries"
down_revision = "0050_listing_sold_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "order_finance_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("marketplace", sa.String(length=64), nullable=False, server_default="ebay"),
        sa.Column("external_order_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("transaction_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("line_item_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("legacy_item_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("sku", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("entry_kind", sa.String(length=32), nullable=False, server_default="other"),
        sa.Column("fee_type", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(length=8), nullable=False, server_default="USD"),
        sa.Column("booking_entry", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("transaction_type", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("transaction_status", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("transaction_date", sa.DateTime(), nullable=True),
        sa.Column("memo", sa.Text(), nullable=False, server_default=""),
        sa.Column("source", sa.String(length=64), nullable=False, server_default="ebay_finances"),
        sa.Column("raw_json", sa.Text(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_order_finance_entries_order_id", "order_finance_entries", ["order_id"], unique=False)
    op.create_index(
        "ix_order_finance_entries_order_kind",
        "order_finance_entries",
        ["order_id", "entry_kind"],
        unique=False,
    )
    op.create_index(
        "ix_order_finance_entries_order_tx",
        "order_finance_entries",
        ["order_id", "transaction_id"],
        unique=False,
    )
    op.create_index(
        "ix_order_finance_entries_external_order",
        "order_finance_entries",
        ["external_order_id"],
        unique=False,
    )
    op.create_index(
        "ix_order_finance_entries_marketplace",
        "order_finance_entries",
        ["marketplace"],
        unique=False,
    )
    op.create_index(
        "ix_order_finance_entries_transaction_type",
        "order_finance_entries",
        ["transaction_type"],
        unique=False,
    )
    op.create_index(
        "ix_order_finance_entries_entry_kind",
        "order_finance_entries",
        ["entry_kind"],
        unique=False,
    )
    op.create_index(
        "ix_order_finance_entries_fee_type",
        "order_finance_entries",
        ["fee_type"],
        unique=False,
    )
    op.create_index(
        "ix_order_finance_entries_sku",
        "order_finance_entries",
        ["sku"],
        unique=False,
    )
    op.create_index(
        "ix_order_finance_entries_line_item_id",
        "order_finance_entries",
        ["line_item_id"],
        unique=False,
    )
    op.create_index(
        "ix_order_finance_entries_legacy_item_id",
        "order_finance_entries",
        ["legacy_item_id"],
        unique=False,
    )
    op.create_index(
        "ix_order_finance_entries_transaction_status",
        "order_finance_entries",
        ["transaction_status"],
        unique=False,
    )
    op.create_index(
        "ix_order_finance_entries_transaction_date",
        "order_finance_entries",
        ["transaction_date"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_order_finance_entries_transaction_date", table_name="order_finance_entries")
    op.drop_index("ix_order_finance_entries_transaction_status", table_name="order_finance_entries")
    op.drop_index("ix_order_finance_entries_legacy_item_id", table_name="order_finance_entries")
    op.drop_index("ix_order_finance_entries_line_item_id", table_name="order_finance_entries")
    op.drop_index("ix_order_finance_entries_sku", table_name="order_finance_entries")
    op.drop_index("ix_order_finance_entries_fee_type", table_name="order_finance_entries")
    op.drop_index("ix_order_finance_entries_entry_kind", table_name="order_finance_entries")
    op.drop_index("ix_order_finance_entries_transaction_type", table_name="order_finance_entries")
    op.drop_index("ix_order_finance_entries_marketplace", table_name="order_finance_entries")
    op.drop_index("ix_order_finance_entries_external_order", table_name="order_finance_entries")
    op.drop_index("ix_order_finance_entries_order_tx", table_name="order_finance_entries")
    op.drop_index("ix_order_finance_entries_order_kind", table_name="order_finance_entries")
    op.drop_index("ix_order_finance_entries_order_id", table_name="order_finance_entries")
    op.drop_table("order_finance_entries")
