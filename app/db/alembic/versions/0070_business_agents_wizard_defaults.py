"""default business agent wizard prompts

Revision ID: 0070_business_agents
Revises: 0069_goldie_ai_accountant
Create Date: 2026-05-27
"""

from alembic import op
import sqlalchemy as sa


revision = "0070_business_agents"
down_revision = "0069_goldie_ai_accountant"
branch_labels = None
depends_on = None


KURT_SYSTEM_MESSAGE = (
    "You are Kurt, GoldenStackers' Inventory Intake Agent. You specialize in turning Slack messages, photos, "
    "purchase documents, invoices, and operator notes into structured inventory intake drafts for coins, bullion, "
    "collectibles, antiques, and resale goods. Extract likely product identity, category, quantity, lot/source "
    "relationships, media evidence, package facts, and cost-basis evidence. Be explicit about uncertainty and ask "
    "short confirmation questions for missing cost, lot, quantity, weight, metal, condition, or source details. "
    "Never blur product unit cost with whole-lot landed cost or assignment-level cost."
)

KURT_CHAT_INSTRUCTION = (
    "Respond as Kurt. Produce a compact intake draft with proposed fields, confidence, missing confirmations, "
    "cost-basis/lot evidence notes, and the next approval-gated action. Do not directly write records unless the "
    "app has routed the request through the approved intake workflow."
)

MURDOCK_SYSTEM_MESSAGE = (
    "You are Murdock, GoldenStackers' Listing and Sales Copy Agent. You specialize in creating eBay-ready listing "
    "drafts that are accurate, policy-aware, buyer-facing, and compelling. Use product evidence, comps, fee and "
    "breakeven estimates, category requirements, condition rules, item specifics, media readiness, and shipping "
    "facts. Write descriptions with clear formatting preserved as eBay-safe HTML. Do not invent grade, metal, "
    "weight, brand, authenticity, scarcity, or handmade claims unless evidence supports them."
)

MURDOCK_CHAT_INSTRUCTION = (
    "Respond as Murdock. Produce a concise listing draft plan with title, buyer-facing description direction, "
    "category/condition/item-specifics readiness, media/video status, price/fee/breakeven notes, publish blockers, "
    "and the next approval-gated action. Preserve eBay policy and evidence boundaries."
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
                    'business_chat_room_enabled',
                    'true',
                    'bool',
                    'Enable Goldy Business Chat Room roster and specialist-agent routing context.'
                ),
                (
                    'business_chat_room_agents_csv',
                    'atlas,kurt,murdock,scout,goldie',
                    'str',
                    'Ordered business chat room agents shown in Ask and used for operator-facing routing copy.'
                ),
                (
                    'business_chat_room_ai_replies_enabled',
                    'true',
                    'bool',
                    'Enable AI specialist replies on the dedicated Business Chat Room page.'
                ),
                (
                    'business_chat_room_max_agent_replies',
                    '5',
                    'int',
                    'Maximum specialist agent replies generated per Business Chat Room user turn.'
                ),
                (
                    'business_chat_room_write_actions_require_approval',
                    'true',
                    'bool',
                    'Capture Business Chat Room write/action requests as blocked approval-gated queue jobs.'
                ),
                (
                    'slack_ops_intent_listing_enabled',
                    'true',
                    'bool',
                    'Enable Slack listing/Murdock command ingestion for approval-gated listing draft assistance.'
                ),
                (
                    'slack_ops_listing_system_message',
                    :murdock_system_message,
                    'str',
                    'Murdock listing agent system message for Slack listing draft assistance.'
                ),
                (
                    'slack_ops_listing_instruction',
                    :murdock_chat_instruction,
                    'str',
                    'Murdock listing agent instruction for Slack listing draft assistance.'
                ),
                (
                    'slack_ops_intake_system_message',
                    :kurt_system_message,
                    'str',
                    'Kurt inventory intake agent system message for Slack intake draft assistance.'
                ),
                (
                    'slack_ops_intake_instruction',
                    :kurt_chat_instruction,
                    'str',
                    'Kurt inventory intake agent instruction for Slack intake draft assistance.'
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
            'migration_0070_business_agents',
            true,
            CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP
        FROM envs
        CROSS JOIN defaults
        ON CONFLICT (environment, key) DO UPDATE
        SET
            value_type = EXCLUDED.value_type,
            description = EXCLUDED.description,
            updated_by = EXCLUDED.updated_by,
            is_active = true,
            updated_at = CURRENT_TIMESTAMP
        WHERE runtime_settings.value IS NULL
           OR btrim(runtime_settings.value) = ''
           OR runtime_settings.key IN (
                'business_chat_room_enabled',
                'business_chat_room_agents_csv',
                'slack_ops_intent_listing_enabled'
           )
        """
        ),
        {
            "kurt_system_message": KURT_SYSTEM_MESSAGE,
            "kurt_chat_instruction": KURT_CHAT_INSTRUCTION,
            "murdock_system_message": MURDOCK_SYSTEM_MESSAGE,
            "murdock_chat_instruction": MURDOCK_CHAT_INSTRUCTION,
        },
    )


def downgrade() -> None:
    pass
