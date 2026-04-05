"""add ebay publish presets table

Revision ID: 0017_ebay_publish_presets
Revises: 0016_listing_partial_unique
Create Date: 2026-03-24 10:35:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0017_ebay_publish_presets"
down_revision: Union[str, None] = "0016_listing_partial_unique"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ebay_publish_presets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("environment", sa.String(length=32), nullable=False, server_default="local"),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("marketplace_id", sa.String(length=32), nullable=False, server_default="EBAY_US"),
        sa.Column("currency", sa.String(length=8), nullable=False, server_default="USD"),
        sa.Column("content_language", sa.String(length=16), nullable=False, server_default="en-US"),
        sa.Column("merchant_location_key", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("payment_policy_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("fulfillment_policy_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("return_policy_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("category_id", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("format_type", sa.String(length=16), nullable=False, server_default="FIXED_PRICE"),
        sa.Column("listing_duration", sa.String(length=16), nullable=False, server_default="GTC"),
        sa.Column("condition_value", sa.String(length=32), nullable=False, server_default="NEW"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "environment",
            "username",
            "name",
            name="uq_ebay_publish_preset_env_user_name",
        ),
    )
    op.create_index(
        "ix_ebay_publish_presets_environment",
        "ebay_publish_presets",
        ["environment"],
        unique=False,
    )
    op.create_index(
        "ix_ebay_publish_presets_username",
        "ebay_publish_presets",
        ["username"],
        unique=False,
    )
    op.create_index(
        "ix_ebay_publish_presets_name",
        "ebay_publish_presets",
        ["name"],
        unique=False,
    )
    op.create_index(
        "ix_ebay_publish_presets_is_default",
        "ebay_publish_presets",
        ["is_default"],
        unique=False,
    )
    op.create_index(
        "ix_ebay_publish_presets_is_active",
        "ebay_publish_presets",
        ["is_active"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ebay_publish_presets_is_active", table_name="ebay_publish_presets")
    op.drop_index("ix_ebay_publish_presets_is_default", table_name="ebay_publish_presets")
    op.drop_index("ix_ebay_publish_presets_name", table_name="ebay_publish_presets")
    op.drop_index("ix_ebay_publish_presets_username", table_name="ebay_publish_presets")
    op.drop_index("ix_ebay_publish_presets_environment", table_name="ebay_publish_presets")
    op.drop_table("ebay_publish_presets")

