"""coin ai run history

Revision ID: 0024_coin_ai_runs
Revises: 0023_runtime_settings
Create Date: 2026-03-24 18:40:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0024_coin_ai_runs"
down_revision: Union[str, None] = "0023_runtime_settings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "coin_ai_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("environment", sa.String(length=32), nullable=False, server_default="local"),
        sa.Column("tool_name", sa.String(length=32), nullable=False),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("listing_id", sa.Integer(), nullable=True),
        sa.Column("input_hint", sa.Text(), nullable=False, server_default=""),
        sa.Column("image_filename", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("image_content_type", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("result_markdown", sa.Text(), nullable=False, server_default=""),
        sa.Column("result_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("web_rows_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["listing_id"], ["marketplace_listings.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_coin_ai_runs_environment", "coin_ai_runs", ["environment"], unique=False)
    op.create_index("ix_coin_ai_runs_tool_name", "coin_ai_runs", ["tool_name"], unique=False)
    op.create_index("ix_coin_ai_runs_username", "coin_ai_runs", ["username"], unique=False)
    op.create_index("ix_coin_ai_runs_product_id", "coin_ai_runs", ["product_id"], unique=False)
    op.create_index("ix_coin_ai_runs_listing_id", "coin_ai_runs", ["listing_id"], unique=False)
    op.create_index("ix_coin_ai_runs_created_at", "coin_ai_runs", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_coin_ai_runs_created_at", table_name="coin_ai_runs")
    op.drop_index("ix_coin_ai_runs_listing_id", table_name="coin_ai_runs")
    op.drop_index("ix_coin_ai_runs_product_id", table_name="coin_ai_runs")
    op.drop_index("ix_coin_ai_runs_username", table_name="coin_ai_runs")
    op.drop_index("ix_coin_ai_runs_tool_name", table_name="coin_ai_runs")
    op.drop_index("ix_coin_ai_runs_environment", table_name="coin_ai_runs")
    op.drop_table("coin_ai_runs")
