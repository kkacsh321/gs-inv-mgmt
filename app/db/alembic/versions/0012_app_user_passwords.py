"""add app user password fields

Revision ID: 0012_app_user_passwords
Revises: 0011_rbac_users_and_permissions
Create Date: 2026-03-23
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0012_app_user_passwords"
down_revision: Union[str, None] = "0011_rbac_users_and_permissions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "app_users",
        sa.Column("password_hash", sa.String(length=512), nullable=False, server_default=""),
    )
    op.add_column(
        "app_users",
        sa.Column("password_salt", sa.String(length=128), nullable=False, server_default=""),
    )
    op.add_column(
        "app_users",
        sa.Column("password_updated_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("app_users", "password_updated_at")
    op.drop_column("app_users", "password_salt")
    op.drop_column("app_users", "password_hash")
