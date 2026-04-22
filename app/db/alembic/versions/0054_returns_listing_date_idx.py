"""add returns and listing date-window indexes

Revision ID: 0054_returns_listing_date_idx
Revises: 0053_report_hotpath_idx
Create Date: 2026-04-20
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "0054_returns_listing_date_idx"
down_revision = "0053_report_hotpath_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_returns_returned_at_id",
        "returns",
        ["returned_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_marketplace_listings_listed_at_id",
        "marketplace_listings",
        ["listed_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_marketplace_listings_created_at_id",
        "marketplace_listings",
        ["created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_marketplace_listings_created_at_id", table_name="marketplace_listings")
    op.drop_index("ix_marketplace_listings_listed_at_id", table_name="marketplace_listings")
    op.drop_index("ix_returns_returned_at_id", table_name="returns")
