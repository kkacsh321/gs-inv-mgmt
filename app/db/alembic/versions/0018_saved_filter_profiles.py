"""add saved filter profiles table

Revision ID: 0018_saved_filter_profiles
Revises: 0017_ebay_publish_presets
Create Date: 2026-03-24 18:10:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0018_saved_filter_profiles"
down_revision: Union[str, None] = "0017_ebay_publish_presets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "saved_filter_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("environment", sa.String(length=32), nullable=False, server_default="local"),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("filter_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "environment",
            "username",
            "scope",
            "name",
            name="uq_saved_filter_profile_env_user_scope_name",
        ),
    )
    op.create_index(
        "ix_saved_filter_profiles_environment",
        "saved_filter_profiles",
        ["environment"],
        unique=False,
    )
    op.create_index(
        "ix_saved_filter_profiles_username",
        "saved_filter_profiles",
        ["username"],
        unique=False,
    )
    op.create_index(
        "ix_saved_filter_profiles_scope",
        "saved_filter_profiles",
        ["scope"],
        unique=False,
    )
    op.create_index(
        "ix_saved_filter_profiles_name",
        "saved_filter_profiles",
        ["name"],
        unique=False,
    )
    op.create_index(
        "ix_saved_filter_profiles_is_active",
        "saved_filter_profiles",
        ["is_active"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_saved_filter_profiles_is_active", table_name="saved_filter_profiles")
    op.drop_index("ix_saved_filter_profiles_name", table_name="saved_filter_profiles")
    op.drop_index("ix_saved_filter_profiles_scope", table_name="saved_filter_profiles")
    op.drop_index("ix_saved_filter_profiles_username", table_name="saved_filter_profiles")
    op.drop_index("ix_saved_filter_profiles_environment", table_name="saved_filter_profiles")
    op.drop_table("saved_filter_profiles")
