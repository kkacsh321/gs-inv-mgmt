"""add inventory sources master data

Revision ID: 0006_inventory_sources
Revises: 0005_inventory_movements
Create Date: 2026-03-23
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0006_inventory_sources"
down_revision: Union[str, None] = "0005_inventory_movements"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "inventory_sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False, server_default="vendor"),
        sa.Column("contact_name", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("contact_email", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("contact_phone", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_inventory_sources_name", "inventory_sources", ["name"], unique=True)
    op.create_index("ix_inventory_sources_source_type", "inventory_sources", ["source_type"], unique=False)
    op.create_index("ix_inventory_sources_is_active", "inventory_sources", ["is_active"], unique=False)

    op.add_column("purchase_lots", sa.Column("source_id", sa.Integer(), nullable=True))
    op.create_index("ix_purchase_lots_source_id", "purchase_lots", ["source_id"], unique=False)
    op.create_foreign_key(
        "fk_purchase_lots_source_id_inventory_sources",
        "purchase_lots",
        "inventory_sources",
        ["source_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_purchase_lots_source_id_inventory_sources", "purchase_lots", type_="foreignkey")
    op.drop_index("ix_purchase_lots_source_id", table_name="purchase_lots")
    op.drop_column("purchase_lots", "source_id")

    op.drop_index("ix_inventory_sources_is_active", table_name="inventory_sources")
    op.drop_index("ix_inventory_sources_source_type", table_name="inventory_sources")
    op.drop_index("ix_inventory_sources_name", table_name="inventory_sources")
    op.drop_table("inventory_sources")
