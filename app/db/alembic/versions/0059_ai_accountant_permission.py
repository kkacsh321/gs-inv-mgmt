"""grant ai accountant permission to ops and admin roles

Revision ID: 0059_ai_accountant_permission
Revises: 0058_assignment_alloc_weight
Create Date: 2026-04-27
"""

from alembic import op


revision = "0059_ai_accountant_permission"
down_revision = "0058_assignment_alloc_weight"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO role_permissions (role, permission, created_at, updated_at)
        VALUES
            ('ops', 'ai_accountant_use', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
            ('admin', 'ai_accountant_use', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT (role, permission) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM role_permissions
        WHERE permission = 'ai_accountant_use'
          AND role IN ('ops', 'admin')
        """
    )
