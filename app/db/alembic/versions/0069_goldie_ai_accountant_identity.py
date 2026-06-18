"""name AI Accountant Goldie by default

Revision ID: 0069_goldie_ai_accountant
Revises: 0068_ebay_fee_defaults
Create Date: 2026-05-21
"""

from alembic import op
import sqlalchemy as sa


revision = "0069_goldie_ai_accountant"
down_revision = "0068_ebay_fee_defaults"
branch_labels = None
depends_on = None


GOLDIE_SYSTEM_MESSAGE = (
    "You are Goldie, GoldenStackers' AI Accountant, a vigilant read-only accounting controller for a coin, "
    "bullion, collectibles, and resale business. Answer to the name Goldie in Ask GoldenStackers, the Goldie "
    "workspace, and Slack. You continuously watch cost basis, lot allocation, COGS, gross/net sales, marketplace "
    "fees, shipping label spend, returns, tax evidence, close readiness, and sign-off evidence. Be precise, cite "
    "provided evidence, label estimates versus actuals, and identify concrete corrections. You may summarize "
    "local/state/federal tax research for planning, but never give filing, legal, or tax-advisor replacement "
    "conclusions. Route unsupported tax/legal determinations to human advisor review."
)

GOLDIE_CHAT_INSTRUCTION = (
    "Answer as Goldie, the AI Accountant. Use app evidence as source of truth, use web research only as external "
    "context that requires verification, and return concise markdown with direct answer, evidence checked, "
    "risks/corrections, and advisor-review notes. Do not propose direct writes."
)

GOLDIE_MONITOR_INSTRUCTION = (
    "Review Goldie's scheduled AI Accountant monitor evidence. Return concise markdown with: close/watch status, "
    "highest-risk findings, corrections to make, profit/cost-basis notes, and tax/advisor-review notes. "
    "When profit or COGS basis is questioned, use sale_fifo_cogs_evidence_rows to trace sale COGS back to "
    "product, lot, assignment, quantity, unit cost, total cost, and source. "
    "Do not propose direct writes; recommend human-reviewed corrections only."
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
                    'accountant_llm_system_message',
                    :system_message,
                    'str',
                    'Primary Goldie AI Accountant identity/system message for reviews, chat, and Slack answers.'
                ),
                (
                    'ai_accountant_chat_instruction',
                    :chat_instruction,
                    'str',
                    'Instruction prompt for interactive Goldie chat and Slack responses.'
                ),
                (
                    'ai_accountant_monitor_review_instruction',
                    :monitor_instruction,
                    'str',
                    'Instruction prompt for automated scheduled Goldie monitor reviews.'
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
            'migration_0069_goldie_ai_accountant',
            true,
            CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP
        FROM envs
        CROSS JOIN defaults
        ON CONFLICT (environment, key) DO UPDATE
        SET
            value = EXCLUDED.value,
            value_type = EXCLUDED.value_type,
            description = EXCLUDED.description,
            updated_by = EXCLUDED.updated_by,
            is_active = true,
            updated_at = CURRENT_TIMESTAMP
        WHERE runtime_settings.value IS NULL
           OR btrim(runtime_settings.value) = ''
           OR runtime_settings.value LIKE 'You are GoldenStackers'' AI Accountant,%'
           OR runtime_settings.value LIKE 'Answer as the AI Accountant.%'
           OR runtime_settings.value LIKE 'Review the scheduled AI Accountant monitor evidence.%'
        """
        ),
        {
            "system_message": GOLDIE_SYSTEM_MESSAGE,
            "chat_instruction": GOLDIE_CHAT_INSTRUCTION,
            "monitor_instruction": GOLDIE_MONITOR_INSTRUCTION,
        },
    )


def downgrade() -> None:
    pass
