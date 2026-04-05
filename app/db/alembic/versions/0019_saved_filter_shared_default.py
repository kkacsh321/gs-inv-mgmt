"""add shared/default flags to saved filter profiles

Revision ID: 0019_saved_filter_shared_default
Revises: 0018_saved_filter_profiles
Create Date: 2026-03-24 18:35:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0019_saved_filter_shared_default"
down_revision: Union[str, None] = "0018_saved_filter_profiles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "saved_filter_profiles",
        sa.Column("is_shared", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "saved_filter_profiles",
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_index(
        "ix_saved_filter_profiles_is_shared",
        "saved_filter_profiles",
        ["is_shared"],
        unique=False,
    )
    op.create_index(
        "ix_saved_filter_profiles_is_default",
        "saved_filter_profiles",
        ["is_default"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_saved_filter_profiles_is_default", table_name="saved_filter_profiles")
    op.drop_index("ix_saved_filter_profiles_is_shared", table_name="saved_filter_profiles")
    op.drop_column("saved_filter_profiles", "is_default")
    op.drop_column("saved_filter_profiles", "is_shared")
