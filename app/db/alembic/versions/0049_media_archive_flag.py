"""add media archive flag

Revision ID: 0049_media_archive_flag
Revises: 0048_order_party_payload
Create Date: 2026-04-14
"""

from alembic import op
import sqlalchemy as sa


revision = "0049_media_archive_flag"
down_revision = "0048_order_party_payload"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("media_assets") as batch_op:
        batch_op.add_column(
            sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    op.create_index("ix_media_assets_is_archived", "media_assets", ["is_archived"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_media_assets_is_archived", table_name="media_assets")
    with op.batch_alter_table("media_assets") as batch_op:
        batch_op.drop_column("is_archived")

