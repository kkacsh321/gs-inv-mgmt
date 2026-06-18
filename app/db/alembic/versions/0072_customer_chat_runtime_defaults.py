"""enable customer chat and Slack defaults

Revision ID: 0072_customer_chat_defaults
Revises: 0071_customers
Create Date: 2026-06-03
"""

from alembic import op
import sqlalchemy as sa


revision = "0072_customer_chat_defaults"
down_revision = "0071_customers"
branch_labels = None
depends_on = None


CUSTOMER_SYSTEM_MESSAGE = (
    "You are a concise customer intelligence analyst for GoldenStackers operators. Use order/customer evidence "
    "to summarize repeat buyers, customer rollups, follow-up priorities, and internal-note presence. Treat "
    "internal customer notes as private operator context and never write customer-facing messages from them."
)

CUSTOMER_CHAT_INSTRUCTION = (
    "Answer from the provided customer snapshot. Summarize repeat-buyer status, dormant buyers, customer-note "
    "presence, and follow-up priorities. Keep the response read-only and cite that internal notes are operator "
    "context only."
)


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            """
        WITH envs AS (
            SELECT DISTINCT environment FROM runtime_settings
            UNION
            SELECT 'local'
        ),
        defaults(key, value, value_type, description) AS (
            VALUES
                (
                    'chat_allowed_domains_ops_csv',
                    'accounting,customers,inventory,listings,orders,reports,sales,shipping,sync,tax',
                    'str',
                    'Allowed Ask GoldenStackers read-only data domains for ops users.'
                ),
                (
                    'chat_allowed_domains_admin_csv',
                    'accounting,admin,customers,inventory,listings,orders,reports,sales,shipping,sync,tax',
                    'str',
                    'Allowed Ask GoldenStackers read-only data domains for admin users.'
                ),
                (
                    'slack_ops_intent_customer_enabled',
                    'true',
                    'bool',
                    'Enable Slack customer/repeat-buyer intelligence questions.'
                ),
                (
                    'slack_ops_customer_system_message',
                    :customer_system_message,
                    'str',
                    'Customer intelligence agent system message for Slack customer questions.'
                ),
                (
                    'slack_ops_customer_instruction',
                    :customer_chat_instruction,
                    'str',
                    'Customer intelligence agent instruction for Slack customer questions.'
                )
        )
        INSERT INTO runtime_settings (
            environment,
            key,
            value,
            value_type,
            description,
            updated_by,
            is_active,
            created_at,
            updated_at
        )
        SELECT
            envs.environment,
            defaults.key,
            defaults.value,
            defaults.value_type,
            defaults.description,
            'migration_0072_customer_chat_defaults',
            true,
            CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP
        FROM envs
        CROSS JOIN defaults
        ON CONFLICT (environment, key) DO UPDATE
        SET
            value = CASE
                WHEN runtime_settings.value IS NULL
                  OR btrim(runtime_settings.value) = ''
                  OR runtime_settings.key = 'slack_ops_intent_customer_enabled'
                THEN EXCLUDED.value
                ELSE runtime_settings.value
            END,
            value_type = EXCLUDED.value_type,
            description = EXCLUDED.description,
            updated_by = EXCLUDED.updated_by,
            is_active = true,
            updated_at = CURRENT_TIMESTAMP
        WHERE runtime_settings.value IS NULL
           OR btrim(runtime_settings.value) = ''
           OR runtime_settings.key = 'slack_ops_intent_customer_enabled'
        """
        ),
        {
            "customer_system_message": CUSTOMER_SYSTEM_MESSAGE,
            "customer_chat_instruction": CUSTOMER_CHAT_INSTRUCTION,
        },
    )
    op.get_bind().execute(
        sa.text(
            """
        UPDATE runtime_settings
        SET
            value = CASE
                WHEN value IS NULL OR btrim(value) = '' THEN 'customers'
                ELSE rtrim(value, ', ') || ',customers'
            END,
            updated_by = 'migration_0072_customer_chat_defaults',
            is_active = true,
            updated_at = CURRENT_TIMESTAMP
        WHERE key IN ('chat_allowed_domains_ops_csv', 'chat_allowed_domains_admin_csv')
          AND NOT EXISTS (
              SELECT 1
              FROM regexp_split_to_table(lower(coalesce(runtime_settings.value, '')), '\\s*,\\s*') AS token
              WHERE token = 'customers'
          )
        """
        )
    )


def downgrade() -> None:
    pass
