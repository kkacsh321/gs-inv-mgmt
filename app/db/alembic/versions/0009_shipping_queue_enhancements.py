"""add shipping queue enhancement schema

Revision ID: 0009_shipping_queue_enhancements
Revises: 0008_returns_workflow
Create Date: 2026-03-23
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0009_shipping_queue_enhancements"
down_revision: Union[str, None] = "0008_returns_workflow"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "shipping_presets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("shipping_provider", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("shipping_service", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("shipping_package_type", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_shipping_presets_name", "shipping_presets", ["name"], unique=True)
    op.create_index("ix_shipping_presets_is_default", "shipping_presets", ["is_default"], unique=False)
    op.create_index("ix_shipping_presets_is_active", "shipping_presets", ["is_active"], unique=False)

    op.add_column(
        "sales",
        sa.Column("shipping_package_type", sa.String(length=64), nullable=False, server_default=""),
    )
    op.add_column(
        "sales",
        sa.Column("shipping_exception_code", sa.String(length=64), nullable=False, server_default=""),
    )
    op.add_column(
        "sales",
        sa.Column("shipping_exception_notes", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "sales",
        sa.Column("shipping_exception_action", sa.String(length=64), nullable=False, server_default=""),
    )
    op.add_column("sales", sa.Column("shipping_exception_resolved_at", sa.DateTime(), nullable=True))
    op.add_column(
        "sales",
        sa.Column("shipping_exception_resolved_by", sa.String(length=128), nullable=False, server_default=""),
    )
    op.add_column("sales", sa.Column("shipment_exported_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("sales", "shipment_exported_at")
    op.drop_column("sales", "shipping_exception_resolved_by")
    op.drop_column("sales", "shipping_exception_resolved_at")
    op.drop_column("sales", "shipping_exception_action")
    op.drop_column("sales", "shipping_exception_notes")
    op.drop_column("sales", "shipping_exception_code")
    op.drop_column("sales", "shipping_package_type")

    op.drop_index("ix_shipping_presets_is_active", table_name="shipping_presets")
    op.drop_index("ix_shipping_presets_is_default", table_name="shipping_presets")
    op.drop_index("ix_shipping_presets_name", table_name="shipping_presets")
    op.drop_table("shipping_presets")
