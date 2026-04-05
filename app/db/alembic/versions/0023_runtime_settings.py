"""add runtime settings table

Revision ID: 0023_runtime_settings
Revises: 0022_ai_provider_configs
Create Date: 2026-03-24 21:10:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0023_runtime_settings"
down_revision: Union[str, None] = "0022_ai_provider_configs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "runtime_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("environment", sa.String(length=32), nullable=False, server_default="local"),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        sa.Column("value_type", sa.String(length=16), nullable=False, server_default="str"),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_by", sa.String(length=128), nullable=False, server_default="system"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("environment", "key", name="uq_runtime_setting_env_key"),
    )
    op.create_index("ix_runtime_settings_environment", "runtime_settings", ["environment"], unique=False)
    op.create_index("ix_runtime_settings_key", "runtime_settings", ["key"], unique=False)
    op.create_index("ix_runtime_settings_is_active", "runtime_settings", ["is_active"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_runtime_settings_is_active", table_name="runtime_settings")
    op.drop_index("ix_runtime_settings_key", table_name="runtime_settings")
    op.drop_index("ix_runtime_settings_environment", table_name="runtime_settings")
    op.drop_table("runtime_settings")
