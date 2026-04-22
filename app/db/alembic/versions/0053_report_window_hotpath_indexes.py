"""add report window hot-path indexes

Revision ID: 0053_report_hotpath_idx
Revises: 0052_outbox_claim_dedupe_idx
Create Date: 2026-04-20
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "0053_report_hotpath_idx"
down_revision = "0052_outbox_claim_dedupe_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_sales_sold_at_id",
        "sales",
        ["sold_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_orders_sold_at_id",
        "orders",
        ["sold_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_order_finance_entries_kind_txdate_created",
        "order_finance_entries",
        ["entry_kind", "transaction_date", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_order_finance_entries_kind_txdate_created", table_name="order_finance_entries")
    op.drop_index("ix_orders_sold_at_id", table_name="orders")
    op.drop_index("ix_sales_sold_at_id", table_name="sales")
