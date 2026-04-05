"""add immutable document artifacts table

Revision ID: 0041_document_artifacts
Revises: 0040_product_inventory_class
Create Date: 2026-04-02
"""

from alembic import op
import sqlalchemy as sa


revision = "0041_document_artifacts"
down_revision = "0040_product_inventory_class"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_artifacts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("environment", sa.String(length=32), nullable=False, server_default="local"),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=True),
        sa.Column("doc_type", sa.String(length=32), nullable=False),
        sa.Column("document_number", sa.String(length=128), nullable=False),
        sa.Column("artifact_kind", sa.String(length=64), nullable=False, server_default="printable_html"),
        sa.Column("file_name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("mime_type", sa.String(length=128), nullable=False, server_default="text/html"),
        sa.Column("content_sha256", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("storage_backend", sa.String(length=32), nullable=False, server_default="db_inline"),
        sa.Column("storage_ref", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("content_base64", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(length=128), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_document_artifacts_environment", "document_artifacts", ["environment"], unique=False)
    op.create_index("ix_document_artifacts_source_type", "document_artifacts", ["source_type"], unique=False)
    op.create_index("ix_document_artifacts_source_id", "document_artifacts", ["source_id"], unique=False)
    op.create_index("ix_document_artifacts_doc_type", "document_artifacts", ["doc_type"], unique=False)
    op.create_index("ix_document_artifacts_document_number", "document_artifacts", ["document_number"], unique=False)
    op.create_index("ix_document_artifacts_artifact_kind", "document_artifacts", ["artifact_kind"], unique=False)
    op.create_index("ix_document_artifacts_content_sha256", "document_artifacts", ["content_sha256"], unique=False)
    op.create_index("ix_document_artifacts_created_at", "document_artifacts", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_document_artifacts_created_at", table_name="document_artifacts")
    op.drop_index("ix_document_artifacts_content_sha256", table_name="document_artifacts")
    op.drop_index("ix_document_artifacts_artifact_kind", table_name="document_artifacts")
    op.drop_index("ix_document_artifacts_document_number", table_name="document_artifacts")
    op.drop_index("ix_document_artifacts_doc_type", table_name="document_artifacts")
    op.drop_index("ix_document_artifacts_source_id", table_name="document_artifacts")
    op.drop_index("ix_document_artifacts_source_type", table_name="document_artifacts")
    op.drop_index("ix_document_artifacts_environment", table_name="document_artifacts")
    op.drop_table("document_artifacts")
