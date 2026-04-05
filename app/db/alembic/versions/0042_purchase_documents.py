"""add purchase documents table for incoming invoices/receipts

Revision ID: 0042_purchase_documents
Revises: 0041_document_artifacts
Create Date: 2026-04-02
"""

from alembic import op
import sqlalchemy as sa


revision = "0042_purchase_documents"
down_revision = "0041_document_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "purchase_documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lot_id", sa.Integer(), nullable=True),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("source_id", sa.Integer(), nullable=True),
        sa.Column("document_kind", sa.String(length=64), nullable=False, server_default="incoming_invoice"),
        sa.Column("title", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("original_filename", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("content_type", sa.String(length=128), nullable=False, server_default="application/octet-stream"),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("content_sha256", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("s3_bucket", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("s3_key", sa.String(length=512), nullable=False),
        sa.Column("s3_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("ai_extracted_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("ai_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("uploaded_by", sa.String(length=128), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["lot_id"], ["purchase_lots.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_id"], ["inventory_sources.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("s3_key"),
    )
    op.create_index("ix_purchase_documents_lot_id", "purchase_documents", ["lot_id"], unique=False)
    op.create_index("ix_purchase_documents_product_id", "purchase_documents", ["product_id"], unique=False)
    op.create_index("ix_purchase_documents_source_id", "purchase_documents", ["source_id"], unique=False)
    op.create_index("ix_purchase_documents_document_kind", "purchase_documents", ["document_kind"], unique=False)
    op.create_index("ix_purchase_documents_content_sha256", "purchase_documents", ["content_sha256"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_purchase_documents_content_sha256", table_name="purchase_documents")
    op.drop_index("ix_purchase_documents_document_kind", table_name="purchase_documents")
    op.drop_index("ix_purchase_documents_source_id", table_name="purchase_documents")
    op.drop_index("ix_purchase_documents_product_id", table_name="purchase_documents")
    op.drop_index("ix_purchase_documents_lot_id", table_name="purchase_documents")
    op.drop_table("purchase_documents")
