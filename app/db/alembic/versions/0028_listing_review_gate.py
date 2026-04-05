"""add listing review gate fields

Revision ID: 0028_listing_review
Revises: 0027_ebay_templates
Create Date: 2026-03-25
"""

from alembic import op
import sqlalchemy as sa


revision = "0028_listing_review"
down_revision = "0027_ebay_templates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("marketplace_listings", sa.Column("review_status", sa.String(length=32), nullable=True))
    op.add_column("marketplace_listings", sa.Column("reviewed_at", sa.DateTime(), nullable=True))
    op.add_column("marketplace_listings", sa.Column("reviewed_by", sa.String(length=128), nullable=True))
    op.create_index("ix_marketplace_listings_review_status", "marketplace_listings", ["review_status"], unique=False)

    op.execute(
        """
        UPDATE marketplace_listings
        SET
            review_status = CASE
                WHEN listing_status = 'active' THEN 'approved'
                ELSE 'pending'
            END,
            reviewed_by = CASE
                WHEN listing_status = 'active' THEN COALESCE(NULLIF(reviewed_by, ''), 'system')
                ELSE COALESCE(reviewed_by, '')
            END
        """
    )
    op.alter_column("marketplace_listings", "review_status", nullable=False, server_default="pending")
    op.alter_column("marketplace_listings", "reviewed_by", nullable=False, server_default="")


def downgrade() -> None:
    op.drop_index("ix_marketplace_listings_review_status", table_name="marketplace_listings")
    op.drop_column("marketplace_listings", "reviewed_by")
    op.drop_column("marketplace_listings", "reviewed_at")
    op.drop_column("marketplace_listings", "review_status")
