"""add rbac users and role permissions

Revision ID: 0011_rbac_users_and_permissions
Revises: 0010_document_template_profiles
Create Date: 2026-03-23
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0011_rbac_users_and_permissions"
down_revision: Union[str, None] = "0010_document_template_profiles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "app_users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False, server_default="viewer"),
        sa.Column("display_name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("email", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )
    op.create_index("ix_app_users_username", "app_users", ["username"], unique=True)
    op.create_index("ix_app_users_role", "app_users", ["role"], unique=False)
    op.create_index("ix_app_users_is_active", "app_users", ["is_active"], unique=False)

    op.create_table(
        "role_permissions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("permission", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("role", "permission", name="uq_role_permission"),
    )
    op.create_index("ix_role_permissions_role", "role_permissions", ["role"], unique=False)
    op.create_index("ix_role_permissions_permission", "role_permissions", ["permission"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_role_permissions_permission", table_name="role_permissions")
    op.drop_index("ix_role_permissions_role", table_name="role_permissions")
    op.drop_table("role_permissions")

    op.drop_index("ix_app_users_is_active", table_name="app_users")
    op.drop_index("ix_app_users_role", table_name="app_users")
    op.drop_index("ix_app_users_username", table_name="app_users")
    op.drop_table("app_users")
