"""add ai provider runtime configs

Revision ID: 0022_ai_provider_configs
Revises: 0021_source_url
Create Date: 2026-03-24 20:20:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0022_ai_provider_configs"
down_revision: Union[str, None] = "0021_source_url"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_provider_configs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("environment", sa.String(length=32), nullable=False, server_default="local"),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False, server_default="openai"),
        sa.Column("model", sa.String(length=128), nullable=False, server_default="gpt-4o-mini"),
        sa.Column("base_url", sa.String(length=255), nullable=False, server_default="https://api.openai.com/v1"),
        sa.Column("endpoint_type", sa.String(length=32), nullable=False, server_default="responses"),
        sa.Column("api_key", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("temperature", sa.Numeric(4, 2), nullable=False, server_default="0.20"),
        sa.Column("max_output_tokens", sa.Integer(), nullable=False, server_default="600"),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("environment", "name", name="uq_ai_provider_config_env_name"),
    )
    op.create_index(
        "ix_ai_provider_configs_environment",
        "ai_provider_configs",
        ["environment"],
        unique=False,
    )
    op.create_index(
        "ix_ai_provider_configs_name",
        "ai_provider_configs",
        ["name"],
        unique=False,
    )
    op.create_index(
        "ix_ai_provider_configs_provider",
        "ai_provider_configs",
        ["provider"],
        unique=False,
    )
    op.create_index(
        "ix_ai_provider_configs_is_default",
        "ai_provider_configs",
        ["is_default"],
        unique=False,
    )
    op.create_index(
        "ix_ai_provider_configs_is_active",
        "ai_provider_configs",
        ["is_active"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ai_provider_configs_is_active", table_name="ai_provider_configs")
    op.drop_index("ix_ai_provider_configs_is_default", table_name="ai_provider_configs")
    op.drop_index("ix_ai_provider_configs_provider", table_name="ai_provider_configs")
    op.drop_index("ix_ai_provider_configs_name", table_name="ai_provider_configs")
    op.drop_index("ix_ai_provider_configs_environment", table_name="ai_provider_configs")
    op.drop_table("ai_provider_configs")
