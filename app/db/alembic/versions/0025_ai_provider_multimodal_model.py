"""add ai provider multimodal model

Revision ID: 0025_ai_provider_mm_model
Revises: 0024_coin_ai_runs
Create Date: 2026-03-25 07:15:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0025_ai_provider_mm_model"
down_revision: Union[str, None] = "0024_coin_ai_runs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ai_provider_configs",
        sa.Column("multimodal_model", sa.String(length=128), nullable=False, server_default=""),
    )
    op.execute(
        "UPDATE ai_provider_configs SET multimodal_model = model WHERE COALESCE(multimodal_model, '') = ''"
    )


def downgrade() -> None:
    op.drop_column("ai_provider_configs", "multimodal_model")
