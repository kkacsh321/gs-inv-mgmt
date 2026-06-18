from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.ai_accountant_identity import AI_ACCOUNTANT_LABEL, AI_ACCOUNTANT_NAME


@dataclass(frozen=True)
class BusinessAgent:
    key: str
    name: str
    role: str
    label: str
    domains: frozenset[str]
    slack_aliases: tuple[str, ...]
    write_capable: bool
    system_message: str
    chat_instruction: str


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

BUSINESS_AGENT_REGISTRY: dict[str, BusinessAgent] = {
    "kurt_intake_agent": BusinessAgent(
        key="kurt_intake_agent",
        name="Kurt",
        role="Inventory Intake Agent",
        label="Kurt (Inventory Intake)",
        domains=frozenset({"inventory", "orders", "shipping", "reports", "listings"}),
        slack_aliases=("kurt", "intake", "inventory-intake", "inventory_intake"),
        write_capable=True,
        system_message=KURT_SYSTEM_MESSAGE,
        chat_instruction=KURT_CHAT_INSTRUCTION,
    ),
    "murdock_listing_agent": BusinessAgent(
        key="murdock_listing_agent",
        name="Murdock",
        role="Listing and Sales Copy Agent",
        label="Murdock (Listing + Sales Copy)",
        domains=frozenset({"listings", "inventory", "sales", "reports", "sync"}),
        slack_aliases=("murdock", "listing", "listings", "draft-listing", "draft_listing"),
        write_capable=True,
        system_message=MURDOCK_SYSTEM_MESSAGE,
        chat_instruction=MURDOCK_CHAT_INSTRUCTION,
    ),
    "goldie_accountant_agent": BusinessAgent(
        key="goldie_accountant_agent",
        name=AI_ACCOUNTANT_NAME,
        role="AI Accountant",
        label=AI_ACCOUNTANT_LABEL,
        domains=frozenset({"accounting", "reports", "sales", "orders", "customers", "inventory", "tax"}),
        slack_aliases=("goldie", "accountant", "accounting", "tax"),
        write_capable=False,
        system_message="Goldie is the specialist accounting controller for close readiness, COGS, taxes, and cost-basis evidence.",
        chat_instruction="Route accounting and tax evidence questions to Goldie. Goldie remains read-only.",
    ),
    "research_pricing_agent": BusinessAgent(
        key="research_pricing_agent",
        name="Scout",
        role="Research and Pricing Agent",
        label="Scout (Research + Pricing)",
        domains=frozenset({"inventory", "listings", "sales", "reports"}),
        slack_aliases=("scout", "research", "pricing", "comps", "comp"),
        write_capable=False,
        system_message="Scout researches market evidence, sold comps, dealer references, melt/spot context, and pricing confidence.",
        chat_instruction="Return pricing evidence, confidence, and what would improve the comp before a listing price is trusted.",
    ),
    "business_monitor_agent": BusinessAgent(
        key="business_monitor_agent",
        name="Atlas",
        role="Business Direction and Monitoring Agent",
        label="Atlas (Business Monitor)",
        domains=frozenset({"admin", "reports", "sales", "orders", "customers", "inventory", "sync", "shipping"}),
        slack_aliases=("atlas", "business", "monitor", "status"),
        write_capable=False,
        system_message="Atlas watches business direction, operational health, sales trends, backlog, sync health, and priorities.",
        chat_instruction="Summarize business state, risks, and next priorities without performing writes.",
    ),
}


BUSINESS_CHAT_ROOM_AGENT_ORDER: tuple[str, ...] = (
    "business_monitor_agent",
    "kurt_intake_agent",
    "murdock_listing_agent",
    "research_pricing_agent",
    "goldie_accountant_agent",
)


def get_business_agent(key: str) -> BusinessAgent | None:
    return BUSINESS_AGENT_REGISTRY.get(str(key or "").strip().lower())


def resolve_business_agent_key(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_ -]+", " ", str(value or "").strip().lower())
    normalized = "_".join(normalized.replace("-", "_").split())
    if not normalized:
        return ""
    if normalized in BUSINESS_AGENT_REGISTRY:
        return normalized
    for key, agent in BUSINESS_AGENT_REGISTRY.items():
        labels = {
            agent.name.lower(),
            agent.role.lower(),
            agent.label.lower(),
            *[alias.lower() for alias in agent.slack_aliases],
        }
        normalized_labels = {
            "_".join(label.replace("-", "_").split())
            for label in labels
            if label.strip()
        }
        if normalized in normalized_labels:
            return key
    return ""


def detect_business_agent_mentions(text: str) -> list[str]:
    haystack = f" {str(text or '').lower()} "
    found: list[str] = []
    for key in BUSINESS_CHAT_ROOM_AGENT_ORDER:
        agent = BUSINESS_AGENT_REGISTRY[key]
        candidates = (agent.name, *agent.slack_aliases)
        for candidate in candidates:
            token = str(candidate or "").strip().lower()
            if not token:
                continue
            pattern = rf"(?<![a-z0-9_])@?{re.escape(token)}(?![a-z0-9_])"
            if re.search(pattern, haystack):
                found.append(key)
                break
    return found


def business_agent_labels() -> dict[str, str]:
    return {key: agent.label for key, agent in BUSINESS_AGENT_REGISTRY.items()}


def business_agent_domain_scopes() -> dict[str, set[str]]:
    return {key: set(agent.domains) for key, agent in BUSINESS_AGENT_REGISTRY.items()}


def build_business_chat_room_roster() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for key in BUSINESS_CHAT_ROOM_AGENT_ORDER:
        agent = BUSINESS_AGENT_REGISTRY[key]
        rows.append(
            {
                "key": agent.key,
                "name": agent.name,
                "role": agent.role,
                "label": agent.label,
                "domains": sorted(agent.domains),
                "slack_aliases": list(agent.slack_aliases),
                "write_capable": agent.write_capable,
            }
        )
    return rows


def build_business_chat_room_plan(*, prompt: str, selected_agent: str, allowed_domains: set[str]) -> dict[str, object]:
    agent = get_business_agent(selected_agent)
    roster = build_business_chat_room_roster()
    participants = [row["label"] for row in roster]
    effective_domains = sorted(set(allowed_domains).intersection(set(agent.domains if agent else allowed_domains)))
    return {
        "room": "GoldenStackers Business Chat Room",
        "prompt_preview": str(prompt or "")[:160],
        "primary_agent": agent.label if agent else "Goldy (Auto Router)",
        "participants": participants,
        "effective_domains": effective_domains,
        "coordination_rules": [
            "Goldy coordinates routing and keeps human approval boundaries.",
            "Kurt owns inventory intake drafts and lot/cost evidence questions.",
            "Murdock owns listing drafts, eBay-safe descriptions, and readiness blockers.",
            f"{AI_ACCOUNTANT_NAME} owns accounting/tax evidence review and remains read-only.",
            "Write-capable agents may prepare proposed actions, but writes require approval-gated app workflows.",
        ],
    }
