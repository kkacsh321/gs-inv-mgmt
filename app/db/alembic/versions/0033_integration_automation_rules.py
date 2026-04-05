"""add integration automation rules

Revision ID: 0033_integration_automation
Revises: 0032_shipping_label_fields
Create Date: 2026-03-29 17:20:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0033_integration_automation"
down_revision = "0032_shipping_label_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "integration_automation_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("environment", sa.String(length=32), nullable=False, server_default="local"),
        sa.Column("integration", sa.String(length=64), nullable=False, server_default="shipping"),
        sa.Column("action", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("name", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("trigger_status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("conditions_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("effect_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("requires_approval", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_by", sa.String(length=128), nullable=False, server_default="system"),
        sa.Column("updated_by", sa.String(length=128), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "environment",
            "integration",
            "action",
            "name",
            name="uq_integration_automation_rule_env_integration_action_name",
        ),
    )
    op.create_index(
        "ix_integration_automation_rules_environment",
        "integration_automation_rules",
        ["environment"],
        unique=False,
    )
    op.create_index(
        "ix_integration_automation_rules_integration",
        "integration_automation_rules",
        ["integration"],
        unique=False,
    )
    op.create_index(
        "ix_integration_automation_rules_action",
        "integration_automation_rules",
        ["action"],
        unique=False,
    )
    op.create_index(
        "ix_integration_automation_rules_name",
        "integration_automation_rules",
        ["name"],
        unique=False,
    )
    op.create_index(
        "ix_integration_automation_rules_trigger_status",
        "integration_automation_rules",
        ["trigger_status"],
        unique=False,
    )
    op.create_index(
        "ix_integration_automation_rules_requires_approval",
        "integration_automation_rules",
        ["requires_approval"],
        unique=False,
    )
    op.create_index(
        "ix_integration_automation_rules_is_active",
        "integration_automation_rules",
        ["is_active"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_integration_automation_rules_is_active", table_name="integration_automation_rules")
    op.drop_index("ix_integration_automation_rules_requires_approval", table_name="integration_automation_rules")
    op.drop_index("ix_integration_automation_rules_trigger_status", table_name="integration_automation_rules")
    op.drop_index("ix_integration_automation_rules_name", table_name="integration_automation_rules")
    op.drop_index("ix_integration_automation_rules_action", table_name="integration_automation_rules")
    op.drop_index("ix_integration_automation_rules_integration", table_name="integration_automation_rules")
    op.drop_index("ix_integration_automation_rules_environment", table_name="integration_automation_rules")
    op.drop_table("integration_automation_rules")
