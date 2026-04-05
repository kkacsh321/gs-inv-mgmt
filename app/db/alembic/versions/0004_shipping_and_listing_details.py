"""add shipping tracking and listing detail fields

Revision ID: 0004_shipping_listing
Revises: 0003_add_audit_logs
Create Date: 2026-03-23
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0004_shipping_listing"
down_revision: Union[str, None] = "0003_add_audit_logs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("products", sa.Column("package_weight_oz", sa.Numeric(10, 4), nullable=True))
    op.add_column("products", sa.Column("package_length_in", sa.Numeric(10, 2), nullable=True))
    op.add_column("products", sa.Column("package_width_in", sa.Numeric(10, 2), nullable=True))
    op.add_column("products", sa.Column("package_height_in", sa.Numeric(10, 2), nullable=True))

    op.add_column(
        "marketplace_listings",
        sa.Column("marketplace_url", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "marketplace_listings",
        sa.Column("marketplace_details", sa.Text(), nullable=False, server_default=""),
    )

    op.add_column("sales", sa.Column("shipping_provider", sa.String(length=64), nullable=False, server_default=""))
    op.add_column("sales", sa.Column("shipping_service", sa.String(length=128), nullable=False, server_default=""))
    op.add_column("sales", sa.Column("tracking_number", sa.String(length=128), nullable=False, server_default=""))
    op.add_column("sales", sa.Column("tracking_status", sa.String(length=64), nullable=False, server_default=""))
    op.add_column("sales", sa.Column("shipped_at", sa.DateTime(), nullable=True))
    op.add_column("sales", sa.Column("delivered_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("sales", "delivered_at")
    op.drop_column("sales", "shipped_at")
    op.drop_column("sales", "tracking_status")
    op.drop_column("sales", "tracking_number")
    op.drop_column("sales", "shipping_service")
    op.drop_column("sales", "shipping_provider")

    op.drop_column("marketplace_listings", "marketplace_details")
    op.drop_column("marketplace_listings", "marketplace_url")

    op.drop_column("products", "package_height_in")
    op.drop_column("products", "package_width_in")
    op.drop_column("products", "package_length_in")
    op.drop_column("products", "package_weight_oz")
