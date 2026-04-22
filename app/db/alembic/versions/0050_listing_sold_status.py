"""add sold listing status

Revision ID: 0050_listing_sold_status
Revises: 0049_media_archive_flag
Create Date: 2026-04-14
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "0050_listing_sold_status"
down_revision = "0049_media_archive_flag"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE listing_status_enum ADD VALUE IF NOT EXISTS 'sold'")


def downgrade() -> None:
    # PostgreSQL enum values are not removed safely in downgrade.
    pass
