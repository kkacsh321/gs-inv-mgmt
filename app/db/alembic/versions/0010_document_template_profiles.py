"""add document template profiles

Revision ID: 0010_document_template_profiles
Revises: 0009_shipping_queue_enhancements
Create Date: 2026-03-23
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0010_document_template_profiles"
down_revision: Union[str, None] = "0009_shipping_queue_enhancements"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "document_template_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("environment", sa.String(length=32), nullable=False, server_default="local"),
        sa.Column("doc_type", sa.String(length=32), nullable=False, server_default="all"),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("template_name", sa.String(length=64), nullable=False, server_default="Classic"),
        sa.Column("accent_color", sa.String(length=16), nullable=False, server_default="#b45309"),
        sa.Column("company_name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("company_email", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("company_phone", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("company_website", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "environment",
            "doc_type",
            "name",
            name="uq_document_template_profile_env_doc_type_name",
        ),
    )
    op.create_index(
        "ix_document_template_profiles_environment",
        "document_template_profiles",
        ["environment"],
        unique=False,
    )
    op.create_index(
        "ix_document_template_profiles_doc_type",
        "document_template_profiles",
        ["doc_type"],
        unique=False,
    )
    op.create_index(
        "ix_document_template_profiles_name",
        "document_template_profiles",
        ["name"],
        unique=False,
    )
    op.create_index(
        "ix_document_template_profiles_is_default",
        "document_template_profiles",
        ["is_default"],
        unique=False,
    )
    op.create_index(
        "ix_document_template_profiles_is_active",
        "document_template_profiles",
        ["is_active"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_document_template_profiles_is_active", table_name="document_template_profiles")
    op.drop_index("ix_document_template_profiles_is_default", table_name="document_template_profiles")
    op.drop_index("ix_document_template_profiles_name", table_name="document_template_profiles")
    op.drop_index("ix_document_template_profiles_doc_type", table_name="document_template_profiles")
    op.drop_index("ix_document_template_profiles_environment", table_name="document_template_profiles")
    op.drop_table("document_template_profiles")
