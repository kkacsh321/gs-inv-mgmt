"""add ebay listing template profiles

Revision ID: 0027_ebay_templates
Revises: 0026_product_ai_fields
Create Date: 2026-03-25 09:10:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0027_ebay_templates"
down_revision: Union[str, None] = "0026_product_ai_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ebay_listing_template_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("environment", sa.String(length=32), nullable=False, server_default="local"),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("marketplace", sa.String(length=32), nullable=False, server_default="ebay"),
        sa.Column("listing_title_template", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("marketplace_details_template", sa.Text(), nullable=False, server_default=""),
        sa.Column("listing_price_default", sa.Numeric(12, 2), nullable=True),
        sa.Column("quantity_default", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("listing_status_default", sa.String(length=16), nullable=False, server_default="draft"),
        sa.Column("is_shared", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "environment",
            "username",
            "name",
            name="uq_ebay_listing_template_profile_env_user_name",
        ),
    )
    op.create_index(
        "ix_ebay_listing_template_profiles_environment",
        "ebay_listing_template_profiles",
        ["environment"],
        unique=False,
    )
    op.create_index(
        "ix_ebay_listing_template_profiles_username",
        "ebay_listing_template_profiles",
        ["username"],
        unique=False,
    )
    op.create_index(
        "ix_ebay_listing_template_profiles_name",
        "ebay_listing_template_profiles",
        ["name"],
        unique=False,
    )
    op.create_index(
        "ix_ebay_listing_template_profiles_is_shared",
        "ebay_listing_template_profiles",
        ["is_shared"],
        unique=False,
    )
    op.create_index(
        "ix_ebay_listing_template_profiles_is_default",
        "ebay_listing_template_profiles",
        ["is_default"],
        unique=False,
    )
    op.create_index(
        "ix_ebay_listing_template_profiles_is_active",
        "ebay_listing_template_profiles",
        ["is_active"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ebay_listing_template_profiles_is_active", table_name="ebay_listing_template_profiles")
    op.drop_index("ix_ebay_listing_template_profiles_is_default", table_name="ebay_listing_template_profiles")
    op.drop_index("ix_ebay_listing_template_profiles_is_shared", table_name="ebay_listing_template_profiles")
    op.drop_index("ix_ebay_listing_template_profiles_name", table_name="ebay_listing_template_profiles")
    op.drop_index("ix_ebay_listing_template_profiles_username", table_name="ebay_listing_template_profiles")
    op.drop_index("ix_ebay_listing_template_profiles_environment", table_name="ebay_listing_template_profiles")
    op.drop_table("ebay_listing_template_profiles")
