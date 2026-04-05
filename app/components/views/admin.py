from datetime import datetime, timedelta
from pathlib import Path
from decimal import Decimal
from urllib.parse import urlparse
import json
import re
from collections import Counter
import zipfile
from io import BytesIO
from typing import Any

import pandas as pd
import streamlit as st
from sqlalchemy import delete, text
from sqlalchemy import select

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.auth import DEFAULT_PERMISSIONS, auth_debug_snapshot, current_user, ensure_permission
from app.components.views.shared import handoff_to_documents_draft, render_help_panel
from app.components.views.system_health import render_system_health
from app.config import settings
from app.db.migrate import downgrade as migrate_downgrade
from app.db.migrate import upgrade as migrate_upgrade
from app.db.models import (
    AIProviderConfig,
    AuditLog,
    CoinAIRun,
    DocumentTemplateProfile,
    IntegrationQueueJob,
    InventoryMovement,
    InventorySource,
    MarketplaceListing,
    MediaAsset,
    Order,
    OrderItem,
    Product,
    ProductLotAssignment,
    PurchaseLot,
    ReturnRecord,
    Sale,
    SavedFilterProfile,
    ShippingPreset,
)
from app.db.seed import seed_dev_data
from app.repository import InventoryRepository
from app.services.db_backup import (
    create_backup_dump,
    download_backup_from_s3,
    list_backups_in_s3,
    pg_tools_status,
    restore_dump_file,
    s3_backup_enabled,
    upload_backup_to_s3,
)
from app.services.config_health import health_state, required_env_keys, required_runtime_keys
from app.services.env_manager import (
    SENSITIVE_ENV_KEYS,
    ensure_env_defaults,
    is_editable_env_key,
    mask_env_value,
    read_process_env_values,
    read_env_file,
    upsert_env_key,
    uses_env_file,
)
from app.services.ebay import EbayClient
from app.services.coin_reference_sources import (
    resolve_paid_coin_source_adapter,
    resolve_paid_coin_source_config,
)
from app.services.grading_standards import (
    CURATED_COMP_BASELINE,
    CURATED_GRADING_BASELINE,
    build_coin_grading_rules_context_from_web,
    build_comp_rules_context_from_web,
    clear_standards_snapshot_cache,
    fetch_standards_snapshot,
)
from app.services.llm_runtime import (
    DEFAULT_COMP_INSTRUCTION,
    DEFAULT_COMP_SYSTEM_MESSAGE,
    LLMRuntimeConfig,
    fetch_available_models,
    validate_llm_runtime_config,
)
from app.services.sync_jobs import is_sync_job_enabled, sync_job_catalog
from app.services.runtime_settings import get_runtime_bool, get_runtime_int, get_runtime_str
from app.services.integration_automation import preview_rule_impact, simulate_rule_evaluation_for_job
from app.services.integration_queue import (
    process_due_google_queue_jobs,
    process_due_integration_queue_jobs,
    process_integration_queue_job,
)
from app.services.shipping_labels import purchase_shipping_label
from app.services.slack_notify import build_slack_alert_text, dispatch_slack_alert, send_slack_message
from app.components.views.tools import DEFAULT_COMP_DEALER_DOMAINS
from app.utils.time import utcnow_naive


def _audit_changes(row: AuditLog) -> dict:
    try:
        payload = json.loads(str(getattr(row, "changes_json", "") or "{}"))
    except Exception:
        payload = {}
    if isinstance(payload, dict):
        return payload
    return {}


def _all_permission_options() -> list[str]:
    options = set()
    for perms in DEFAULT_PERMISSIONS.values():
        options.update(perms)
    return sorted(options)


def _workspace_parity_specs() -> list[dict]:
    return [
        {
            "workflow": "eBay listing lifecycle",
            "legacy_surface": "Listings + eBay Ops",
            "unified_surface": "eBay Workspace",
            "required_permission": "bulk_update",
            "audit_entity_types": ["listing", "sync_run", "sync_error"],
            "audit_actions": ["update", "retry", "resolve_error"],
            "task_completion_workflows": ["ebay_workspace", "listings"],
        },
        {
            "workflow": "Shipping queue bulk updates",
            "legacy_surface": "Shipping",
            "unified_surface": "Fulfillment Ops (planned)",
            "required_permission": "bulk_update",
            "audit_entity_types": ["sale", "shipping_preset", "sync_run"],
            "audit_actions": ["update", "create"],
            "task_completion_workflows": ["shipping"],
        },
        {
            "workflow": "Sync failure triage/retry",
            "legacy_surface": "Sync",
            "unified_surface": "Sync Ops (planned)",
            "required_permission": "create",
            "audit_entity_types": ["sync_run", "sync_error"],
            "audit_actions": ["create", "update", "resolve_error"],
            "task_completion_workflows": ["sync"],
        },
        {
            "workflow": "Runtime/env configuration updates",
            "legacy_surface": "Admin tabs",
            "unified_surface": "Admin controls",
            "required_permission": "manage_settings",
            "audit_entity_types": ["runtime_setting", "app_user", "role_permission"],
            "audit_actions": ["create", "update", "delete"],
            "task_completion_workflows": [],
        },
        {
            "workflow": "AI-assisted operational tools",
            "legacy_surface": "Tools",
            "unified_surface": "Workspace-integrated tools",
            "required_permission": "ai_comp_use",
            "audit_entity_types": ["coin_ai_run", "workspace_feedback", "navigation"],
            "audit_actions": ["create", "submit", "page_view"],
            "task_completion_workflows": ["operations_home"],
        },
    ]


def _get_current_db_revision(repo: InventoryRepository) -> str:
    try:
        value = repo.db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        return str(value)
    except Exception:
        return "unknown (alembic_version not found)"


def _migration_history_rows() -> list[dict[str, str]]:
    project_root = Path(__file__).resolve().parents[3]
    cfg = Config(str(project_root / "alembic.ini"))
    script = ScriptDirectory.from_config(cfg)
    head_revision = script.get_current_head()
    rows: list[dict[str, str]] = []
    for rev in script.walk_revisions(base="base", head="heads"):
        down = rev.down_revision
        if isinstance(down, tuple):
            down_label = ", ".join(str(item) for item in down)
        else:
            down_label = str(down or "")
        rows.append(
            {
                "revision": rev.revision,
                "down_revision": down_label,
                "message": rev.doc or "",
                "is_head": "yes" if rev.revision == head_revision else "",
            }
        )
    return rows


def _seed_mode_label(mode: str) -> str:
    if mode == "append_only":
        return "Append seed data (no wipe)"
    if mode == "wipe_seed_tables_then_seed":
        return "Wipe seed tables then seed"
    return "Wipe operational data then seed (empty-db style)"


def _mask_secret(value: str, visible: int = 4) -> str:
    clean = (value or "").strip()
    if not clean:
        return "(not set)"
    if len(clean) <= visible:
        return "*" * len(clean)
    return f"{'*' * max(3, len(clean) - visible)}{clean[-visible:]}"


def _normalize_comp_dealer_domains_csv(value: str) -> tuple[str, list[str]]:
    tokens = str(value or "").replace("\n", ",").split(",")
    out: list[str] = []
    for token in tokens:
        clean = token.strip().lower()
        if not clean:
            continue
        if clean.startswith("https://") or clean.startswith("http://"):
            clean = (urlparse(clean).netloc or clean).lower()
        clean = clean.lstrip("www.")
        if not clean:
            continue
        if "/" in clean:
            clean = clean.split("/")[0].strip()
        if "." not in clean:
            continue
        if clean not in out:
            out.append(clean)
    return ",".join(out), out


def _runtime_setting_seed_defaults() -> list[dict[str, str]]:
    return [
        {
            "key": "app_build_version",
            "value": settings.app_build_version,
            "value_type": "str",
            "description": "Application build version identifier (for deployment traceability).",
        },
        {
            "key": "app_build_sha",
            "value": settings.app_build_sha,
            "value_type": "str",
            "description": "Application build git SHA (for deployment traceability).",
        },
        {
            "key": "comp_web_fallback_enabled",
            "value": "true" if settings.comp_web_fallback_enabled else "false",
            "value_type": "bool",
            "description": "Default web fallback behavior for Comp Tool when eBay comps are empty.",
        },
        {
            "key": "ebay_allow_sandbox_seller_ops",
            "value": "true" if settings.ebay_allow_sandbox_seller_ops else "false",
            "value_type": "bool",
            "description": "Allow seller operations in sandbox environment.",
        },
        {
            "key": "ebay_require_runbook_for_bulk_ops",
            "value": "false",
            "value_type": "bool",
            "description": "Require eBay Workspace runbook completion before bulk eBay Ops actions are enabled.",
        },
        {
            "key": "ebay_marketplace_id",
            "value": settings.ebay_marketplace_id,
            "value_type": "str",
            "description": "Default marketplace ID for eBay operations.",
        },
        {
            "key": "ebay_currency",
            "value": settings.ebay_currency,
            "value_type": "str",
            "description": "Default eBay listing currency.",
        },
        {
            "key": "ebay_content_language",
            "value": settings.ebay_content_language,
            "value_type": "str",
            "description": "Default eBay content language.",
        },
        {
            "key": "ebay_merchant_location_key",
            "value": settings.ebay_merchant_location_key,
            "value_type": "str",
            "description": "Default eBay merchant location key.",
        },
        {
            "key": "ebay_payment_policy_id",
            "value": settings.ebay_payment_policy_id,
            "value_type": "str",
            "description": "Default eBay payment policy ID.",
        },
        {
            "key": "ebay_fulfillment_policy_id",
            "value": settings.ebay_fulfillment_policy_id,
            "value_type": "str",
            "description": "Default eBay fulfillment policy ID.",
        },
        {
            "key": "ebay_return_policy_id",
            "value": settings.ebay_return_policy_id,
            "value_type": "str",
            "description": "Default eBay return policy ID.",
        },
        {
            "key": "ebay_category_id",
            "value": "",
            "value_type": "str",
            "description": "Default eBay category ID used by workspace/listing publish defaults.",
        },
        {
            "key": "ebay_listing_format_default",
            "value": "FIXED_PRICE",
            "value_type": "str",
            "description": "Default eBay listing format (`FIXED_PRICE` or `AUCTION`).",
        },
        {
            "key": "ebay_best_offer_default",
            "value": "false",
            "value_type": "bool",
            "description": "Default Best Offer toggle for fixed-price eBay listings.",
        },
        {
            "key": "ebay_auction_duration_default",
            "value": "DAYS_7",
            "value_type": "str",
            "description": "Default auction duration for eBay listing workflows.",
        },
        {
            "key": "ebay_auction_start_default",
            "value": "1.0",
            "value_type": "float",
            "description": "Default auction start price for eBay listing workflows.",
        },
        {
            "key": "ebay_auction_reserve_default",
            "value": "0.0",
            "value_type": "float",
            "description": "Default auction reserve price for eBay listing workflows.",
        },
        {
            "key": "ebay_auction_buy_now_default",
            "value": "0.0",
            "value_type": "float",
            "description": "Default auction Buy It Now price for eBay listing workflows.",
        },
        {
            "key": "ebay_workspace_store_profiles_json",
            "value": "{}",
            "value_type": "str",
            "description": "Persisted eBay workspace store/policy/listing-format profiles.",
        },
        {
            "key": "ebay_workspace_default_store_profile",
            "value": "",
            "value_type": "str",
            "description": "Default eBay workspace store profile alias loaded on workspace start.",
        },
        {
            "key": "ebay_user_access_token",
            "value": settings.ebay_user_access_token,
            "value_type": "str",
            "description": "Default eBay user access token used in forms.",
        },
        {
            "key": "spot_price_provider",
            "value": settings.spot_price_provider,
            "value_type": "str",
            "description": "Spot provider (`yahoo_finance` or `metals_api`).",
        },
        {
            "key": "metals_api_base_url",
            "value": settings.metals_api_base_url,
            "value_type": "str",
            "description": "Metals API base URL runtime override.",
        },
        {
            "key": "metals_api_key",
            "value": settings.metals_api_key,
            "value_type": "str",
            "description": "Metals API key runtime override.",
        },
        {
            "key": "yahoo_finance_base_url",
            "value": settings.yahoo_finance_base_url,
            "value_type": "str",
            "description": "Yahoo chart base URL runtime override.",
        },
        {
            "key": "yahoo_symbol_gold",
            "value": settings.yahoo_symbol_gold,
            "value_type": "str",
            "description": "Yahoo symbol for gold spot.",
        },
        {
            "key": "yahoo_symbol_silver",
            "value": settings.yahoo_symbol_silver,
            "value_type": "str",
            "description": "Yahoo symbol for silver spot.",
        },
        {
            "key": "yahoo_symbol_platinum",
            "value": settings.yahoo_symbol_platinum,
            "value_type": "str",
            "description": "Yahoo symbol for platinum spot.",
        },
        {
            "key": "sync_job_ebay_orders_pull_import_enabled",
            "value": "true" if settings.sync_job_ebay_orders_pull_import_enabled else "false",
            "value_type": "bool",
            "description": "Enable/disable eBay order pull/import job.",
        },
        {
            "key": "sync_job_ebay_orders_pull_import_limit",
            "value": str(settings.sync_job_ebay_orders_pull_import_limit),
            "value_type": "int",
            "description": "Default limit for eBay pull/import worker runs.",
        },
        {
            "key": "sync_job_ebay_orders_pull_import_offset",
            "value": str(settings.sync_job_ebay_orders_pull_import_offset),
            "value_type": "int",
            "description": "Default offset for eBay pull/import worker runs.",
        },
        {
            "key": "sync_job_ebay_shipping_tracking_push_enabled",
            "value": "true" if settings.sync_job_ebay_shipping_tracking_push_enabled else "false",
            "value_type": "bool",
            "description": "Enable/disable eBay tracking push job.",
        },
        {
            "key": "sync_job_quickbooks_export_enabled",
            "value": "true" if settings.sync_job_quickbooks_export_enabled else "false",
            "value_type": "bool",
            "description": "Enable/disable QuickBooks export job scaffold.",
        },
        {
            "key": "sync_job_shopify_orders_pull_enabled",
            "value": "true" if settings.sync_job_shopify_orders_pull_enabled else "false",
            "value_type": "bool",
            "description": "Enable/disable Shopify pull job scaffold.",
        },
        {
            "key": "sync_job_shopify_orders_pull_shop_domain",
            "value": settings.sync_job_shopify_orders_pull_shop_domain,
            "value_type": "str",
            "description": "Default Shopify shop domain for pull jobs (my-shop.myshopify.com).",
        },
        {
            "key": "sync_job_shopify_orders_pull_access_token",
            "value": settings.sync_job_shopify_orders_pull_access_token,
            "value_type": "str",
            "description": "Default Shopify Admin API access token for pull jobs.",
        },
        {
            "key": "sync_job_shopify_orders_pull_limit",
            "value": str(settings.sync_job_shopify_orders_pull_limit),
            "value_type": "int",
            "description": "Default fetch limit for Shopify pull jobs.",
        },
        {
            "key": "sync_job_shopify_orders_pull_offset",
            "value": str(settings.sync_job_shopify_orders_pull_offset),
            "value_type": "int",
            "description": "Default fetch offset for Shopify pull jobs.",
        },
        {
            "key": "governance_snapshot_runner_enabled",
            "value": "false",
            "value_type": "bool",
            "description": "Enable/disable scheduled governance snapshot creation in sync runner.",
        },
        {
            "key": "governance_snapshot_interval_hours",
            "value": "24",
            "value_type": "int",
            "description": "Minimum hours between sync-runner governance snapshot events.",
        },
        {
            "key": "governance_snapshot_lookback_days",
            "value": "30",
            "value_type": "int",
            "description": "Lookback window for scheduled governance snapshot event counts.",
        },
        {
            "key": "governance_snapshot_max_rows_per_scope",
            "value": "2000",
            "value_type": "int",
            "description": "Max rows per governance scope sampled in scheduled snapshots.",
        },
        {
            "key": "backup_policy_enabled",
            "value": "false",
            "value_type": "bool",
            "description": "Enable scheduled backup policy reporting/tracking for this environment.",
        },
        {
            "key": "backup_policy_cadence_hours",
            "value": "24",
            "value_type": "int",
            "description": "Expected backup cadence in hours for compliance and readiness checks.",
        },
        {
            "key": "backup_policy_retention_days",
            "value": "30",
            "value_type": "int",
            "description": "Expected backup retention window in days.",
        },
        {
            "key": "backup_policy_upload_to_s3",
            "value": "true",
            "value_type": "bool",
            "description": "Whether backups should be uploaded to S3 by policy.",
        },
        {
            "key": "backup_restore_drill_interval_days",
            "value": "30",
            "value_type": "int",
            "description": "Maximum target days between successful restore drills.",
        },
        {
            "key": "backup_restore_rto_target_minutes",
            "value": "60",
            "value_type": "int",
            "description": "Target restore recovery-time objective (minutes) used for drill evidence.",
        },
        {
            "key": "backup_policy_owner",
            "value": "",
            "value_type": "str",
            "description": "Primary owner/team accountable for backup policy and drill execution.",
        },
        {
            "key": "comp_llm_system_message",
            "value": DEFAULT_COMP_SYSTEM_MESSAGE,
            "value_type": "str",
            "description": "System message for AI comp synthesis prompts.",
        },
        {
            "key": "comp_llm_instruction_template",
            "value": DEFAULT_COMP_INSTRUCTION,
            "value_type": "str",
            "description": "Instruction template for AI comp synthesis prompts.",
        },
        {
            "key": "comp_reference_rules_context",
            "value": (
                "For comp analysis, prioritize sold comparables and clearly separate certified vs raw coins. "
                "When certified comps are present, compare within same grading service tier (PCGS/NGC/ANACS/ICG) "
                "and nearby grade bands; avoid mixing unlike grade populations without an explicit adjustment note. "
                "Call out when outliers, altered/cleaned coins, or weak title matches may distort pricing."
            ),
            "value_type": "str",
            "description": "Supplemental grading/comps rule context appended to comp prompts.",
        },
        {
            "key": "coin_grading_rules_context",
            "value": (
                "Use major third-party grading standards as reference context (PCGS, NGC, ANACS, ICG). "
                "Evaluate wear/friction, luster, strike quality, surface preservation, eye appeal, toning, "
                "and cleaning/damage indicators. Grade conservatively when uncertain."
            ),
            "value_type": "str",
            "description": "Supplemental grading standards context appended to grader prompts.",
        },
        {
            "key": "comp_web_fallback_limit",
            "value": "20",
            "value_type": "int",
            "description": "Default max web fallback result rows evaluated in Comp Tool.",
        },
        {
            "key": "comp_web_detail_fetch_limit",
            "value": "20",
            "value_type": "int",
            "description": "Default max web fallback links opened for detailed on-page price extraction.",
        },
        {
            "key": "documents_handoff_governance_review_mode",
            "value": "false",
            "value_type": "bool",
            "description": "When true, Admin governance clear-audit and preset-audit share one date preset/range.",
        },
        {
            "key": "listing_review_two_person_required",
            "value": "false",
            "value_type": "bool",
            "description": "Require a different user than reviewer when setting listing to active on configured channels.",
        },
        {
            "key": "listing_review_two_person_channels_csv",
            "value": "ebay",
            "value_type": "str",
            "description": "Comma-separated marketplaces where two-person review policy applies.",
        },
        {
            "key": "comp_dealer_domains_csv",
            "value": ",".join(DEFAULT_COMP_DEALER_DOMAINS),
            "value_type": "str",
            "description": "Comma-separated dealer domains used for comp parser/domain weighting.",
        },
        {
            "key": "coin_ref_paid_source_enabled",
            "value": "false",
            "value_type": "bool",
            "description": "Enable optional paid coin-reference source adapter contract (disabled by default).",
        },
        {
            "key": "coin_ref_paid_source_provider",
            "value": "none",
            "value_type": "str",
            "description": "Paid source provider key (`none`, `greysheet`).",
        },
        {
            "key": "coin_ref_paid_source_base_url",
            "value": "",
            "value_type": "str",
            "description": "Paid source API base URL (if licensed/in use).",
        },
        {
            "key": "coin_ref_paid_source_api_key",
            "value": "",
            "value_type": "str",
            "description": "Paid source API key/token (if licensed/in use).",
        },
        {
            "key": "coin_ref_paid_source_license_ack",
            "value": "false",
            "value_type": "bool",
            "description": "Set true only after legal/licensing approval for paid source usage.",
        },
        {
            "key": "coin_ref_paid_source_allow_prod",
            "value": "false",
            "value_type": "bool",
            "description": "Allow paid source usage in production environment (separate guardrail).",
        },
        {
            "key": "ai_voice_enabled",
            "value": "false",
            "value_type": "bool",
            "description": "Enable/disable voice features in AI chat/copilot surfaces.",
        },
        {
            "key": "ai_voice_stt_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable microphone speech-to-text prompt capture in AI chat.",
        },
        {
            "key": "ai_voice_tts_enabled",
            "value": "false",
            "value_type": "bool",
            "description": "Enable text-to-speech playback for AI responses.",
        },
        {
            "key": "ai_voice_provider",
            "value": "openai",
            "value_type": "str",
            "description": "Voice provider identifier (`openai` or `localai`).",
        },
        {
            "key": "ai_voice_base_url",
            "value": (settings.comp_llm_base_url or "https://api.openai.com/v1").strip().rstrip("/"),
            "value_type": "str",
            "description": "Voice provider base URL.",
        },
        {
            "key": "ai_voice_api_key",
            "value": settings.openai_api_key or "",
            "value_type": "str",
            "description": "Voice provider API key/token.",
        },
        {
            "key": "ai_voice_stt_model",
            "value": "gpt-4o-mini-transcribe",
            "value_type": "str",
            "description": "Speech-to-text model id.",
        },
        {
            "key": "ai_voice_stt_language",
            "value": "",
            "value_type": "str",
            "description": "Optional speech-to-text language hint (for example `en`).",
        },
        {
            "key": "ai_voice_tts_model",
            "value": "gpt-4o-mini-tts",
            "value_type": "str",
            "description": "Text-to-speech model id.",
        },
        {
            "key": "ai_voice_tts_voice",
            "value": "alloy",
            "value_type": "str",
            "description": "Text-to-speech voice id.",
        },
        {
            "key": "ai_voice_tts_response_format",
            "value": "mp3",
            "value_type": "str",
            "description": "Text-to-speech response format (`mp3` or `wav`).",
        },
        {
            "key": "ai_voice_timeout_seconds",
            "value": "45",
            "value_type": "int",
            "description": "Voice API timeout seconds.",
        },
        {
            "key": "ai_voice_tts_max_chars",
            "value": "1400",
            "value_type": "int",
            "description": "Maximum response chars to synthesize for one TTS call.",
        },
        {
            "key": "ai_domain_chat_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable/disable Ask GoldenStackers chat.",
        },
        {
            "key": "ai_domain_comp_tool_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable/disable Comp Tool features.",
        },
        {
            "key": "ai_domain_coin_grader_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable/disable Coin Grader features.",
        },
        {
            "key": "ai_domain_coin_identifier_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable/disable Coin Identifier features.",
        },
        {
            "key": "chat_ai_refine_enabled",
            "value": "false",
            "value_type": "bool",
            "description": "Enable/disable orchestration-backed AI refinement pass for Ask GoldenStackers responses.",
        },
        {
            "key": "chat_ai_refine_system_message",
            "value": (
                "You are GoldenStackers' read-only operations copilot. "
                "Preserve factual values from the provided draft answer and citations."
            ),
            "value_type": "str",
            "description": "System message used for Ask GoldenStackers AI refinement pass.",
        },
        {
            "key": "chat_ai_refine_instruction",
            "value": (
                "Rewrite the draft answer for clarity and operator usefulness. "
                "Do not invent values. Keep output concise markdown with short bullets."
            ),
            "value_type": "str",
            "description": "Instruction template used for Ask GoldenStackers AI refinement pass.",
        },
        {
            "key": "chat_mask_sensitive_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Master toggle for masking sensitive values in Ask GoldenStackers responses.",
        },
        {
            "key": "chat_mask_email_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Mask email addresses in Ask GoldenStackers responses.",
        },
        {
            "key": "chat_mask_phone_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Mask phone numbers in Ask GoldenStackers responses.",
        },
        {
            "key": "chat_mask_tracking_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Mask tracking numbers in Ask GoldenStackers responses.",
        },
        {
            "key": "ai_fallback_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable/disable AI runtime fallback across active provider profiles.",
        },
        {
            "key": "ai_fallback_max_profiles",
            "value": "3",
            "value_type": "int",
            "description": "Maximum number of active AI runtime profiles to attempt per request.",
        },
        {
            "key": "google_integration_enabled",
            "value": "false",
            "value_type": "bool",
            "description": "Master toggle for Google Workspace integration features (Gmail/Calendar/Drive).",
        },
        {
            "key": "google_oauth_client_id",
            "value": "",
            "value_type": "str",
            "description": "Google OAuth client ID for this environment.",
        },
        {
            "key": "google_oauth_client_secret",
            "value": "",
            "value_type": "str",
            "description": "Google OAuth client secret for this environment.",
        },
        {
            "key": "google_oauth_redirect_uri",
            "value": "",
            "value_type": "str",
            "description": "Google OAuth redirect URI for this environment.",
        },
        {
            "key": "google_workspace_scopes_csv",
            "value": "https://www.googleapis.com/auth/gmail.send,https://www.googleapis.com/auth/calendar.events,https://www.googleapis.com/auth/drive.file",
            "value_type": "str",
            "description": "Comma-separated Google OAuth scopes requested by the app.",
        },
        {
            "key": "google_oauth_access_token",
            "value": "",
            "value_type": "str",
            "description": "Google OAuth access token for API calls (runtime-managed credential).",
        },
        {
            "key": "google_oauth_refresh_token",
            "value": "",
            "value_type": "str",
            "description": "Google OAuth refresh token for future token refresh flow.",
        },
        {
            "key": "google_default_sender_email",
            "value": "sales@goldenstackers.com",
            "value_type": "str",
            "description": "Default sender email used for Gmail invoice/receipt workflows.",
        },
        {
            "key": "google_drive_root_folder_id",
            "value": "",
            "value_type": "str",
            "description": "Optional default Google Drive folder ID for exports/uploads.",
        },
        {
            "key": "google_default_calendar_id",
            "value": "primary",
            "value_type": "str",
            "description": "Default Google Calendar ID for follow-up event creation.",
        },
        {
            "key": "google_default_timezone",
            "value": "America/Denver",
            "value_type": "str",
            "description": "Default timezone for Google Calendar event scheduling.",
        },
        {
            "key": "google_http_timeout_seconds",
            "value": "30",
            "value_type": "int",
            "description": "Timeout for Google API HTTP requests.",
        },
        {
            "key": "google_queue_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable/disable Google integration retry queue for failed actions.",
        },
        {
            "key": "google_queue_max_retries",
            "value": "5",
            "value_type": "int",
            "description": "Maximum retry attempts per queued Google integration action.",
        },
        {
            "key": "google_queue_backoff_base_seconds",
            "value": "120",
            "value_type": "int",
            "description": "Base backoff seconds for exponential retry scheduling.",
        },
        {
            "key": "google_queue_backoff_max_seconds",
            "value": "3600",
            "value_type": "int",
            "description": "Maximum backoff seconds for queued retries.",
        },
        {
            "key": "shipping_queue_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable/disable shipping integration retry queue execution.",
        },
        {
            "key": "shipping_queue_max_retries",
            "value": "5",
            "value_type": "int",
            "description": "Default max retries for queued shipping label purchase jobs.",
        },
        {
            "key": "shipping_queue_backoff_base_seconds",
            "value": "60",
            "value_type": "int",
            "description": "Base backoff seconds for shipping queue retry scheduling.",
        },
        {
            "key": "shipping_queue_backoff_max_seconds",
            "value": "3600",
            "value_type": "int",
            "description": "Maximum backoff seconds for shipping queue retries.",
        },
        {
            "key": "shipping_label_purchase_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable/disable shipping label purchase queue actions.",
        },
        {
            "key": "shipping_label_live_provider_calls_enabled",
            "value": "false",
            "value_type": "bool",
            "description": "Guardrail toggle for live external label-purchase API calls.",
        },
        {
            "key": "shipping_label_provider_pirateship_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable/disable Pirate Ship as a shipping label provider.",
        },
        {
            "key": "shipping_label_provider_ebay_shipping_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable/disable eBay Shipping as a shipping label provider.",
        },
        {
            "key": "shipping_label_provider_usps_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable/disable USPS as a shipping label provider.",
        },
        {
            "key": "shipping_label_provider_ups_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable/disable UPS as a shipping label provider.",
        },
        {
            "key": "shipping_label_provider_fedex_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable/disable FedEx as a shipping label provider.",
        },
        {
            "key": "shipping_label_provider_other_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable/disable generic/other shipping label provider jobs.",
        },
        {
            "key": "shipping_label_pirateship_mode",
            "value": "mock",
            "value_type": "str",
            "description": "Pirate Ship adapter mode (`mock` or `api`) for live-provider execution path.",
        },
        {
            "key": "shipping_label_pirateship_base_url",
            "value": "",
            "value_type": "str",
            "description": "Pirate Ship adapter base URL for API mode.",
        },
        {
            "key": "shipping_label_pirateship_api_key",
            "value": "",
            "value_type": "str",
            "description": "Pirate Ship adapter API key/token for API mode.",
        },
        {
            "key": "shipping_label_pirateship_endpoint_path",
            "value": "/v1/labels/purchase",
            "value_type": "str",
            "description": "Pirate Ship adapter endpoint path (joined with base URL).",
        },
        {
            "key": "shipping_label_pirateship_auth_scheme",
            "value": "bearer",
            "value_type": "str",
            "description": "Pirate Ship auth scheme (`bearer` or `token`).",
        },
        {
            "key": "shipping_label_pirateship_timeout_seconds",
            "value": "20",
            "value_type": "int",
            "description": "Pirate Ship API timeout seconds for live mode.",
        },
        {
            "key": "invoicing_tax_jurisdiction",
            "value": "Golden, Colorado",
            "value_type": "str",
            "description": "Default jurisdiction label for invoice/receipt tax display.",
        },
        {
            "key": "invoicing_tax_rate_percent_default",
            "value": "8.81",
            "value_type": "str",
            "description": "Default sales-tax rate percent used by Documents tax calculator.",
        },
        {
            "key": "invoicing_tax_shipping_taxable_default",
            "value": "false",
            "value_type": "bool",
            "description": "Default toggle for whether shipping is taxable in Documents tax calculator.",
        },
        {
            "key": "invoicing_tax_exempt_categories_csv",
            "value": "bullion,coins",
            "value_type": "str",
            "description": "Comma-separated product categories treated as tax-exempt in auto tax mode.",
        },
        {
            "key": "slack_notifications_enabled",
            "value": "false",
            "value_type": "bool",
            "description": "Master toggle for Slack notifications.",
        },
        {
            "key": "slack_bot_token",
            "value": "",
            "value_type": "str",
            "description": "Slack Bot OAuth token used for posting notifications.",
        },
        {
            "key": "slack_signing_secret",
            "value": "",
            "value_type": "str",
            "description": "Slack signing secret for future interactive/event verification.",
        },
        {
            "key": "slack_default_channel",
            "value": "",
            "value_type": "str",
            "description": "Default Slack channel for operational notifications (for example #ops-alerts).",
        },
        {
            "key": "slack_notify_sync_failures",
            "value": "true",
            "value_type": "bool",
            "description": "Send Slack notifications for sync failures/partial runs.",
        },
        {
            "key": "slack_notify_shipping_exceptions",
            "value": "true",
            "value_type": "bool",
            "description": "Send Slack notifications for shipping exceptions.",
        },
        {
            "key": "slack_notify_daily_summary",
            "value": "false",
            "value_type": "bool",
            "description": "Send one daily Slack operational summary message.",
        },
        {
            "key": "slack_notify_google_queue_failures",
            "value": "true",
            "value_type": "bool",
            "description": "Send Slack notifications when Google integration queue jobs hit terminal failure.",
        },
        {
            "key": "slack_notify_integration_queue_failures",
            "value": "true",
            "value_type": "bool",
            "description": "Send Slack notifications when any integration queue job hits terminal failure.",
        },
        {
            "key": "slack_notify_parity_decisions",
            "value": "false",
            "value_type": "bool",
            "description": "Send Slack notifications when workspace parity release decisions are recorded.",
        },
        {
            "key": "slack_notify_followup_overdue",
            "value": "false",
            "value_type": "bool",
            "description": "Allow sending Slack notifications for overdue workspace rollout follow-up tasks.",
        },
        {
            "key": "slack_notify_system_health_critical",
            "value": "false",
            "value_type": "bool",
            "description": "Send Slack notifications when System Health critical-signal thresholds are breached.",
        },
        {
            "key": "slack_daily_summary_cron",
            "value": "0 16 * * *",
            "value_type": "str",
            "description": "Cron expression for daily summary schedule (UTC).",
        },
        {
            "key": "slack_http_timeout_seconds",
            "value": "15",
            "value_type": "int",
            "description": "Timeout for Slack API requests.",
        },
        {
            "key": "slack_queue_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable/disable Slack delivery retry queue on post failures.",
        },
        {
            "key": "slack_queue_max_retries",
            "value": "5",
            "value_type": "int",
            "description": "Maximum retry attempts per queued Slack delivery.",
        },
        {
            "key": "slack_queue_backoff_base_seconds",
            "value": "60",
            "value_type": "int",
            "description": "Base backoff seconds for Slack retry queue scheduling.",
        },
        {
            "key": "slack_queue_backoff_max_seconds",
            "value": "3600",
            "value_type": "int",
            "description": "Maximum backoff seconds for Slack retry queue scheduling.",
        },
        {
            "key": "integration_automation_dry_run_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "When true, automation rules are evaluated/logged but rule effects are not persisted.",
        },
        {
            "key": "integration_automation_execute_approval_required_enabled",
            "value": "false",
            "value_type": "bool",
            "description": "When true, rules marked requires_approval may auto-apply in execution engine.",
        },
        {
            "key": "health_queue_execute_exceptions_warn_24h",
            "value": "1",
            "value_type": "int",
            "description": "System Health warning threshold (24h) for queue execute exceptions.",
        },
        {
            "key": "health_queue_execute_exceptions_critical_24h",
            "value": "5",
            "value_type": "int",
            "description": "System Health critical threshold (24h) for queue execute exceptions.",
        },
        {
            "key": "health_terminal_queue_failures_warn_24h",
            "value": "1",
            "value_type": "int",
            "description": "System Health warning threshold (24h) for terminal integration queue failures.",
        },
        {
            "key": "health_terminal_queue_failures_critical_24h",
            "value": "3",
            "value_type": "int",
            "description": "System Health critical threshold (24h) for terminal integration queue failures.",
        },
        {
            "key": "health_integration_warnings_warn_24h",
            "value": "10",
            "value_type": "int",
            "description": "System Health warning threshold (24h) for integration warning events.",
        },
        {
            "key": "health_integration_warnings_critical_24h",
            "value": "30",
            "value_type": "int",
            "description": "System Health critical threshold (24h) for integration warning events.",
        },
        {
            "key": "runbook_queue_execute_exceptions_url",
            "value": "",
            "value_type": "str",
            "description": "Runbook URL for queue execute exception remediation.",
        },
        {
            "key": "runbook_terminal_queue_failures_url",
            "value": "",
            "value_type": "str",
            "description": "Runbook URL for terminal queue failure remediation.",
        },
        {
            "key": "runbook_integration_warnings_url",
            "value": "",
            "value_type": "str",
            "description": "Runbook URL for elevated integration warning remediation.",
        },
        {
            "key": "go_live_readiness_weight_checklist_gap_pct",
            "value": "40",
            "value_type": "int",
            "description": "Weight (percent) applied to checklist completion gap in go-live readiness score.",
        },
        {
            "key": "go_live_readiness_weight_env_missing",
            "value": "5",
            "value_type": "int",
            "description": "Per-key penalty for missing required environment keys in readiness score.",
        },
        {
            "key": "go_live_readiness_weight_runtime_missing",
            "value": "5",
            "value_type": "int",
            "description": "Per-key penalty for missing/inactive required runtime keys in readiness score.",
        },
        {
            "key": "go_live_readiness_weight_terminal_queue_failure",
            "value": "10",
            "value_type": "int",
            "description": "Per-failure penalty for terminal queue failures (24h), capped by max setting.",
        },
        {
            "key": "go_live_readiness_weight_queue_execute_exception",
            "value": "5",
            "value_type": "int",
            "description": "Per-exception penalty for queue execute exceptions (24h), capped by max setting.",
        },
        {
            "key": "go_live_readiness_penalty_terminal_queue_failure_max",
            "value": "30",
            "value_type": "int",
            "description": "Maximum total penalty applied for terminal queue failures.",
        },
        {
            "key": "go_live_readiness_penalty_queue_execute_exception_max",
            "value": "20",
            "value_type": "int",
            "description": "Maximum total penalty applied for queue execute exceptions.",
        },
        {
            "key": "go_live_readiness_penalty_integration_warnings_warn",
            "value": "10",
            "value_type": "int",
            "description": "Penalty applied when 24h integration warnings exceed warn threshold.",
        },
        {
            "key": "go_live_readiness_penalty_integration_warnings_critical",
            "value": "20",
            "value_type": "int",
            "description": "Penalty applied when 24h integration warnings exceed critical threshold.",
        },
        {
            "key": "go_live_readiness_threshold_green",
            "value": "85",
            "value_type": "int",
            "description": "Minimum readiness score for GREEN state.",
        },
        {
            "key": "go_live_readiness_threshold_yellow",
            "value": "65",
            "value_type": "int",
            "description": "Minimum readiness score for YELLOW state (below this is RED).",
        },
        {
            "key": "health_auto_alert_critical_enabled",
            "value": "false",
            "value_type": "bool",
            "description": "Enable automatic System Health critical-signal alert dispatch.",
        },
        {
            "key": "health_auto_alert_cooldown_minutes",
            "value": "60",
            "value_type": "int",
            "description": "Cooldown minutes before repeating identical System Health critical alerts.",
        },
        {
            "key": "slack_channel_sync_failures",
            "value": "",
            "value_type": "str",
            "description": "Optional channel override for sync failure alerts.",
        },
        {
            "key": "slack_channel_google_queue_failures",
            "value": "",
            "value_type": "str",
            "description": "Optional channel override for Google queue failure alerts.",
        },
        {
            "key": "slack_channel_integration_queue_failures",
            "value": "",
            "value_type": "str",
            "description": "Optional channel override for generic integration queue failure alerts.",
        },
        {
            "key": "slack_channel_parity_decision",
            "value": "",
            "value_type": "str",
            "description": "Optional channel override for parity release decision alerts.",
        },
        {
            "key": "slack_channel_followup_overdue",
            "value": "",
            "value_type": "str",
            "description": "Optional channel override for overdue rollout follow-up alerts.",
        },
        {
            "key": "slack_channel_warning",
            "value": "",
            "value_type": "str",
            "description": "Optional channel override for warning-severity alerts.",
        },
        {
            "key": "slack_channel_error",
            "value": "",
            "value_type": "str",
            "description": "Optional channel override for error-severity alerts.",
        },
        {
            "key": "slack_channel_critical",
            "value": "",
            "value_type": "str",
            "description": "Optional channel override for critical-severity alerts.",
        },
        {
            "key": "slack_channel_system_health_critical",
            "value": "",
            "value_type": "str",
            "description": "Optional channel override for System Health critical alerts.",
        },
        {
            "key": "slack_template_sync_failures",
            "value": (
                ":warning: *GoldenStackers* sync run `{job_name}` `{status}`\n"
                "- Env: `{env}`\n"
                "- Run: `#{run_id}`\n"
                "- Processed: `{processed}`\n"
                "- Failed: `{failed}`\n"
                "- Actor: `{actor}`"
            ),
            "value_type": "str",
            "description": "Template for sync failure/partial Slack alerts.",
        },
        {
            "key": "slack_template_google_queue_failures",
            "value": (
                ":warning: *GoldenStackers* Google queue job failed permanently\n"
                "- Env: `{env}`\n"
                "- Job: `#{job_id}` `{action}`\n"
                "- Retries: `{retry_count}/{max_retries}`\n"
                "- Error: `{error}`"
            ),
            "value_type": "str",
            "description": "Template for terminal Google queue failure Slack alerts.",
        },
        {
            "key": "slack_template_integration_queue_failures",
            "value": (
                ":warning: *GoldenStackers* integration queue job failed permanently\n"
                "- Env: `{env}`\n"
                "- Integration: `{integration}`\n"
                "- Job: `#{job_id}` `{action}`\n"
                "- Retries: `{retry_count}/{max_retries}`\n"
                "- Error: `{error}`"
            ),
            "value_type": "str",
            "description": "Template for terminal integration queue failure Slack alerts.",
        },
        {
            "key": "slack_template_parity_decision",
            "value": (
                ":clipboard: *GoldenStackers* parity release decision `{decision}`\n"
                "- Env: `{env}`\n"
                "- Snapshot: `#{snapshot_id}`\n"
                "- Actor: `{actor}`\n"
                "- Note: `{note}`"
            ),
            "value_type": "str",
            "description": "Template for workspace parity release decision alerts.",
        },
        {
            "key": "slack_template_followup_overdue",
            "value": (
                ":rotating_light: *GoldenStackers* rollout follow-up overdue\n"
                "- Env: `{env}`\n"
                "- Task: `{task_key}`\n"
                "- Title: `{title}`\n"
                "- Owner: `{owner}`\n"
                "- Due: `{due_date}`\n"
                "- Priority: `{priority}`"
            ),
            "value_type": "str",
            "description": "Template for overdue workspace rollout follow-up alerts.",
        },
        {
            "key": "slack_template_system_health_critical",
            "value": (
                ":rotating_light: *GoldenStackers* System Health critical signals detected\n"
                "- Env: `{env}`\n"
                "- Critical Signals: `{critical_signals}`\n"
                "- Queue Execute Exceptions: `{queue_execute_exceptions}`\n"
                "- Terminal Queue Failures: `{terminal_queue_failures}`\n"
                "- Integration Warnings: `{integration_warnings}`"
            ),
            "value_type": "str",
            "description": "Template for System Health critical threshold alerts.",
        },
        {
            "key": "ux_workspace_ebay_enabled",
            "value": "true" if settings.ux_workspace_ebay_enabled else "false",
            "value_type": "bool",
            "description": "Enable consolidated eBay Workspace UX controls and defaults.",
        },
        {
            "key": "ux_workspace_inventory_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable Inventory workspace grouping/navigation surfaces.",
        },
        {
            "key": "ux_workspace_fulfillment_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable Fulfillment workspace grouping/navigation surfaces.",
        },
        {
            "key": "ux_workspace_sync_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable Sync workspace grouping/navigation surfaces.",
        },
        {
            "key": "ux_workspace_revenue_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable Revenue workspace grouping/navigation surfaces.",
        },
        {
            "key": "ux_listings_auto_photo_comp_review_preset",
            "value": "false",
            "value_type": "bool",
            "description": "Auto-apply Listings Photo-Comp Review Queue preset once per user session.",
        },
        {
            "key": "ux_navigation_mode",
            "value": "unified",
            "value_type": "str",
            "description": "Navigation rollout mode (`unified` or `legacy`).",
        },
        {
            "key": "ux_navigation_telemetry_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable navigation telemetry audit events (page views/switches) for IA tuning.",
        },
        {
            "key": "ux_role_default_landing_enabled",
            "value": "true",
            "value_type": "bool",
            "description": "Enable role-based default landing redirect from Home page.",
        },
        {
            "key": "ux_navigation_window_start_iso",
            "value": "",
            "value_type": "str",
            "description": "Optional telemetry window lower-bound timestamp (ISO UTC) for nav analytics.",
        },
        {
            "key": "ux_readiness_weight_permission_gap",
            "value": "12",
            "value_type": "int",
            "description": "Readiness score penalty per permission gap workflow.",
        },
        {
            "key": "ux_readiness_weight_audit_gap",
            "value": "8",
            "value_type": "int",
            "description": "Readiness score penalty per missing-audit-evidence workflow.",
        },
        {
            "key": "ux_readiness_weight_overdue_followup",
            "value": "5",
            "value_type": "int",
            "description": "Readiness score penalty per overdue open follow-up task.",
        },
        {
            "key": "ux_readiness_weight_task_gap",
            "value": "6",
            "value_type": "int",
            "description": "Readiness score penalty per workflow missing task-completion evidence.",
        },
        {
            "key": "ux_readiness_penalty_rejected_decision",
            "value": "25",
            "value_type": "int",
            "description": "Readiness score penalty when latest release decision is rejected.",
        },
        {
            "key": "ux_readiness_penalty_missing_decision",
            "value": "10",
            "value_type": "int",
            "description": "Readiness score penalty when no latest approved/rejected decision is present.",
        },
        {
            "key": "ux_parity_min_task_completion_events",
            "value": "1",
            "value_type": "int",
            "description": "Minimum workspace task-completion events required in parity lookback window per workflow.",
        },
    ]


def _build_env_coverage_rows(env_values: dict[str, str], recommended_defaults: dict[str, str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    keys = sorted(set(recommended_defaults.keys()) | set(env_values.keys()))
    for key in keys:
        current = str(env_values.get(key, ""))
        recommended = str(recommended_defaults.get(key, ""))
        present = key in env_values
        is_empty = present and not current.strip()
        if not present:
            status = "missing"
        elif is_empty:
            status = "empty"
        elif key in recommended_defaults and current.strip() == recommended.strip():
            status = "default"
        else:
            status = "set"
        rows.append(
            {
                "key": key,
                "status": status,
                "tracked": bool(key in recommended_defaults),
                "present_in_env": bool(present),
                "editable": bool(is_editable_env_key(key)),
                "is_sensitive": bool(key in SENSITIVE_ENV_KEYS),
                "current_value": mask_env_value(key, current),
                "recommended_default": mask_env_value(key, recommended),
                "current_raw_len": len(current),
            }
        )
    return rows


def _apply_slack_channel_presets(repo: InventoryRepository, *, actor: str, env_name: str) -> int:
    env = str(env_name or settings.app_env or "local").strip().lower()
    defaults = {
        "slack_default_channel": f"#gs-{env}-ops",
        "slack_channel_sync_failures": f"#gs-{env}-sync",
        "slack_channel_google_queue_failures": f"#gs-{env}-integrations",
        "slack_channel_warning": f"#gs-{env}-warn",
        "slack_channel_error": f"#gs-{env}-error",
        "slack_channel_critical": f"#gs-{env}-critical",
    }
    updated = 0
    for key, value in defaults.items():
        repo.upsert_runtime_setting(
            environment=settings.app_env,
            key=key,
            value=value,
            value_type="str",
            description="Auto-applied Slack channel preset.",
            is_active=True,
            actor=actor,
        )
        updated += 1
    return updated


def _health_label_and_emoji(ratio: float) -> tuple[str, str]:
    state = health_state(ratio)
    if state == "healthy":
        return "healthy", "green"
    if state == "warning":
        return "warning", "orange"
    return "critical", "red"


def _apply_required_env_defaults(
    *,
    env_path: str,
    required_keys: set[str],
    env_values: dict[str, str],
    recommended_defaults: dict[str, str],
) -> int:
    updated = 0
    for key in sorted(required_keys):
        current = str(env_values.get(key, ""))
        needs_fix = key not in env_values or not current.strip()
        if not needs_fix:
            continue
        if key not in recommended_defaults:
            continue
        upsert_env_key(env_path, key, str(recommended_defaults.get(key, "")))
        updated += 1
    return updated


def _apply_all_env_defaults(
    *,
    env_path: str,
    env_values: dict[str, str],
    recommended_defaults: dict[str, str],
) -> int:
    updated = 0
    for key in sorted(recommended_defaults.keys()):
        current = str(env_values.get(key, ""))
        needs_fix = key not in env_values or not current.strip()
        if not needs_fix:
            continue
        upsert_env_key(env_path, key, str(recommended_defaults.get(key, "")))
        updated += 1
    return updated


def _apply_required_runtime_defaults(
    *,
    repo: InventoryRepository,
    actor: str,
    required_keys: set[str],
    runtime_rows: list,
    seed_defaults: list[dict[str, str]],
) -> int:
    by_key = {str(row.key): row for row in runtime_rows}
    defaults_by_key = {str(item["key"]): item for item in seed_defaults}
    updated = 0
    for key in sorted(required_keys):
        default_item = defaults_by_key.get(key)
        if default_item is None:
            continue
        row = by_key.get(key)
        if row is None:
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key=key,
                value=str(default_item["value"]),
                value_type=str(default_item["value_type"]),
                description=str(default_item.get("description") or ""),
                is_active=True,
                actor=actor,
            )
            updated += 1
            continue
        if not bool(row.is_active):
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key=key,
                value=str(row.value or default_item["value"]),
                value_type=str(row.value_type or default_item["value_type"]),
                description=str(row.description or default_item.get("description") or ""),
                is_active=True,
                actor=actor,
            )
            updated += 1
    return updated


def _apply_all_runtime_defaults(
    *,
    repo: InventoryRepository,
    actor: str,
    runtime_rows: list,
    seed_defaults: list[dict[str, str]],
) -> int:
    by_key = {str(row.key): row for row in runtime_rows}
    updated = 0
    for item in seed_defaults:
        key = str(item.get("key") or "")
        if not key:
            continue
        row = by_key.get(key)
        if row is None:
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key=key,
                value=str(item.get("value") or ""),
                value_type=str(item.get("value_type") or "str"),
                description=str(item.get("description") or ""),
                is_active=True,
                actor=actor,
            )
            updated += 1
            continue
        if not bool(row.is_active):
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key=key,
                value=str(row.value or item.get("value") or ""),
                value_type=str(row.value_type or item.get("value_type") or "str"),
                description=str(row.description or item.get("description") or ""),
                is_active=True,
                actor=actor,
            )
            updated += 1
    return updated


def _build_runtime_coverage_rows(runtime_rows: list, seed_defaults: list[dict[str, str]]) -> list[dict[str, str]]:
    by_key = {str(row.key): row for row in runtime_rows}
    tracked_keys: set[str] = set()
    rows: list[dict[str, str]] = []
    for item in seed_defaults:
        key = str(item.get("key") or "")
        tracked_keys.add(key)
        expected_value = str(item.get("value") or "")
        expected_type = str(item.get("value_type") or "str")
        row = by_key.get(key)
        if row is None:
            status = "missing"
            current_value = ""
            current_type = expected_type
            is_active = False
            updated_by = ""
            updated_at = ""
        else:
            current_value = str(row.value or "")
            current_type = str(row.value_type or "")
            is_active = bool(row.is_active)
            updated_by = str(row.updated_by or "")
            updated_at = row.updated_at.isoformat() if row.updated_at else ""
            if not is_active:
                status = "inactive"
            elif current_value.strip() == expected_value.strip() and current_type == expected_type:
                status = "default"
            else:
                status = "overridden"
        rows.append(
            {
                "key": key,
                "status": status,
                "expected_type": expected_type,
                "current_type": current_type,
                "expected_default": expected_value,
                "current_value": current_value,
                "is_active": bool(is_active),
                "updated_by": updated_by,
                "updated_at": updated_at,
                "description": str(item.get("description") or ""),
            }
        )
    for key, row in sorted(by_key.items(), key=lambda kv: str(kv[0])):
        if key in tracked_keys:
            continue
        rows.append(
            {
                "key": key,
                "status": "custom_untracked",
                "expected_type": "",
                "current_type": str(row.value_type or ""),
                "expected_default": "",
                "current_value": str(row.value or ""),
                "is_active": bool(row.is_active),
                "updated_by": str(row.updated_by or ""),
                "updated_at": row.updated_at.isoformat() if row.updated_at else "",
                "description": str(row.description or ""),
            }
        )
    return rows


def _seed_missing_runtime_defaults(repo: InventoryRepository, *, actor: str, seed_defaults: list[dict[str, str]]) -> int:
    seeded = 0
    for item in seed_defaults:
        try:
            existing = repo.get_runtime_setting(
                environment=settings.app_env,
                key=item["key"],
                active_only=False,
            )
            if existing is not None:
                continue
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key=item["key"],
                value=item["value"],
                value_type=item["value_type"],
                description=item["description"],
                is_active=True,
                actor=actor,
            )
            seeded += 1
        except Exception:
            continue
    return seeded


def _render_comp_dealer_domains_editor(repo: InventoryRepository, user) -> None:
    st.markdown("### Comp Dealer Domains")
    st.caption(
        "Manage dealer domains used by Comp Tool parser confidence and domain-specific extraction."
    )
    current_domain_setting = repo.get_runtime_setting(
        environment=settings.app_env,
        key="comp_dealer_domains_csv",
        active_only=False,
    )
    current_domain_value = (
        (current_domain_setting.value if current_domain_setting is not None else ",".join(DEFAULT_COMP_DEALER_DOMAINS))
        or ",".join(DEFAULT_COMP_DEALER_DOMAINS)
    )
    normalized_preview_csv, normalized_preview = _normalize_comp_dealer_domains_csv(current_domain_value)
    st.caption(f"Current normalized domains: {len(normalized_preview)}")
    with st.form("admin_comp_dealer_domains_form"):
        comp_domains_text = st.text_area(
            "Dealer Domains (comma-separated)",
            value=current_domain_value,
            height=120,
            help="Example: apmex.com,jmbullion.com,sdbullion.com",
        )
        normalized_input_csv, normalized_input = _normalize_comp_dealer_domains_csv(comp_domains_text)
        st.caption(f"Normalized preview ({len(normalized_input)}): {', '.join(normalized_input[:20])}")
        if len(normalized_input) > 20:
            st.caption(f"... and {len(normalized_input) - 20} more")
        comp_domains_is_active = st.checkbox(
            "Active",
            value=bool(current_domain_setting.is_active) if current_domain_setting is not None else True,
        )
        cd1, cd2 = st.columns(2)
        with cd1:
            comp_domains_save = st.form_submit_button("Save Dealer Domains")
        with cd2:
            comp_domains_reset = st.form_submit_button("Reset To Defaults")
    if comp_domains_save:
        if not normalized_input:
            st.error("Provide at least one valid domain (example: apmex.com).")
            return
        try:
            row = repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="comp_dealer_domains_csv",
                value=normalized_input_csv,
                value_type="str",
                description="Comma-separated dealer domains used for comp parser/domain weighting.",
                is_active=bool(comp_domains_is_active),
                actor=user.username,
            )
            st.success(f"Saved `{row.key}`.")
            st.rerun()
        except Exception as exc:
            st.error(f"Unable to save dealer domains: {exc}")
    if comp_domains_reset:
        try:
            row = repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="comp_dealer_domains_csv",
                value=",".join(DEFAULT_COMP_DEALER_DOMAINS),
                value_type="str",
                description="Comma-separated dealer domains used for comp parser/domain weighting.",
                is_active=True,
                actor=user.username,
            )
            st.success(f"Reset `{row.key}` to defaults.")
            st.rerun()
        except Exception as exc:
            st.error(f"Unable to reset dealer domains: {exc}")


def _render_comp_photo_retry_telemetry(repo: InventoryRepository, user) -> None:
    st.markdown("### Photo-Comp Retry Telemetry")
    st.caption(
        "Review recent photo-comp retry strategy outcomes (coverage, no-result rate, and strategy effectiveness)."
    )
    c1, c2 = st.columns(2)
    with c1:
        lookback_days = st.number_input(
            "Lookback Days",
            min_value=1,
            max_value=365,
            value=14,
            step=1,
            key="admin_comp_photo_retry_lookback_days",
        )
    with c2:
        max_rows = st.number_input(
            "Max Rows",
            min_value=20,
            max_value=5000,
            value=500,
            step=20,
            key="admin_comp_photo_retry_max_rows",
        )
    cutoff = utcnow_naive() - timedelta(days=int(lookback_days))
    logs = repo.db.scalars(
        select(AuditLog)
        .where(
            AuditLog.entity_type == "comp_photo_retry",
            AuditLog.action == "run",
            AuditLog.created_at >= cutoff,
        )
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(int(max_rows))
    ).all()
    parsed_rows: list[dict] = []
    for log in logs:
        try:
            payload = json.loads(log.changes_json or "{}")
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}
        parsed_rows.append(
            {
                "time": log.created_at,
                "actor": str(log.actor or ""),
                "strategy": str(payload.get("strategy") or ""),
                "run_label": str(payload.get("run_label") or ""),
                "query": str(payload.get("query") or ""),
                "coverage_pct": float(payload.get("coverage_pct") or 0.0),
                "rows_total": int(payload.get("rows_total") or 0),
                "rows_priced": int(payload.get("rows_priced") or 0),
                "rows_missing_price": int(payload.get("rows_missing_price") or 0),
                "web_rows_total": int(payload.get("web_rows_total") or 0),
                "web_rows_priced": int(payload.get("web_rows_priced") or 0),
                "web_rows_missing_price": int(payload.get("web_rows_missing_price") or 0),
                "result": str(payload.get("result") or ""),
                "raw_payload": payload,
            }
        )
    if not parsed_rows:
        st.info("No `comp_photo_retry` telemetry events found in selected lookback.")
        return

    strategy_values = sorted(
        {str(row.get("strategy") or "").strip().lower() for row in parsed_rows if str(row.get("strategy") or "").strip()}
    )
    selected_strategies = st.multiselect(
        "Strategy Filter",
        options=strategy_values,
        default=strategy_values,
        key="admin_comp_photo_retry_strategy_filter",
    )
    filtered_rows = [
        row
        for row in parsed_rows
        if not selected_strategies
        or str(row.get("strategy") or "").strip().lower() in set(selected_strategies)
    ]
    if not filtered_rows:
        st.info("No telemetry rows match selected strategy filters.")
        return

    total_runs = len(filtered_rows)
    no_rows_runs = sum(1 for row in filtered_rows if str(row.get("result") or "").strip().lower() == "no_rows")
    avg_coverage = sum(float(row.get("coverage_pct") or 0.0) for row in filtered_rows) / max(1, total_runs)
    no_rows_rate = (float(no_rows_runs) / float(total_runs) * 100.0) if total_runs else 0.0
    strategy_df = (
        pd.DataFrame(filtered_rows)
        .groupby(["strategy"], dropna=False)
        .agg(
            runs=("strategy", "count"),
            avg_coverage_pct=("coverage_pct", "mean"),
            no_rows_runs=("result", lambda s: int((s.fillna("").astype(str).str.lower() == "no_rows").sum())),
        )
        .reset_index()
        .sort_values(["runs", "avg_coverage_pct"], ascending=[False, False])
    )
    if not strategy_df.empty:
        strategy_df["no_rows_rate_pct"] = strategy_df.apply(
            lambda r: (float(r["no_rows_runs"]) / float(max(1, int(r["runs"]))) * 100.0), axis=1
        )
        best_row = strategy_df.sort_values(["avg_coverage_pct", "runs"], ascending=[False, False]).iloc[0]
        best_strategy = str(best_row.get("strategy") or "")
        best_strategy_coverage = float(best_row.get("avg_coverage_pct") or 0.0)
    else:
        best_strategy = ""
        best_strategy_coverage = 0.0

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Runs", int(total_runs))
    m2.metric("No-Result Rate", f"{no_rows_rate:.1f}%")
    m3.metric("Avg Coverage", f"{avg_coverage:.1f}%")
    m4.metric("Top Strategy (Coverage)", f"{best_strategy or '-'} ({best_strategy_coverage:.1f}%)")

    st.caption("Strategy performance summary")
    st.dataframe(strategy_df, use_container_width=True)

    st.markdown("#### Strategy Trends")
    trend_df = pd.DataFrame(filtered_rows).copy()
    if not trend_df.empty:
        trend_df["date"] = pd.to_datetime(trend_df["time"], errors="coerce").dt.date
        coverage_trend = (
            trend_df.groupby(["date", "strategy"], dropna=False)["coverage_pct"]
            .mean()
            .reset_index()
            .sort_values(["date", "strategy"], ascending=[True, True])
        )
        if not coverage_trend.empty:
            coverage_pivot = (
                coverage_trend.pivot(index="date", columns="strategy", values="coverage_pct")
                .sort_index()
            )
            st.caption("Average coverage % by strategy over time")
            st.line_chart(coverage_pivot, use_container_width=True)
        no_result_trend = (
            trend_df.assign(
                no_result=trend_df["result"].fillna("").astype(str).str.lower().eq("no_rows").astype(int)
            )
            .groupby(["date", "strategy"], dropna=False)
            .agg(no_result_rate_pct=("no_result", "mean"))
            .reset_index()
            .sort_values(["date", "strategy"], ascending=[True, True])
        )
        if not no_result_trend.empty:
            no_result_trend["no_result_rate_pct"] = no_result_trend["no_result_rate_pct"] * 100.0
            no_result_pivot = (
                no_result_trend.pivot(index="date", columns="strategy", values="no_result_rate_pct")
                .sort_index()
            )
            st.caption("No-result rate % by strategy over time")
            st.line_chart(no_result_pivot, use_container_width=True)

    st.markdown("#### Top Missing-Price Domains")
    domain_miss_counter: Counter[str] = Counter()
    domain_priced_counter: Counter[str] = Counter()
    for row in filtered_rows:
        payload = row.get("raw_payload") or {}
        try:
            missing_pairs = json.loads(str(payload.get("top_missing_domains_json") or "[]"))
        except Exception:
            missing_pairs = []
        try:
            priced_pairs = json.loads(str(payload.get("top_priced_domains_json") or "[]"))
        except Exception:
            priced_pairs = []
        if isinstance(missing_pairs, list):
            for pair in missing_pairs:
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    continue
                domain = str(pair[0] or "").strip().lower()
                if not domain:
                    continue
                try:
                    domain_miss_counter[domain] += int(pair[1] or 0)
                except Exception:
                    continue
        if isinstance(priced_pairs, list):
            for pair in priced_pairs:
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    continue
                domain = str(pair[0] or "").strip().lower()
                if not domain:
                    continue
                try:
                    domain_priced_counter[domain] += int(pair[1] or 0)
                except Exception:
                    continue
    domain_rows: list[dict[str, Any]] = []
    for domain, miss_count in domain_miss_counter.most_common(30):
        priced_count = int(domain_priced_counter.get(domain, 0))
        total = int(miss_count + priced_count)
        miss_rate = (float(miss_count) / float(max(1, total))) * 100.0
        domain_rows.append(
            {
                "domain": domain,
                "missing_price_rows": int(miss_count),
                "priced_rows": int(priced_count),
                "total_rows": int(total),
                "missing_rate_pct": float(round(miss_rate, 2)),
            }
        )
    if domain_rows:
        domain_df = pd.DataFrame(domain_rows).sort_values(
            ["missing_price_rows", "missing_rate_pct"], ascending=[False, False]
        )
        st.dataframe(domain_df, use_container_width=True)
        top10 = domain_df.head(10).set_index("domain")
        st.caption("Top 10 missing-price domains")
        st.bar_chart(top10["missing_price_rows"], use_container_width=True)

        st.markdown("#### Dealer-Domain Recommendations")
        current_domain_setting = repo.get_runtime_setting(
            environment=settings.app_env,
            key="comp_dealer_domains_csv",
            active_only=False,
        )
        current_domain_csv = (
            str(current_domain_setting.value or "").strip()
            if current_domain_setting is not None
            else ",".join(DEFAULT_COMP_DEALER_DOMAINS)
        )
        _, current_domains = _normalize_comp_dealer_domains_csv(current_domain_csv)
        current_domain_set = {d.strip().lower() for d in current_domains if d.strip()}

        r1, r2, r3 = st.columns(3)
        with r1:
            min_obs = st.number_input(
                "Min Domain Observations",
                min_value=1,
                max_value=200,
                value=5,
                step=1,
                key="admin_comp_domain_rec_min_obs",
            )
        with r2:
            add_max_missing_rate = st.number_input(
                "Add If Missing Rate ≤ %",
                min_value=0.0,
                max_value=100.0,
                value=40.0,
                step=1.0,
                key="admin_comp_domain_rec_add_max_missing",
            )
        with r3:
            remove_min_missing_rate = st.number_input(
                "Remove If Missing Rate ≥ %",
                min_value=0.0,
                max_value=100.0,
                value=80.0,
                step=1.0,
                key="admin_comp_domain_rec_remove_min_missing",
            )

        recommended_add: list[str] = []
        recommended_remove: list[str] = []
        for _, row in domain_df.iterrows():
            domain = str(row.get("domain") or "").strip().lower()
            if not domain:
                continue
            total_rows = int(row.get("total_rows") or 0)
            priced_rows = int(row.get("priced_rows") or 0)
            miss_rate = float(row.get("missing_rate_pct") or 0.0)
            if total_rows < int(min_obs):
                continue
            if domain not in current_domain_set and priced_rows > 0 and miss_rate <= float(add_max_missing_rate):
                recommended_add.append(domain)
            if domain in current_domain_set and priced_rows == 0 and miss_rate >= float(remove_min_missing_rate):
                recommended_remove.append(domain)

        st.caption(f"Current configured dealer domains: {len(current_domains)}")
        a1, a2 = st.columns(2)
        with a1:
            st.caption(f"Recommended Add ({len(recommended_add)})")
            st.code(", ".join(recommended_add) if recommended_add else "(none)")
        with a2:
            st.caption(f"Recommended Remove ({len(recommended_remove)})")
            st.code(", ".join(recommended_remove) if recommended_remove else "(none)")

        merged_domain_list = sorted((current_domain_set | set(recommended_add)) - set(recommended_remove))
        st.caption(f"Preview configured domains after apply: {len(merged_domain_list)}")
        st.code(", ".join(merged_domain_list) if merged_domain_list else "(empty)")

        st.markdown("##### Dry-Run Change Preview")
        preview_mode = st.radio(
            "Preview Mode",
            options=["Add Only", "Remove Only", "Add + Remove"],
            horizontal=True,
            key="admin_comp_domain_preview_mode",
        )
        if preview_mode == "Add Only":
            preview_updated_domains = sorted(current_domain_set | set(recommended_add))
            preview_add = sorted(set(recommended_add) - current_domain_set)
            preview_remove = []
        elif preview_mode == "Remove Only":
            preview_updated_domains = sorted(current_domain_set - set(recommended_remove))
            preview_add = []
            preview_remove = sorted(current_domain_set & set(recommended_remove))
        else:
            preview_updated_domains = sorted((current_domain_set | set(recommended_add)) - set(recommended_remove))
            preview_add = sorted(set(recommended_add) - current_domain_set)
            preview_remove = sorted(current_domain_set & set(recommended_remove))
        d1, d2, d3, d4 = st.columns(4)
        d1.metric("Current Domains", int(len(current_domain_set)))
        d2.metric("Will Add", int(len(preview_add)))
        d3.metric("Will Remove", int(len(preview_remove)))
        d4.metric("Result Domains", int(len(preview_updated_domains)))
        preview_rows: list[dict[str, str]] = []
        for domain in preview_add:
            preview_rows.append({"domain": domain, "change": "add"})
        for domain in preview_remove:
            preview_rows.append({"domain": domain, "change": "remove"})
        if preview_rows:
            st.dataframe(pd.DataFrame(preview_rows).sort_values(["change", "domain"]), use_container_width=True)
        else:
            st.caption("No domain changes for this preview mode.")

        ap1, ap2, ap3 = st.columns(3)
        with ap1:
            apply_add = st.button(
                "Apply Add Recommendations",
                key="admin_comp_domain_apply_add_btn",
                disabled=not bool(recommended_add),
            )
        with ap2:
            apply_remove = st.button(
                "Apply Remove Recommendations",
                key="admin_comp_domain_apply_remove_btn",
                disabled=not bool(recommended_remove),
            )
        with ap3:
            apply_all = st.button(
                "Apply Add + Remove",
                key="admin_comp_domain_apply_all_btn",
                disabled=not bool(recommended_add or recommended_remove),
            )

        if apply_add or apply_remove or apply_all:
            if apply_add:
                updated_domains = sorted(current_domain_set | set(recommended_add))
            elif apply_remove:
                updated_domains = sorted(current_domain_set - set(recommended_remove))
            else:
                updated_domains = sorted((current_domain_set | set(recommended_add)) - set(recommended_remove))
            if not updated_domains:
                st.error("Apply blocked: result would leave dealer-domain config empty.")
            else:
                try:
                    row = repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key="comp_dealer_domains_csv",
                        value=",".join(updated_domains),
                        value_type="str",
                        description="Comma-separated dealer domains used for comp parser/domain weighting.",
                        is_active=bool(current_domain_setting.is_active) if current_domain_setting is not None else True,
                        actor=user.username,
                    )
                    action_label = (
                        "add-only"
                        if apply_add
                        else ("remove-only" if apply_remove else "add+remove")
                    )
                    try:
                        repo.record_audit_event(
                            entity_type="comp_domain_recommendation",
                            entity_id=None,
                            action="apply",
                            actor=user.username,
                            changes={
                                "mode": action_label,
                                "current_count": int(len(current_domain_set)),
                                "updated_count": int(len(updated_domains)),
                                "recommended_add": list(recommended_add),
                                "recommended_remove": list(recommended_remove),
                                "applied_add": sorted(list(set(updated_domains) - current_domain_set)),
                                "applied_remove": sorted(list(current_domain_set - set(updated_domains))),
                                "before_domains_csv": ",".join(sorted(current_domain_set)),
                                "after_domains_csv": ",".join(sorted(updated_domains)),
                            },
                        )
                    except Exception:
                        pass
                    st.success(
                        f"Applied dealer-domain recommendations ({action_label}). "
                        f"`{row.key}` now has {len(updated_domains)} domains."
                    )
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to apply dealer-domain recommendations: {exc}")

        st.markdown("##### Recommendation Apply History")
        hist1, hist2 = st.columns(2)
        with hist1:
            history_lookback_days = st.number_input(
                "History Lookback Days",
                min_value=1,
                max_value=365,
                value=30,
                step=1,
                key="admin_comp_domain_rec_history_lookback_days",
            )
        with hist2:
            history_limit = st.number_input(
                "History Max Rows",
                min_value=20,
                max_value=2000,
                value=200,
                step=20,
                key="admin_comp_domain_rec_history_limit",
            )
        history_cutoff = utcnow_naive() - timedelta(days=int(history_lookback_days))
        history_logs = repo.db.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "comp_domain_recommendation",
                AuditLog.action == "apply",
                AuditLog.created_at >= history_cutoff,
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(int(history_limit))
        ).all()
        history_rows: list[dict[str, Any]] = []
        for log in history_logs:
            try:
                payload = json.loads(log.changes_json or "{}")
                if not isinstance(payload, dict):
                    payload = {}
            except Exception:
                payload = {}
            history_rows.append(
                {
                    "audit_id": int(log.id),
                    "time": log.created_at,
                    "actor": str(log.actor or ""),
                    "mode": str(payload.get("mode") or ""),
                    "current_count": int(payload.get("current_count") or 0),
                    "updated_count": int(payload.get("updated_count") or 0),
                    "recommended_add_count": len(payload.get("recommended_add") or []),
                    "recommended_remove_count": len(payload.get("recommended_remove") or []),
                    "applied_add_count": len(payload.get("applied_add") or []),
                    "applied_remove_count": len(payload.get("applied_remove") or []),
                    "applied_add_csv": ",".join(payload.get("applied_add") or []),
                    "applied_remove_csv": ",".join(payload.get("applied_remove") or []),
                    "before_domains_csv": str(payload.get("before_domains_csv") or ""),
                    "after_domains_csv": str(payload.get("after_domains_csv") or ""),
                }
            )
        if history_rows:
            history_df = pd.DataFrame(history_rows).sort_values(["time"], ascending=[False])
            st.dataframe(history_df, use_container_width=True)
            row_map = {
                f"#{int(row['audit_id'])} | {str(row['time'])} | {str(row['actor'])} | {str(row['mode'])}": row
                for row in history_rows
            }
            selected_history_key = st.selectbox(
                "Undo Target",
                options=list(row_map.keys()),
                key="admin_comp_domain_rec_undo_target",
            )
            if st.button("Undo Selected Apply (Restore Before Set)", key="admin_comp_domain_rec_undo_btn"):
                selected_row = row_map.get(selected_history_key) or {}
                before_csv = str(selected_row.get("before_domains_csv") or "").strip()
                if not before_csv:
                    try:
                        repo.record_audit_event(
                            entity_type="comp_domain_recommendation",
                            entity_id=None,
                            action="undo_failed",
                            actor=user.username,
                            changes={
                                "source_apply_audit_id": int(selected_row.get("audit_id") or 0),
                                "reason": "missing_before_snapshot",
                            },
                        )
                    except Exception:
                        pass
                    st.error(
                        "Undo is unavailable for this event because full before-domain snapshot "
                        "was not recorded yet."
                    )
                else:
                    _, normalized_before = _normalize_comp_dealer_domains_csv(before_csv)
                    if not normalized_before:
                        try:
                            repo.record_audit_event(
                                entity_type="comp_domain_recommendation",
                                entity_id=None,
                                action="undo_failed",
                                actor=user.username,
                                changes={
                                    "source_apply_audit_id": int(selected_row.get("audit_id") or 0),
                                    "reason": "empty_normalized_before_snapshot",
                                },
                            )
                        except Exception:
                            pass
                        st.error("Undo blocked: before snapshot resolved to empty domain list.")
                    else:
                        try:
                            row = repo.upsert_runtime_setting(
                                environment=settings.app_env,
                                key="comp_dealer_domains_csv",
                                value=",".join(normalized_before),
                                value_type="str",
                                description="Comma-separated dealer domains used for comp parser/domain weighting.",
                                is_active=bool(current_domain_setting.is_active) if current_domain_setting is not None else True,
                                actor=user.username,
                            )
                            try:
                                repo.record_audit_event(
                                    entity_type="comp_domain_recommendation",
                                    entity_id=None,
                                    action="undo",
                                    actor=user.username,
                                    changes={
                                        "source_apply_audit_id": int(selected_row.get("audit_id") or 0),
                                        "restored_domain_count": int(len(normalized_before)),
                                        "restored_domains_csv": ",".join(normalized_before),
                                    },
                                )
                            except Exception:
                                pass
                            st.success(
                                f"Undo complete. Restored `{row.key}` to "
                                f"{len(normalized_before)} domains from selected apply event."
                            )
                            st.rerun()
                        except Exception as exc:
                            try:
                                repo.record_audit_event(
                                    entity_type="comp_domain_recommendation",
                                    entity_id=None,
                                    action="undo_failed",
                                    actor=user.username,
                                    changes={
                                        "source_apply_audit_id": int(selected_row.get("audit_id") or 0),
                                        "reason": "exception",
                                        "error": str(exc),
                                    },
                                )
                            except Exception:
                                pass
                            st.error(f"Unable to undo selected apply event: {exc}")
            st.download_button(
                "Download Recommendation Apply History CSV",
                data=history_df.to_csv(index=False).encode("utf-8"),
                file_name=(
                    f"comp_domain_recommendation_apply_{settings.app_env}_"
                    f"{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv"
                ),
                mime="text/csv",
                key="admin_comp_domain_rec_history_csv_btn",
            )

            st.markdown("##### Undo Telemetry")
            undo_logs = repo.db.scalars(
                select(AuditLog)
                .where(
                    AuditLog.entity_type == "comp_domain_recommendation",
                    AuditLog.action.in_(["undo", "undo_failed"]),
                    AuditLog.created_at >= history_cutoff,
                )
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                .limit(int(history_limit))
            ).all()
            undo_rows: list[dict[str, Any]] = []
            for log in undo_logs:
                try:
                    payload = json.loads(log.changes_json or "{}")
                    if not isinstance(payload, dict):
                        payload = {}
                except Exception:
                    payload = {}
                undo_rows.append(
                    {
                        "time": log.created_at,
                        "actor": str(log.actor or ""),
                        "action": str(log.action or ""),
                        "source_apply_audit_id": int(payload.get("source_apply_audit_id") or 0),
                        "reason": str(payload.get("reason") or ""),
                        "restored_domain_count": int(payload.get("restored_domain_count") or 0),
                        "error": str(payload.get("error") or ""),
                    }
                )
            if undo_rows:
                undo_total = len(undo_rows)
                undo_success = sum(1 for row in undo_rows if str(row.get("action") or "") == "undo")
                undo_failed = sum(1 for row in undo_rows if str(row.get("action") or "") == "undo_failed")
                undo_success_rate = (float(undo_success) / float(max(1, undo_total))) * 100.0
                u1, u2, u3 = st.columns(3)
                u1.metric("Undo Attempts", int(undo_total))
                u2.metric("Undo Success", int(undo_success))
                u3.metric("Undo Success Rate", f"{undo_success_rate:.1f}%")
                undo_df = pd.DataFrame(undo_rows).sort_values(["time"], ascending=[False])
                actor_summary = (
                    undo_df.groupby(["actor", "action"], dropna=False)
                    .size()
                    .reset_index(name="events")
                    .sort_values(["events"], ascending=[False])
                )
                st.caption("Undo events by actor/action")
                st.dataframe(actor_summary, use_container_width=True)
                st.caption("Recent undo events")
                st.dataframe(undo_df.head(50), use_container_width=True)
                st.download_button(
                    "Download Undo Telemetry CSV",
                    data=undo_df.to_csv(index=False).encode("utf-8"),
                    file_name=(
                        f"comp_domain_recommendation_undo_{settings.app_env}_"
                        f"{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv"
                    ),
                    mime="text/csv",
                    key="admin_comp_domain_rec_undo_csv_btn",
                )
            else:
                st.caption("No undo events found for the selected lookback window.")

            st.markdown("##### Governance Bundle Export")
            bundle_buffer = BytesIO()
            with zipfile.ZipFile(bundle_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as bundle_zip:
                strategy_export_df = strategy_df.copy()
                strategy_export_df.insert(0, "environment", settings.app_env)
                strategy_export_df.insert(1, "lookback_days", int(lookback_days))
                strategy_export_df.insert(2, "strategy_filter_csv", ",".join(selected_strategies))
                bundle_zip.writestr(
                    "photo_comp_strategy_performance.csv",
                    strategy_export_df.to_csv(index=False),
                )
                domain_export_df = domain_df.copy()
                domain_export_df.insert(0, "environment", settings.app_env)
                domain_export_df.insert(1, "lookback_days", int(lookback_days))
                bundle_zip.writestr(
                    "photo_comp_domain_leaderboard.csv",
                    domain_export_df.to_csv(index=False),
                )
                history_export_df = history_df.copy()
                history_export_df.insert(0, "environment", settings.app_env)
                history_export_df.insert(1, "history_lookback_days", int(history_lookback_days))
                bundle_zip.writestr(
                    "domain_recommendation_apply_history.csv",
                    history_export_df.to_csv(index=False),
                )
                if undo_rows:
                    undo_export_df = undo_df.copy()
                    undo_export_df.insert(0, "environment", settings.app_env)
                    undo_export_df.insert(1, "history_lookback_days", int(history_lookback_days))
                    bundle_zip.writestr(
                        "domain_recommendation_undo_telemetry.csv",
                        undo_export_df.to_csv(index=False),
                    )
                recommendation_summary_df = pd.DataFrame(
                    [
                        {
                            "environment": settings.app_env,
                            "min_domain_observations": int(min_obs),
                            "add_max_missing_rate_pct": float(add_max_missing_rate),
                            "remove_min_missing_rate_pct": float(remove_min_missing_rate),
                            "current_domain_count": int(len(current_domains)),
                            "recommended_add_count": int(len(recommended_add)),
                            "recommended_remove_count": int(len(recommended_remove)),
                            "preview_mode": str(preview_mode),
                            "preview_add_count": int(len(preview_add)),
                            "preview_remove_count": int(len(preview_remove)),
                            "preview_result_domain_count": int(len(preview_updated_domains)),
                            "generated_at_utc": utcnow_naive().isoformat(),
                        }
                    ]
                )
                bundle_zip.writestr(
                    "domain_recommendation_summary.csv",
                    recommendation_summary_df.to_csv(index=False),
                )
            bundle_buffer.seek(0)
            st.download_button(
                "Export Photo-Comp Governance Bundle (ZIP)",
                data=bundle_buffer.getvalue(),
                file_name=(
                    f"photo_comp_governance_bundle_{settings.app_env}_"
                    f"{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.zip"
                ),
                mime="application/zip",
                key="admin_comp_domain_rec_governance_bundle_zip_btn",
            )
        else:
            st.caption("No recommendation-apply history found for the selected lookback window.")
    else:
        st.info("No domain-level miss telemetry available yet. Run photo-comp searches with web fallback.")

    st.markdown("#### Promote Strategy To Default Retry Preset")
    promote_min_runs = st.number_input(
        "Minimum Runs Required For Promotion",
        min_value=1,
        max_value=200,
        value=5,
        step=1,
        key="admin_comp_retry_promote_min_runs",
        help="Guardrail: block promotion until the selected best strategy has at least this many runs.",
    )
    promotable_rows = [row for row in filtered_rows if str(row.get("result") or "").strip().lower() != "no_rows"]
    if promotable_rows:
        best_row = sorted(
            promotable_rows,
            key=lambda r: (float(r.get("coverage_pct") or 0.0), int(r.get("rows_total") or 0)),
            reverse=True,
        )[0]
        suggested_strategy = str(best_row.get("strategy") or "").strip() or "manual"
        suggested_name = f"Auto {suggested_strategy} ({utcnow_naive().strftime('%Y-%m-%d')})"
        strategy_subset = [
            row for row in filtered_rows if str(row.get("strategy") or "").strip().lower() == suggested_strategy.lower()
        ]
        strategy_runs = len(strategy_subset)
        strategy_no_rows_runs = sum(
            1
            for row in strategy_subset
            if str(row.get("result") or "").strip().lower() == "no_rows"
        )
        strategy_no_rows_rate = (
            float(strategy_no_rows_runs) / float(max(1, strategy_runs)) * 100.0
            if strategy_runs
            else 0.0
        )
        confidence_label = "low"
        if strategy_runs >= int(promote_min_runs) and strategy_no_rows_rate <= 25.0:
            confidence_label = "high"
        elif strategy_runs >= max(2, int(promote_min_runs // 2)) and strategy_no_rows_rate <= 40.0:
            confidence_label = "medium"
        p1, p2, p3 = st.columns(3)
        with p1:
            promote_name = st.text_input(
                "Preset Name",
                value=suggested_name,
                key="admin_comp_retry_promote_name",
            )
        with p2:
            promote_shared = st.checkbox(
                "Team-shared",
                value=False,
                key="admin_comp_retry_promote_shared",
            )
        with p3:
            st.caption(
                f"Suggested from strategy `{suggested_strategy}` "
                f"(coverage {float(best_row.get('coverage_pct') or 0.0):.1f}%)."
            )
            st.caption(
                f"Sample confidence: `{confidence_label}` "
                f"(runs={int(strategy_runs)}, no-result-rate={strategy_no_rows_rate:.1f}%)."
            )
            promote_clicked = st.button(
                "Promote Best To Default",
                key="admin_comp_retry_promote_btn",
                use_container_width=True,
            )
        if promote_clicked:
            resolved_name = str(promote_name or "").strip()
            if not resolved_name:
                st.error("Preset name is required.")
            elif int(strategy_runs) < int(promote_min_runs):
                st.error(
                    f"Promotion blocked: strategy `{suggested_strategy}` has {int(strategy_runs)} runs, "
                    f"below minimum required {int(promote_min_runs)}."
                )
            else:
                payload_src = dict(best_row.get("raw_payload") or {})
                preset_payload = {
                    "sold_only": bool(payload_src.get("sold_only")),
                    "auto_broaden": bool(payload_src.get("auto_broaden")),
                    "use_web_fallback": bool(payload_src.get("used_web_fallback")),
                    "use_ai_summary": bool(payload_src.get("used_ai_summary")),
                    "web_fallback_limit": int(payload_src.get("web_fallback_limit") or 20),
                    "web_detail_fetch_limit": int(payload_src.get("web_detail_fetch_limit") or 20),
                    "min_web_confidence": str(payload_src.get("min_web_confidence") or "any"),
                    "min_web_confidence_score": float(payload_src.get("min_web_confidence_score") or 0.0),
                    "parser_source_filter": list(payload_src.get("parser_source_filter") or []),
                    "domain_include_raw": str(payload_src.get("domain_include_raw") or ""),
                    "domain_exclude_raw": str(payload_src.get("domain_exclude_raw") or ""),
                }
                try:
                    repo.upsert_saved_filter_profile(
                        environment=settings.app_env,
                        username=user.username,
                        scope="tools_photo_comp_retry",
                        name=resolved_name,
                        filter_json=json.dumps(preset_payload),
                        is_shared=bool(promote_shared),
                        is_default=True,
                        is_active=True,
                        actor=user.username,
                    )
                    st.success(
                        f"Promoted `{resolved_name}` as default photo-comp retry preset "
                        f"from strategy `{suggested_strategy}`."
                    )
                except Exception as exc:
                    st.error(f"Unable to promote strategy to default preset: {exc}")
    else:
        st.info("No successful (priced) retry runs available to promote yet.")

    rows_df = pd.DataFrame(filtered_rows).sort_values(["time"], ascending=[False])
    st.caption("Recent retry telemetry")
    st.dataframe(rows_df.head(50), use_container_width=True)
    csv_bytes = rows_df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download Retry Telemetry CSV",
        data=csv_bytes,
        file_name=f"comp_photo_retry_{settings.app_env}_{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv",
        mime="text/csv",
        key="admin_comp_photo_retry_csv_btn",
    )


def _render_listing_review_policy_editor(repo: InventoryRepository, user) -> None:
    st.markdown("### Listing Review Policy")
    st.caption("Configure optional two-person review requirement for selected marketplaces.")
    required_row = repo.get_runtime_setting(
        environment=settings.app_env,
        key="listing_review_two_person_required",
        active_only=False,
    )
    channels_row = repo.get_runtime_setting(
        environment=settings.app_env,
        key="listing_review_two_person_channels_csv",
        active_only=False,
    )
    required_value = str(getattr(required_row, "value", "false") or "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }
    channels_value = str(getattr(channels_row, "value", "ebay") or "ebay")
    with st.form("admin_listing_review_policy_form"):
        policy_required = st.checkbox(
            "Require two-person approval before active status",
            value=required_value,
        )
        policy_channels = st.text_input(
            "Policy Marketplaces (comma-separated)",
            value=channels_value,
            help="Example: ebay,facebook,whatnot",
        )
        save_policy = st.form_submit_button("Save Review Policy")
    if save_policy:
        try:
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="listing_review_two_person_required",
                value="true" if bool(policy_required) else "false",
                value_type="bool",
                description="Require a different user than reviewer when setting listing to active on configured channels.",
                is_active=True,
                actor=user.username,
            )
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="listing_review_two_person_channels_csv",
                value=(policy_channels or "").strip() or "ebay",
                value_type="str",
                description="Comma-separated marketplaces where two-person review policy applies.",
                is_active=True,
                actor=user.username,
            )
            st.success("Listing review policy saved.")
            st.rerun()
        except Exception as exc:
            st.error(f"Unable to save listing review policy: {exc}")


def _render_coin_paid_source_editor(repo: InventoryRepository, user) -> None:
    st.markdown("### Coin Paid Source Adapter")
    st.caption(
        "Optional paid-source contract for coin reference ingestion (for example Greysheet). "
        "Use only with approved licensing."
    )

    cfg = resolve_paid_coin_source_config(repo)
    adapter = resolve_paid_coin_source_adapter(repo)
    issues = adapter.validate()

    st.dataframe(
        pd.DataFrame(
            [
                {
                    "enabled": bool(cfg.enabled),
                    "provider": cfg.provider,
                    "base_url": cfg.base_url,
                    "api_key_configured": bool(cfg.api_key),
                    "license_acknowledged": bool(cfg.license_acknowledged),
                    "allow_prod": bool(cfg.allow_prod),
                    "environment": settings.app_env,
                }
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )
    if issues:
        st.warning("Current validation issues:")
        for issue in issues:
            st.write(f"- {issue}")
    else:
        st.success("Current paid-source configuration is valid.")

    with st.form("admin_coin_paid_source_form"):
        p1, p2 = st.columns(2)
        with p1:
            enabled = st.checkbox("Enable paid source adapter", value=bool(cfg.enabled))
        with p2:
            provider = st.selectbox(
                "Provider",
                options=["none", "greysheet"],
                index=1 if cfg.provider == "greysheet" else 0,
            )
        base_url = st.text_input("Base URL", value=cfg.base_url)
        api_key = st.text_input(
            "API Key/Token",
            value="",
            type="password",
            help="Leave blank to keep existing stored key.",
        )
        c1, c2 = st.columns(2)
        with c1:
            license_ack = st.checkbox(
                "I confirm we have required paid-source license approval",
                value=bool(cfg.license_acknowledged),
            )
        with c2:
            allow_prod = st.checkbox(
                "Allow in production environment",
                value=bool(cfg.allow_prod),
                help="Keep off for local/dev validation unless approved for production usage.",
            )
        a1, a2 = st.columns(2)
        with a1:
            save_paid = st.form_submit_button("Save Paid Source Settings")
        with a2:
            disable_paid = st.form_submit_button("Disable + Reset To Safe Defaults")

    if save_paid:
        try:
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="coin_ref_paid_source_enabled",
                value="true" if bool(enabled) else "false",
                value_type="bool",
                description="Enable optional paid coin-reference source adapter contract (disabled by default).",
                is_active=True,
                actor=user.username,
            )
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="coin_ref_paid_source_provider",
                value=str(provider or "none").strip().lower() or "none",
                value_type="str",
                description="Paid source provider key (`none`, `greysheet`).",
                is_active=True,
                actor=user.username,
            )
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="coin_ref_paid_source_base_url",
                value=(base_url or "").strip(),
                value_type="str",
                description="Paid source API base URL (if licensed/in use).",
                is_active=True,
                actor=user.username,
            )
            if (api_key or "").strip():
                api_key_value = api_key.strip()
            else:
                existing_key_row = repo.get_runtime_setting(
                    environment=settings.app_env,
                    key="coin_ref_paid_source_api_key",
                    active_only=False,
                )
                api_key_value = str(getattr(existing_key_row, "value", "") or "")
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="coin_ref_paid_source_api_key",
                value=api_key_value,
                value_type="str",
                description="Paid source API key/token (if licensed/in use).",
                is_active=True,
                actor=user.username,
            )
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="coin_ref_paid_source_license_ack",
                value="true" if bool(license_ack) else "false",
                value_type="bool",
                description="Set true only after legal/licensing approval for paid source usage.",
                is_active=True,
                actor=user.username,
            )
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="coin_ref_paid_source_allow_prod",
                value="true" if bool(allow_prod) else "false",
                value_type="bool",
                description="Allow paid source usage in production environment (separate guardrail).",
                is_active=True,
                actor=user.username,
            )
            st.success("Paid source adapter settings saved.")
            st.rerun()
        except Exception as exc:
            st.error(f"Unable to save paid source settings: {exc}")

    if disable_paid:
        try:
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="coin_ref_paid_source_enabled",
                value="false",
                value_type="bool",
                description="Enable optional paid coin-reference source adapter contract (disabled by default).",
                is_active=True,
                actor=user.username,
            )
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="coin_ref_paid_source_provider",
                value="none",
                value_type="str",
                description="Paid source provider key (`none`, `greysheet`).",
                is_active=True,
                actor=user.username,
            )
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="coin_ref_paid_source_license_ack",
                value="false",
                value_type="bool",
                description="Set true only after legal/licensing approval for paid source usage.",
                is_active=True,
                actor=user.username,
            )
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="coin_ref_paid_source_allow_prod",
                value="false",
                value_type="bool",
                description="Allow paid source usage in production environment (separate guardrail).",
                is_active=True,
                actor=user.username,
            )
            st.success("Paid source adapter reset to safe defaults.")
            st.rerun()
        except Exception as exc:
            st.error(f"Unable to reset paid source settings: {exc}")


def _render_voice_runtime_editor(repo: InventoryRepository, user) -> None:
    st.markdown("### Voice Runtime (STT/TTS)")
    st.caption("Configure speech-to-text and text-to-speech behavior for Ask GoldenStackers.")
    defaults = {
        "ai_voice_enabled": "false",
        "ai_voice_stt_enabled": "true",
        "ai_voice_tts_enabled": "false",
        "ai_voice_provider": "openai",
        "ai_voice_base_url": (settings.comp_llm_base_url or "https://api.openai.com/v1").strip().rstrip("/"),
        "ai_voice_api_key": settings.openai_api_key or "",
        "ai_voice_stt_model": "gpt-4o-mini-transcribe",
        "ai_voice_stt_language": "",
        "ai_voice_tts_model": "gpt-4o-mini-tts",
        "ai_voice_tts_voice": "alloy",
        "ai_voice_tts_response_format": "mp3",
        "ai_voice_timeout_seconds": "45",
        "ai_voice_tts_max_chars": "1400",
    }

    def _get_value(key: str) -> str:
        row = repo.get_runtime_setting(environment=settings.app_env, key=key, active_only=False)
        if row is None:
            return defaults[key]
        return str(row.value or "")

    with st.form("admin_voice_runtime_form"):
        v1, v2, v3 = st.columns(3)
        with v1:
            voice_enabled = st.checkbox(
                "Enable voice features",
                value=_get_value("ai_voice_enabled").strip().lower() in {"1", "true", "yes", "on"},
            )
        with v2:
            voice_stt_enabled = st.checkbox(
                "Enable STT (mic -> text)",
                value=_get_value("ai_voice_stt_enabled").strip().lower() in {"1", "true", "yes", "on"},
            )
        with v3:
            voice_tts_enabled = st.checkbox(
                "Enable TTS (assistant speech)",
                value=_get_value("ai_voice_tts_enabled").strip().lower() in {"1", "true", "yes", "on"},
            )
        p1, p2 = st.columns(2)
        with p1:
            voice_provider = st.selectbox(
                "Voice Provider",
                options=["openai", "localai"],
                index=0 if _get_value("ai_voice_provider").strip().lower() != "localai" else 1,
            )
        with p2:
            voice_base_url = st.text_input("Voice Base URL", value=_get_value("ai_voice_base_url"))
        voice_api_key = st.text_input(
            "Voice API Key/Token",
            value="",
            type="password",
            help="Leave blank to keep existing stored key.",
        )
        m1, m2, m3 = st.columns(3)
        with m1:
            voice_stt_model = st.text_input("STT Model", value=_get_value("ai_voice_stt_model"))
        with m2:
            voice_tts_model = st.text_input("TTS Model", value=_get_value("ai_voice_tts_model"))
        with m3:
            voice_tts_voice = st.text_input("TTS Voice", value=_get_value("ai_voice_tts_voice"))
        n1, n2, n3 = st.columns(3)
        with n1:
            voice_stt_language = st.text_input("STT Language (optional)", value=_get_value("ai_voice_stt_language"))
        with n2:
            voice_tts_format = st.selectbox(
                "TTS Format",
                options=["mp3", "wav"],
                index=0 if _get_value("ai_voice_tts_response_format").strip().lower() != "wav" else 1,
            )
        with n3:
            voice_timeout = st.number_input(
                "Voice Timeout Seconds",
                min_value=5,
                max_value=300,
                value=max(5, int(_get_value("ai_voice_timeout_seconds") or "45")),
                step=5,
            )
        voice_tts_max_chars = st.number_input(
            "TTS Max Chars per Response",
            min_value=200,
            max_value=8000,
            value=max(200, int(_get_value("ai_voice_tts_max_chars") or "1400")),
            step=100,
        )
        save_voice_runtime = st.form_submit_button("Save Voice Runtime Settings")
    st.caption(
        "LocalAI is supported when it exposes OpenAI-compatible audio endpoints "
        "(`.../audio/transcriptions`, `.../audio/speech`) and matching STT/TTS models."
    )
    if save_voice_runtime:
        try:
            upserts = [
                ("ai_voice_enabled", "true" if voice_enabled else "false", "bool", "Enable/disable voice features."),
                (
                    "ai_voice_stt_enabled",
                    "true" if voice_stt_enabled else "false",
                    "bool",
                    "Enable speech-to-text input in chat.",
                ),
                (
                    "ai_voice_tts_enabled",
                    "true" if voice_tts_enabled else "false",
                    "bool",
                    "Enable text-to-speech playback for assistant responses.",
                ),
                ("ai_voice_provider", voice_provider.strip().lower(), "str", "Voice provider id."),
                ("ai_voice_base_url", voice_base_url.strip().rstrip("/"), "str", "Voice provider base URL."),
                ("ai_voice_stt_model", voice_stt_model.strip(), "str", "Speech-to-text model."),
                ("ai_voice_stt_language", voice_stt_language.strip(), "str", "Optional STT language hint."),
                ("ai_voice_tts_model", voice_tts_model.strip(), "str", "Text-to-speech model."),
                ("ai_voice_tts_voice", voice_tts_voice.strip(), "str", "Text-to-speech voice id."),
                ("ai_voice_tts_response_format", voice_tts_format.strip().lower(), "str", "TTS response format."),
                ("ai_voice_timeout_seconds", str(int(voice_timeout)), "int", "Voice request timeout seconds."),
                ("ai_voice_tts_max_chars", str(int(voice_tts_max_chars)), "int", "Max chars for TTS synthesis."),
            ]
            if voice_api_key.strip():
                upserts.append(("ai_voice_api_key", voice_api_key.strip(), "str", "Voice provider API key/token."))
            for key, value, value_type, desc in upserts:
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key=key,
                    value=value,
                    value_type=value_type,
                    description=desc,
                    is_active=True,
                    actor=user.username,
                )
            st.success("Voice runtime settings saved.")
            st.rerun()
        except Exception as exc:
            st.error(f"Unable to save voice runtime settings: {exc}")


def _render_ai_domain_toggles_editor(repo: InventoryRepository, user) -> None:
    st.markdown("### AI Domain Toggles")
    st.caption("Enable/disable AI features by domain without redeploying.")

    def _is_enabled(key: str, default: bool = True) -> bool:
        row = repo.get_runtime_setting(environment=settings.app_env, key=key, active_only=False)
        if row is None:
            return bool(default)
        return str(row.value or "").strip().lower() in {"1", "true", "yes", "y", "on"}

    with st.form("admin_ai_domain_toggles_form"):
        d1, d2 = st.columns(2)
        with d1:
            chat_enabled = st.checkbox(
                "Ask GoldenStackers Chat",
                value=_is_enabled("ai_domain_chat_enabled", True),
            )
            comp_enabled = st.checkbox(
                "Comp Tool",
                value=_is_enabled("ai_domain_comp_tool_enabled", True),
            )
        with d2:
            grader_enabled = st.checkbox(
                "Coin Grader",
                value=_is_enabled("ai_domain_coin_grader_enabled", True),
            )
            identifier_enabled = st.checkbox(
                "Coin Identifier",
                value=_is_enabled("ai_domain_coin_identifier_enabled", True),
            )
        save_domains = st.form_submit_button("Save AI Domain Toggles")
    if save_domains:
        try:
            toggles = [
                ("ai_domain_chat_enabled", chat_enabled, "Enable/disable Ask GoldenStackers chat."),
                ("ai_domain_comp_tool_enabled", comp_enabled, "Enable/disable Comp Tool features."),
                ("ai_domain_coin_grader_enabled", grader_enabled, "Enable/disable Coin Grader features."),
                ("ai_domain_coin_identifier_enabled", identifier_enabled, "Enable/disable Coin Identifier features."),
            ]
            for key, enabled, desc in toggles:
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key=key,
                    value="true" if bool(enabled) else "false",
                    value_type="bool",
                    description=desc,
                    is_active=True,
                    actor=user.username,
                )
            st.success("AI domain toggles saved.")
            st.rerun()
        except Exception as exc:
            st.error(f"Unable to save AI domain toggles: {exc}")


def _runtime_setting_audit_history(repo: InventoryRepository, setting_id: int) -> list[dict]:
    rows = repo.list_audit_logs_for_entity(
        entity_type="runtime_setting",
        entity_id=setting_id,
        limit=500,
    )
    out: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(row.changes_json or "{}")
        except Exception:
            payload = {}
        value_before = None
        value_after = None
        if isinstance(payload, dict):
            if isinstance(payload.get("value"), dict):
                value_before = payload.get("value", {}).get("before")
                value_after = payload.get("value", {}).get("after")
            # Backward-compatible fallback if create payload later includes value under `after`.
            after_obj = payload.get("after") if isinstance(payload.get("after"), dict) else {}
            if value_after is None and "value" in after_obj:
                value_after = after_obj.get("value")
        out.append(
            {
                "audit_id": int(row.id),
                "created_at": row.created_at,
                "actor": row.actor,
                "action": row.action,
                "value_before": "" if value_before is None else str(value_before),
                "value_after": "" if value_after is None else str(value_after),
                "raw_changes": payload,
            }
        )
    return out


def _wipe_operational_data(
    repo: InventoryRepository,
    *,
    include_shipping_presets: bool,
    include_document_templates: bool,
    include_audit_logs: bool,
) -> dict[str, int]:
    targets: list[type] = [
        ReturnRecord,
        Sale,
        OrderItem,
        Order,
        MediaAsset,
        MarketplaceListing,
        ProductLotAssignment,
        InventoryMovement,
        Product,
        PurchaseLot,
        InventorySource,
    ]
    if include_shipping_presets:
        targets.append(ShippingPreset)
    if include_document_templates:
        targets.append(DocumentTemplateProfile)
    if include_audit_logs:
        targets.append(AuditLog)

    counts: dict[str, int] = {}
    for model in targets:
        deleted = repo.db.execute(delete(model)).rowcount or 0
        counts[model.__tablename__] = int(deleted)
    repo.db.commit()
    return counts


def _governance_snapshot_counts(
    repo: InventoryRepository,
    *,
    lookback_days: int,
    max_rows: int,
) -> dict[str, int]:
    cutoff = utcnow_naive() - timedelta(days=max(1, int(lookback_days)))
    capped_rows = max(100, min(10000, int(max_rows)))
    nav_count = len(
        repo.db.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "navigation",
                AuditLog.action.in_(["workspace_handoff_applied", "workspace_handoff_cleared"]),
                AuditLog.created_at >= cutoff,
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(int(capped_rows))
        ).all()
    )
    feedback_count = len(
        repo.db.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "workspace_feedback",
                AuditLog.created_at >= cutoff,
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(int(capped_rows))
        ).all()
    )
    parity_count = len(
        repo.db.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type.in_(["workspace_parity", "workspace_parity_decision", "workspace_followup"]),
                AuditLog.created_at >= cutoff,
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(int(capped_rows))
        ).all()
    )
    comp_count = len(
        repo.db.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type.in_(["comp_photo_retry", "comp_domain_recommendation"]),
                AuditLog.created_at >= cutoff,
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(int(capped_rows))
        ).all()
    )
    return {
        "handoff_events": int(nav_count),
        "workspace_feedback_events": int(feedback_count),
        "parity_followup_events": int(parity_count),
        "photo_comp_events": int(comp_count),
    }


def _record_governance_snapshot_event(
    repo: InventoryRepository,
    *,
    actor: str,
    lookback_days: int,
    max_rows: int,
    source: str,
    download_intent: bool = False,
) -> dict[str, int]:
    counts = _governance_snapshot_counts(repo, lookback_days=int(lookback_days), max_rows=int(max_rows))
    snapshot_time = utcnow_naive()
    repo.record_audit_event(
        entity_type="governance_export",
        entity_id=None,
        action="snapshot",
        actor=actor,
        changes={
            "environment": settings.app_env,
            "recorded_at": snapshot_time.isoformat(timespec="seconds"),
            "source": str(source or "").strip() or "admin",
            "scheduled": False,
            "lookback_days": int(lookback_days),
            "max_rows_per_scope": int(max_rows),
            "counts": counts,
            "download_intent": bool(download_intent),
        },
    )
    return counts


def _render_governance_exports_hub(repo: InventoryRepository, user) -> None:
    st.markdown("### Governance Exports")
    st.caption(
        "Centralized export hub for operations governance artifacts across handoffs, workspace feedback, parity/follow-ups, and photo-comp tuning."
    )
    c1, c2 = st.columns(2)
    with c1:
        lookback_days = st.number_input(
            "Lookback Days",
            min_value=1,
            max_value=365,
            value=30,
            step=1,
            key="admin_governance_export_hub_lookback_days",
        )
    with c2:
        max_rows = st.number_input(
            "Max Rows Per Scope",
            min_value=100,
            max_value=10000,
            value=2000,
            step=100,
            key="admin_governance_export_hub_max_rows",
        )

    cutoff = utcnow_naive() - timedelta(days=int(lookback_days))
    nav_logs = repo.db.scalars(
        select(AuditLog)
        .where(
            AuditLog.entity_type == "navigation",
            AuditLog.action.in_(["workspace_handoff_applied", "workspace_handoff_cleared"]),
            AuditLog.created_at >= cutoff,
        )
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(int(max_rows))
    ).all()
    feedback_logs = repo.db.scalars(
        select(AuditLog)
        .where(
            AuditLog.entity_type == "workspace_feedback",
            AuditLog.created_at >= cutoff,
        )
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(int(max_rows))
    ).all()
    parity_logs = repo.db.scalars(
        select(AuditLog)
        .where(
            AuditLog.entity_type.in_(["workspace_parity", "workspace_parity_decision", "workspace_followup"]),
            AuditLog.created_at >= cutoff,
        )
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(int(max_rows))
    ).all()
    comp_logs = repo.db.scalars(
        select(AuditLog)
        .where(
            AuditLog.entity_type.in_(["comp_photo_retry", "comp_domain_recommendation"]),
            AuditLog.created_at >= cutoff,
        )
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(int(max_rows))
    ).all()

    def _rows(logs: list[AuditLog]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in logs:
            payload = _audit_changes(row)
            out.append(
                {
                    "id": int(row.id),
                    "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
                    "entity_type": str(row.entity_type or ""),
                    "action": str(row.action or ""),
                    "actor": str(row.actor or ""),
                    "entity_id": int(row.entity_id) if row.entity_id is not None else "",
                    "payload_json": json.dumps(payload, ensure_ascii=True)[:2000],
                }
            )
        return out

    nav_df = pd.DataFrame(_rows(nav_logs))
    feedback_df = pd.DataFrame(_rows(feedback_logs))
    parity_df = pd.DataFrame(_rows(parity_logs))
    comp_df = pd.DataFrame(_rows(comp_logs))

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Handoff Events", int(len(nav_df)))
    m2.metric("Workspace Feedback Events", int(len(feedback_df)))
    m3.metric("Parity/Follow-up Events", int(len(parity_df)))
    m4.metric("Photo-Comp Governance Events", int(len(comp_df)))

    with st.expander("Preview Export Coverage", expanded=False):
        p1, p2 = st.columns(2)
        with p1:
            st.caption("Handoff events")
            st.dataframe(nav_df.head(20), use_container_width=True, hide_index=True)
        with p2:
            st.caption("Workspace feedback")
            st.dataframe(feedback_df.head(20), use_container_width=True, hide_index=True)
        p3, p4 = st.columns(2)
        with p3:
            st.caption("Parity + follow-up")
            st.dataframe(parity_df.head(20), use_container_width=True, hide_index=True)
        with p4:
            st.caption("Photo-comp governance")
            st.dataframe(comp_df.head(20), use_container_width=True, hide_index=True)

    all_bundle_buffer = BytesIO()
    with zipfile.ZipFile(all_bundle_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as bundle_zip:
        metadata_df = pd.DataFrame(
            [
                {
                    "environment": settings.app_env,
                    "lookback_days": int(lookback_days),
                    "max_rows_per_scope": int(max_rows),
                    "generated_at_utc": utcnow_naive().isoformat(),
                    "generated_by": str(user.username or ""),
                    "handoff_events": int(len(nav_df)),
                    "workspace_feedback_events": int(len(feedback_df)),
                    "parity_followup_events": int(len(parity_df)),
                    "photo_comp_events": int(len(comp_df)),
                }
            ]
        )
        bundle_zip.writestr("governance_metadata.csv", metadata_df.to_csv(index=False))
        bundle_zip.writestr("handoff_events.csv", nav_df.to_csv(index=False))
        bundle_zip.writestr("workspace_feedback_events.csv", feedback_df.to_csv(index=False))
        bundle_zip.writestr("parity_followup_events.csv", parity_df.to_csv(index=False))
        bundle_zip.writestr("photo_comp_governance_events.csv", comp_df.to_csv(index=False))
    all_bundle_buffer.seek(0)
    st.download_button(
        "Export All Governance Bundles (ZIP)",
        data=all_bundle_buffer.getvalue(),
        file_name=(
            f"governance_exports_bundle_{settings.app_env}_"
            f"{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.zip"
        ),
        mime="application/zip",
        key="admin_governance_export_hub_all_zip_btn",
    )

    split1, split2, split3, split4 = st.columns(4)
    with split1:
        st.download_button(
            "Download Handoff CSV",
            data=nav_df.to_csv(index=False).encode("utf-8"),
            file_name=f"governance_handoff_{settings.app_env}_{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
            key="admin_governance_export_hub_handoff_csv_btn",
        )
    with split2:
        st.download_button(
            "Download Feedback CSV",
            data=feedback_df.to_csv(index=False).encode("utf-8"),
            file_name=f"governance_feedback_{settings.app_env}_{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
            key="admin_governance_export_hub_feedback_csv_btn",
        )
    with split3:
        st.download_button(
            "Download Parity CSV",
            data=parity_df.to_csv(index=False).encode("utf-8"),
            file_name=f"governance_parity_{settings.app_env}_{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
            key="admin_governance_export_hub_parity_csv_btn",
        )
    with split4:
        st.download_button(
            "Download Photo-Comp CSV",
            data=comp_df.to_csv(index=False).encode("utf-8"),
            file_name=f"governance_photo_comp_{settings.app_env}_{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
            key="admin_governance_export_hub_comp_csv_btn",
        )

    st.markdown("#### Go-Live Evidence Pack")
    st.caption(
        "One-click bundle for release readiness review: governance exports, alert evidence, queue snapshot, and checklist snapshot."
    )
    now_ts = utcnow_naive()
    try:
        alembic_version = str(repo.db.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).scalar_one())
    except Exception:
        alembic_version = "unknown"
    env_values = read_env_file(".env")
    env_defaults = read_env_file(".env.example")
    req_env = required_env_keys()
    env_missing = [
        key
        for key in sorted(req_env)
        if key not in env_values or not str(env_values.get(key, "")).strip()
    ]
    runtime_rows = repo.list_runtime_settings(environment=settings.app_env, active_only=False)
    by_key = {str(row.key): row for row in runtime_rows}
    req_runtime = required_runtime_keys()
    runtime_missing = [
        key
        for key in sorted(req_runtime)
        if key not in by_key or not bool(getattr(by_key[key], "is_active", False))
    ]
    untracked_env_keys = sorted([k for k in env_values.keys() if k not in env_defaults])

    alert_rows_raw = repo.db.execute(
        text(
            """
            SELECT created_at, actor, action, changes_json
            FROM audit_logs
            WHERE entity_type = 'integration_event'
              AND created_at >= :since
            ORDER BY created_at DESC
            LIMIT 4000
            """
        ),
        {"since": now_ts - timedelta(days=7)},
    ).all()
    critical_alert_evidence_rows: list[dict[str, Any]] = []
    for created_at, actor, action, changes_json in alert_rows_raw:
        try:
            payload = json.loads(str(changes_json or "{}"))
        except Exception:
            payload = {}
        after = payload.get("after") if isinstance(payload, dict) else {}
        if not isinstance(after, dict):
            continue
        integration_name = str(after.get("integration") or "").strip().lower()
        action_name = str(after.get("action") or action or "").strip().lower()
        status = str(after.get("status") or "").strip().lower()
        details = after.get("details") if isinstance(after.get("details"), dict) else {}
        is_health_alert = (
            integration_name == "system_health"
            and action_name in {"critical_signal_alert", "critical_signal_alert_manual"}
        )
        is_slack_dispatch = (
            integration_name == "slack"
            and action_name == "dispatch_system_health_critical"
        )
        if not (is_health_alert or is_slack_dispatch):
            continue
        critical_alert_evidence_rows.append(
            {
                "created_at": str(created_at or ""),
                "actor": str(actor or ""),
                "integration": integration_name,
                "action": action_name,
                "status": status,
                "critical_signals": ", ".join(str(x) for x in (details.get("critical_signals") or [])),
                "channel": str(details.get("channel") or ""),
                "queue_job_id": str(details.get("queue_job_id") or ""),
                "dispatch_mode": (
                    "queued"
                    if bool(details.get("queued")) or str(status) == "queued"
                    else ("sent" if str(status) == "success" else str(status or ""))
                ),
                "error": str(details.get("error") or "")[:200],
            }
        )
    critical_alert_evidence_df = pd.DataFrame(critical_alert_evidence_rows)

    provider_validation_rows_raw = repo.db.execute(
        text(
            """
            SELECT created_at, actor, action, changes_json
            FROM audit_logs
            WHERE entity_type = 'integration_event'
              AND created_at >= :since
            ORDER BY created_at DESC
            LIMIT 4000
            """
        ),
        {"since": now_ts - timedelta(days=30)},
    ).all()
    provider_validation_rows: list[dict[str, Any]] = []
    for created_at, actor, action, changes_json in provider_validation_rows_raw:
        try:
            payload = json.loads(str(changes_json or "{}"))
        except Exception:
            payload = {}
        after = payload.get("after") if isinstance(payload, dict) else {}
        if not isinstance(after, dict):
            continue
        if str(after.get("integration") or "").strip().lower() != "shipping_provider_validation":
            continue
        details = after.get("details") if isinstance(after.get("details"), dict) else {}
        provider_validation_rows.append(
            {
                "created_at": str(created_at or ""),
                "actor": str(actor or ""),
                "action": str(after.get("action") or action or ""),
                "status": str(after.get("status") or ""),
                "target_env": str(details.get("target_env") or ""),
                "provider": str(details.get("provider") or ""),
                "sale_id": details.get("sale_id"),
                "queue_job_id": details.get("queue_job_id"),
                "queue_status": str(details.get("queue_status") or ""),
                "label_id": str(details.get("label_id") or ""),
                "tracking_number": str(details.get("tracking_number") or ""),
                "message": str(details.get("message") or ""),
                "validation_notes": str(details.get("validation_notes") or ""),
                "error": str(details.get("error") or "")[:220],
            }
        )
    provider_validation_df = pd.DataFrame(provider_validation_rows)
    provider_signoff_logs = repo.db.scalars(
        select(AuditLog)
        .where(AuditLog.entity_type == "shipping_provider_validation_signoff")
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(500)
    ).all()
    provider_signoff_rows: list[dict[str, Any]] = []
    latest_signoff_by_env: dict[str, dict[str, Any]] = {}
    for row in provider_signoff_logs:
        payload = _audit_changes(row)
        target_env = str(payload.get("target_env") or "").strip().lower()
        entry = {
            "recorded_at_utc": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
            "actor": str(row.actor or ""),
            "target_env": target_env,
            "signoff_date": str(payload.get("signoff_date") or ""),
            "owner": str(payload.get("owner") or ""),
            "status": str(payload.get("status") or "").strip().lower(),
            "evidence_link": str(payload.get("evidence_link") or ""),
            "notes": str(payload.get("notes") or "")[:220],
        }
        provider_signoff_rows.append(entry)
        if target_env and target_env not in latest_signoff_by_env:
            latest_signoff_by_env[target_env] = entry
    provider_signoff_df = pd.DataFrame(provider_signoff_rows)
    provider_signoff_dev_status = str((latest_signoff_by_env.get("dev") or {}).get("status") or "")
    provider_signoff_prod_status = str((latest_signoff_by_env.get("prod") or {}).get("status") or "")
    provider_signoff_dev_ready = provider_signoff_dev_status == "approved"
    provider_signoff_prod_ready = provider_signoff_prod_status == "approved"

    health_calibration_logs = repo.db.scalars(
        select(AuditLog)
        .where(AuditLog.entity_type == "system_health_calibration_signoff")
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(500)
    ).all()
    health_calibration_rows: list[dict[str, Any]] = []
    latest_health_calibration_by_env: dict[str, dict[str, Any]] = {}
    for row in health_calibration_logs:
        payload = _audit_changes(row)
        target_env = str(payload.get("target_env") or "").strip().lower()
        entry = {
            "recorded_at_utc": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
            "actor": str(row.actor or ""),
            "target_env": target_env,
            "signoff_date": str(payload.get("signoff_date") or ""),
            "owner": str(payload.get("owner") or ""),
            "status": str(payload.get("status") or "").strip().lower(),
            "evidence_link": str(payload.get("evidence_link") or ""),
            "notes": str(payload.get("notes") or "")[:220],
        }
        health_calibration_rows.append(entry)
        if target_env and target_env not in latest_health_calibration_by_env:
            latest_health_calibration_by_env[target_env] = entry
    health_calibration_df = pd.DataFrame(health_calibration_rows)
    health_calibration_dev_status = str((latest_health_calibration_by_env.get("dev") or {}).get("status") or "")
    health_calibration_prod_status = str((latest_health_calibration_by_env.get("prod") or {}).get("status") or "")
    health_calibration_dev_ready = health_calibration_dev_status == "approved"
    health_calibration_prod_ready = health_calibration_prod_status == "approved"

    health_alert_routing_logs = repo.db.scalars(
        select(AuditLog)
        .where(AuditLog.entity_type == "system_health_alert_routing_signoff")
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(500)
    ).all()
    health_alert_routing_rows: list[dict[str, Any]] = []
    latest_health_alert_routing_by_env: dict[str, dict[str, Any]] = {}
    for row in health_alert_routing_logs:
        payload = _audit_changes(row)
        target_env = str(payload.get("target_env") or "").strip().lower()
        entry = {
            "recorded_at_utc": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
            "actor": str(row.actor or ""),
            "target_env": target_env,
            "signoff_date": str(payload.get("signoff_date") or ""),
            "owner": str(payload.get("owner") or ""),
            "status": str(payload.get("status") or "").strip().lower(),
            "evidence_link": str(payload.get("evidence_link") or ""),
            "channel_routing_confirmed": bool(payload.get("channel_routing_confirmed")),
            "owner_confirmed": bool(payload.get("owner_confirmed")),
            "escalation_confirmed": bool(payload.get("escalation_confirmed")),
            "runbook_confirmed": bool(payload.get("runbook_confirmed")),
            "notes": str(payload.get("notes") or "")[:220],
        }
        health_alert_routing_rows.append(entry)
        if target_env and target_env not in latest_health_alert_routing_by_env:
            latest_health_alert_routing_by_env[target_env] = entry
    health_alert_routing_df = pd.DataFrame(health_alert_routing_rows)
    health_alert_routing_dev_status = str((latest_health_alert_routing_by_env.get("dev") or {}).get("status") or "")
    health_alert_routing_prod_status = str((latest_health_alert_routing_by_env.get("prod") or {}).get("status") or "")
    health_alert_routing_dev_ready = health_alert_routing_dev_status == "approved"
    health_alert_routing_prod_ready = health_alert_routing_prod_status == "approved"

    automation_hardening_logs = repo.db.scalars(
        select(AuditLog)
        .where(AuditLog.entity_type == "integration_automation_hardening_signoff")
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(500)
    ).all()
    automation_hardening_rows: list[dict[str, Any]] = []
    latest_automation_hardening_by_env: dict[str, dict[str, Any]] = {}
    for row in automation_hardening_logs:
        payload = _audit_changes(row)
        target_env = str(payload.get("target_env") or "").strip().lower()
        entry = {
            "recorded_at_utc": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
            "actor": str(row.actor or ""),
            "target_env": target_env,
            "signoff_date": str(payload.get("signoff_date") or ""),
            "owner": str(payload.get("owner") or ""),
            "status": str(payload.get("status") or "").strip().lower(),
            "guardrails_verified": bool(payload.get("guardrails_verified")),
            "approval_policy_reviewed": bool(payload.get("approval_policy_reviewed")),
            "runbook_signed_off": bool(payload.get("runbook_signed_off")),
            "evidence_link": str(payload.get("evidence_link") or ""),
            "notes": str(payload.get("notes") or "")[:220],
        }
        automation_hardening_rows.append(entry)
        if target_env and target_env not in latest_automation_hardening_by_env:
            latest_automation_hardening_by_env[target_env] = entry
    automation_hardening_df = pd.DataFrame(automation_hardening_rows)
    automation_hardening_dev_status = str((latest_automation_hardening_by_env.get("dev") or {}).get("status") or "")
    automation_hardening_prod_status = str((latest_automation_hardening_by_env.get("prod") or {}).get("status") or "")
    automation_hardening_dev_ready = automation_hardening_dev_status == "approved"
    automation_hardening_prod_ready = automation_hardening_prod_status == "approved"

    restore_drill_logs = repo.db.scalars(
        select(AuditLog)
        .where(AuditLog.entity_type == "backup_restore_drill")
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(1000)
    ).all()
    restore_drill_rows: list[dict[str, Any]] = []
    for row in restore_drill_logs:
        payload = _audit_changes(row)
        created_at = row.created_at
        result = str(payload.get("result") or "").strip().lower()
        duration_minutes = payload.get("duration_minutes")
        rto_target_minutes = payload.get("rto_target_minutes")
        duration_int = int(duration_minutes) if str(duration_minutes or "").isdigit() else None
        rto_target_int = int(rto_target_minutes) if str(rto_target_minutes or "").isdigit() else None
        restore_drill_rows.append(
            {
                "id": int(row.id),
                "recorded_at_utc": created_at.isoformat(timespec="seconds") if created_at else "",
                "actor": str(row.actor or ""),
                "target_env": str(payload.get("target_env") or ""),
                "drill_date": str(payload.get("drill_date") or ""),
                "result": result,
                "source_type": str(payload.get("source_type") or ""),
                "source_ref": str(payload.get("source_ref") or ""),
                "duration_minutes": duration_int,
                "rto_target_minutes": rto_target_int,
                "rto_met": payload.get("rto_met"),
                "notes": str(payload.get("notes") or "")[:220],
            }
        )
    restore_drill_df = pd.DataFrame(restore_drill_rows)
    restore_drill_180d_count = 0
    restore_drill_180d_pass_count = 0
    restore_drill_last_at = ""
    restore_drill_last_result = ""
    restore_drill_last_pass_age_days: int | None = None
    since_restore_window = now_ts - timedelta(days=180)
    for row in restore_drill_logs:
        if row.created_at is None:
            continue
        if row.created_at >= since_restore_window:
            restore_drill_180d_count += 1
            payload = _audit_changes(row)
            if str(payload.get("result") or "").strip().lower() == "pass":
                restore_drill_180d_pass_count += 1
    if restore_drill_logs:
        latest_row = restore_drill_logs[0]
        latest_payload = _audit_changes(latest_row)
        restore_drill_last_at = latest_row.created_at.isoformat(timespec="seconds") if latest_row.created_at else ""
        restore_drill_last_result = str(latest_payload.get("result") or "").strip().lower()
    for row in restore_drill_logs:
        payload = _audit_changes(row)
        if str(payload.get("result") or "").strip().lower() != "pass":
            continue
        if row.created_at is None:
            continue
        restore_drill_last_pass_age_days = max(0, (now_ts - row.created_at).days)
        break

    go_live_signoff_logs = repo.db.scalars(
        select(AuditLog)
        .where(AuditLog.entity_type == "go_live_section_signoff")
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(2000)
    ).all()
    go_live_signoff_rows: list[dict[str, Any]] = []
    latest_go_live_signoff_by_key: dict[str, dict[str, Any]] = {}
    for row in go_live_signoff_logs:
        payload = _audit_changes(row)
        section_key = str(payload.get("section_key") or "").strip()
        item_key = str(payload.get("item_key") or "").strip()
        composite_key = f"{section_key}::{item_key}" if section_key and item_key else ""
        entry = {
            "recorded_at_utc": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
            "actor": str(row.actor or ""),
            "section_key": section_key,
            "item_key": item_key,
            "status": str(payload.get("status") or "").strip().lower(),
            "owner": str(payload.get("owner") or ""),
            "evidence_link": str(payload.get("evidence_link") or ""),
            "signoff_date": str(payload.get("signoff_date") or ""),
            "notes": str(payload.get("notes") or "")[:220],
        }
        go_live_signoff_rows.append(entry)
        if composite_key and composite_key not in latest_go_live_signoff_by_key:
            latest_go_live_signoff_by_key[composite_key] = entry
    go_live_signoff_df = pd.DataFrame(go_live_signoff_rows)
    go_live_signoff_total = len(latest_go_live_signoff_by_key)
    go_live_signoff_approved = sum(
        1 for row in latest_go_live_signoff_by_key.values() if str(row.get("status") or "").strip().lower() == "approved"
    )
    legal_policy_catalog: list[tuple[str, str]] = [
        ("tax_treatment", "Tax treatment policy"),
        ("record_retention", "Invoice/receipt retention policy"),
        ("marketplace_policy", "Marketplace policy conformance"),
        ("privacy_data_handling", "Privacy/data handling policy"),
        ("financial_posting_role_controls", "Financial posting role controls"),
        ("legal_accounting_reviewer", "Legal/accounting reviewer sign-off"),
    ]
    legal_signoff_logs = repo.db.scalars(
        select(AuditLog)
        .where(AuditLog.entity_type == "commerce_legal_signoff")
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(1200)
    ).all()
    legal_signoff_rows: list[dict[str, Any]] = []
    latest_legal_signoff_by_key: dict[str, dict[str, Any]] = {}
    for row in legal_signoff_logs:
        payload = _audit_changes(row)
        target_env = str(payload.get("target_env") or "").strip().lower()
        policy_key = str(payload.get("policy_key") or "").strip().lower()
        composite_key = f"{target_env}::{policy_key}" if target_env and policy_key else ""
        entry = {
            "recorded_at_utc": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
            "actor": str(row.actor or ""),
            "target_env": target_env,
            "policy_key": policy_key,
            "policy_label": str(payload.get("policy_label") or ""),
            "status": str(payload.get("status") or "").strip().lower(),
            "owner": str(payload.get("owner") or ""),
            "signoff_date": str(payload.get("signoff_date") or ""),
            "evidence_link": str(payload.get("evidence_link") or ""),
            "notes": str(payload.get("notes") or "")[:220],
        }
        legal_signoff_rows.append(entry)
        if composite_key and composite_key not in latest_legal_signoff_by_key:
            latest_legal_signoff_by_key[composite_key] = entry
    legal_signoff_df = pd.DataFrame(legal_signoff_rows)
    legal_signoff_total = len(latest_legal_signoff_by_key)
    legal_signoff_approved = sum(
        1 for row in latest_legal_signoff_by_key.values() if str(row.get("status") or "").strip().lower() == "approved"
    )
    legal_required_total_prod = len(legal_policy_catalog)
    legal_approved_prod = sum(
        1
        for policy_key, _label in legal_policy_catalog
        if str(
            (latest_legal_signoff_by_key.get(f"prod::{policy_key}") or {}).get("status") or ""
        ).strip().lower()
        == "approved"
    )
    legal_ready_prod = legal_required_total_prod > 0 and legal_approved_prod >= legal_required_total_prod

    dr_checklist_logs = repo.db.scalars(
        select(AuditLog)
        .where(AuditLog.entity_type == "backup_dr_checklist")
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(1000)
    ).all()
    dr_checklist_df = pd.DataFrame(
        [
            {
                "id": int(row.id),
                "recorded_at_utc": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
                "actor": str(row.actor or ""),
                "target_env": str((_audit_changes(row).get("target_env") or "")),
                "owner": str((_audit_changes(row).get("owner") or "")),
                "evidence_link": str((_audit_changes(row).get("evidence_link") or "")),
                "completed_count": (_audit_changes(row).get("completed_count")),
                "total_count": (_audit_changes(row).get("total_count")),
                "completion_percent": (_audit_changes(row).get("completion_percent")),
            }
            for row in dr_checklist_logs
        ]
    )
    dr_checklist_180d_count = 0
    dr_checklist_latest_completion_pct: float | None = None
    since_checklist_window = now_ts - timedelta(days=180)
    for row in dr_checklist_logs:
        if row.created_at is None:
            continue
        if row.created_at >= since_checklist_window:
            dr_checklist_180d_count += 1
    if dr_checklist_logs:
        latest_payload = _audit_changes(dr_checklist_logs[0])
        try:
            dr_checklist_latest_completion_pct = float(latest_payload.get("completion_percent"))
        except Exception:
            dr_checklist_latest_completion_pct = None

    integration_event_24h_rows = repo.db.execute(
        text(
            """
            SELECT created_at, actor, action, changes_json
            FROM audit_logs
            WHERE entity_type = 'integration_event'
              AND created_at >= :since
            ORDER BY created_at DESC
            LIMIT 4000
            """
        ),
        {"since": now_ts - timedelta(hours=24)},
    ).all()
    signal_counts = {"queue_execute_exceptions": 0, "terminal_queue_failures": 0, "integration_warnings": 0}
    signal_samples: list[dict[str, Any]] = []
    for created_at, actor, action, changes_json in integration_event_24h_rows:
        try:
            payload = json.loads(str(changes_json or "{}"))
        except Exception:
            payload = {}
        after = payload.get("after") if isinstance(payload, dict) else {}
        if not isinstance(after, dict):
            continue
        status = str(after.get("status") or "").strip().lower()
        details = after.get("details") if isinstance(after.get("details"), dict) else {}
        if action and str(action).endswith("_execute_exception"):
            signal_counts["queue_execute_exceptions"] += 1
        if status == "failed":
            signal_counts["terminal_queue_failures"] += 1
        if status == "warning":
            signal_counts["integration_warnings"] += 1
        if status in {"error", "failed", "warning"} and len(signal_samples) < 200:
            signal_samples.append(
                {
                    "created_at": str(created_at or ""),
                    "actor": str(actor or ""),
                    "action": str(action or ""),
                    "status": status,
                    "integration": str(after.get("integration") or ""),
                    "error": str(details.get("error") or "")[:200],
                }
            )
    signal_counts_df = pd.DataFrame(
        [
            {"metric": "queue_execute_exceptions_24h", "count": int(signal_counts["queue_execute_exceptions"])},
            {"metric": "terminal_queue_failures_24h", "count": int(signal_counts["terminal_queue_failures"])},
            {"metric": "integration_warnings_24h", "count": int(signal_counts["integration_warnings"])},
        ]
    )
    signal_samples_df = pd.DataFrame(signal_samples)

    queue_rows = repo.db.scalars(
        select(IntegrationQueueJob)
        .where(IntegrationQueueJob.environment == settings.app_env)
        .order_by(IntegrationQueueJob.next_attempt_at.asc(), IntegrationQueueJob.id.desc())
        .limit(5000)
    ).all()
    queue_df = pd.DataFrame(
        [
            {
                "id": int(row.id),
                "environment": str(row.environment or ""),
                "integration": str(row.integration or ""),
                "action": str(row.action or ""),
                "status": str(row.status or ""),
                "retry_count": int(row.retry_count or 0),
                "max_retries": int(row.max_retries or 0),
                "next_attempt_at": row.next_attempt_at.isoformat() if row.next_attempt_at else "",
                "last_attempt_at": row.last_attempt_at.isoformat() if row.last_attempt_at else "",
                "completed_at": row.completed_at.isoformat() if row.completed_at else "",
                "last_error": str(row.last_error or "")[:500],
                "requested_by": str(row.requested_by or ""),
                "updated_by": str(row.updated_by or ""),
            }
            for row in queue_rows
        ]
    )

    missing_required_df = pd.DataFrame(
        [
            {"type": "required_env_missing", "key": key}
            for key in env_missing
        ]
        + [
            {"type": "required_runtime_missing_or_inactive", "key": key}
            for key in runtime_missing
        ]
    )
    env_untracked_df = pd.DataFrame([{"key": key} for key in untracked_env_keys])
    try:
        checklist_text = Path("GO_LIVE_CHECKLIST.md").read_text(encoding="utf-8")
    except Exception:
        checklist_text = "Unable to read GO_LIVE_CHECKLIST.md from workspace."
    checklist_match_rows = re.findall(r"^\s*-\s*\[( |~|x)\]\s+(.*)$", checklist_text, flags=re.MULTILINE)
    checklist_total = len(checklist_match_rows)
    checklist_done = sum(1 for state, _label in checklist_match_rows if str(state).lower() == "x")
    checklist_in_progress = sum(1 for state, _label in checklist_match_rows if str(state) == "~")
    checklist_not_started = sum(1 for state, _label in checklist_match_rows if str(state) == " ")
    checklist_completion_pct = (
        (float(checklist_done) / float(checklist_total) * 100.0) if checklist_total > 0 else 0.0
    )
    checklist_status_df = pd.DataFrame(
        [
            {
                "total_items": int(checklist_total),
                "done_items": int(checklist_done),
                "in_progress_items": int(checklist_in_progress),
                "not_started_items": int(checklist_not_started),
                "completion_percent": round(float(checklist_completion_pct), 2),
            }
        ]
    )

    gl1, gl2, gl3, gl4 = st.columns(4)
    gl1.metric("Checklist Total", int(checklist_total))
    gl2.metric("Checklist Done", int(checklist_done))
    gl3.metric("Checklist In Progress", int(checklist_in_progress))
    gl4.metric("Checklist Completion", f"{checklist_completion_pct:.1f}%")
    gd1, gd2, gd3 = st.columns(3)
    gd1.metric("Restore Drills (180d)", int(restore_drill_180d_count))
    gd2.metric("Restore Drill Passes (180d)", int(restore_drill_180d_pass_count))
    gd3.metric("Latest Pass Age (days)", "n/a" if restore_drill_last_pass_age_days is None else int(restore_drill_last_pass_age_days))
    gs1, gs2 = st.columns(2)
    gs1.metric("Go-Live Sign-Off Items", int(go_live_signoff_total))
    gs2.metric("Go-Live Sign-Off Approved", int(go_live_signoff_approved))
    gls1, gls2 = st.columns(2)
    gls1.metric("Legal Sign-Off Items", int(legal_signoff_total))
    gls2.metric("Legal Sign-Off Approved", int(legal_signoff_approved))
    gls3, gls4 = st.columns(2)
    gls3.metric("Legal Approved (Prod)", f"{int(legal_approved_prod)}/{int(legal_required_total_prod)}")
    gls4.metric("Legal Ready (Prod)", "yes" if legal_ready_prod else "no")
    pv1, pv2 = st.columns(2)
    pv1.metric("Validation Sign-Off Dev", provider_signoff_dev_status or "missing")
    pv2.metric("Validation Sign-Off Prod", provider_signoff_prod_status or "missing")
    ph1, ph2 = st.columns(2)
    ph1.metric("Health Calibration Dev", health_calibration_dev_status or "missing")
    ph2.metric("Health Calibration Prod", health_calibration_prod_status or "missing")
    pr1, pr2 = st.columns(2)
    pr1.metric("Alert Routing Dev", health_alert_routing_dev_status or "missing")
    pr2.metric("Alert Routing Prod", health_alert_routing_prod_status or "missing")
    pa1, pa2 = st.columns(2)
    pa1.metric("Automation Hardening Dev", automation_hardening_dev_status or "missing")
    pa2.metric("Automation Hardening Prod", automation_hardening_prod_status or "missing")
    with st.expander("Readiness Scoring Config", expanded=False):
        sc1, sc2, sc3 = st.columns(3)
        with sc1:
            score_weight_checklist_gap_pct = st.number_input(
                "Checklist Gap Weight %",
                min_value=0,
                max_value=100,
                value=max(0, min(100, int(get_runtime_int(repo, "go_live_readiness_weight_checklist_gap_pct", 40)))),
                step=1,
                key="admin_go_live_score_weight_checklist_gap_pct",
            )
            score_weight_env_missing = st.number_input(
                "Per Env Missing Penalty",
                min_value=0,
                max_value=50,
                value=max(0, min(50, int(get_runtime_int(repo, "go_live_readiness_weight_env_missing", 5)))),
                step=1,
                key="admin_go_live_score_weight_env_missing",
            )
            score_weight_runtime_missing = st.number_input(
                "Per Runtime Missing Penalty",
                min_value=0,
                max_value=50,
                value=max(0, min(50, int(get_runtime_int(repo, "go_live_readiness_weight_runtime_missing", 5)))),
                step=1,
                key="admin_go_live_score_weight_runtime_missing",
            )
            score_weight_terminal_queue_failure = st.number_input(
                "Per Terminal Failure Penalty",
                min_value=0,
                max_value=50,
                value=max(
                    0,
                    min(50, int(get_runtime_int(repo, "go_live_readiness_weight_terminal_queue_failure", 10))),
                ),
                step=1,
                key="admin_go_live_score_weight_terminal_queue_failure",
            )
        with sc2:
            score_weight_queue_execute_exception = st.number_input(
                "Per Execute Exception Penalty",
                min_value=0,
                max_value=50,
                value=max(
                    0,
                    min(50, int(get_runtime_int(repo, "go_live_readiness_weight_queue_execute_exception", 5))),
                ),
                step=1,
                key="admin_go_live_score_weight_queue_execute_exception",
            )
            score_penalty_terminal_queue_failure_max = st.number_input(
                "Terminal Failure Penalty Cap",
                min_value=0,
                max_value=100,
                value=max(
                    0,
                    min(100, int(get_runtime_int(repo, "go_live_readiness_penalty_terminal_queue_failure_max", 30))),
                ),
                step=1,
                key="admin_go_live_score_penalty_terminal_queue_failure_max",
            )
            score_penalty_queue_execute_exception_max = st.number_input(
                "Execute Exception Penalty Cap",
                min_value=0,
                max_value=100,
                value=max(
                    0,
                    min(100, int(get_runtime_int(repo, "go_live_readiness_penalty_queue_execute_exception_max", 20))),
                ),
                step=1,
                key="admin_go_live_score_penalty_queue_execute_exception_max",
            )
            score_penalty_integration_warnings_warn = st.number_input(
                "Warnings Penalty (Warn)",
                min_value=0,
                max_value=100,
                value=max(
                    0,
                    min(100, int(get_runtime_int(repo, "go_live_readiness_penalty_integration_warnings_warn", 10))),
                ),
                step=1,
                key="admin_go_live_score_penalty_warnings_warn",
            )
        with sc3:
            score_penalty_integration_warnings_critical = st.number_input(
                "Warnings Penalty (Critical)",
                min_value=0,
                max_value=100,
                value=max(
                    0,
                    min(
                        100,
                        int(get_runtime_int(repo, "go_live_readiness_penalty_integration_warnings_critical", 20)),
                    ),
                ),
                step=1,
                key="admin_go_live_score_penalty_warnings_critical",
            )
            score_threshold_green = st.number_input(
                "Green Threshold",
                min_value=0,
                max_value=100,
                value=max(0, min(100, int(get_runtime_int(repo, "go_live_readiness_threshold_green", 85)))),
                step=1,
                key="admin_go_live_score_threshold_green",
            )
            score_threshold_yellow = st.number_input(
                "Yellow Threshold",
                min_value=0,
                max_value=100,
                value=max(0, min(100, int(get_runtime_int(repo, "go_live_readiness_threshold_yellow", 65)))),
                step=1,
                key="admin_go_live_score_threshold_yellow",
            )
        if st.button("Save Readiness Scoring Config", key="admin_go_live_score_config_save_btn"):
            try:
                updates = [
                    ("go_live_readiness_weight_checklist_gap_pct", str(int(score_weight_checklist_gap_pct))),
                    ("go_live_readiness_weight_env_missing", str(int(score_weight_env_missing))),
                    ("go_live_readiness_weight_runtime_missing", str(int(score_weight_runtime_missing))),
                    ("go_live_readiness_weight_terminal_queue_failure", str(int(score_weight_terminal_queue_failure))),
                    ("go_live_readiness_weight_queue_execute_exception", str(int(score_weight_queue_execute_exception))),
                    (
                        "go_live_readiness_penalty_terminal_queue_failure_max",
                        str(int(score_penalty_terminal_queue_failure_max)),
                    ),
                    (
                        "go_live_readiness_penalty_queue_execute_exception_max",
                        str(int(score_penalty_queue_execute_exception_max)),
                    ),
                    (
                        "go_live_readiness_penalty_integration_warnings_warn",
                        str(int(score_penalty_integration_warnings_warn)),
                    ),
                    (
                        "go_live_readiness_penalty_integration_warnings_critical",
                        str(int(score_penalty_integration_warnings_critical)),
                    ),
                    ("go_live_readiness_threshold_green", str(int(score_threshold_green))),
                    ("go_live_readiness_threshold_yellow", str(int(score_threshold_yellow))),
                ]
                for key, value in updates:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=key,
                        value=value,
                        value_type="int",
                        description="Go-live readiness scoring configuration.",
                        is_active=True,
                        actor=user.username,
                    )
                st.success("Readiness scoring configuration saved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to save readiness scoring configuration: {exc}")
    readiness_score = 100.0
    readiness_weight_checklist_gap_pct = max(
        0, min(100, int(get_runtime_int(repo, "go_live_readiness_weight_checklist_gap_pct", 40)))
    )
    readiness_weight_env_missing = max(0, min(50, int(get_runtime_int(repo, "go_live_readiness_weight_env_missing", 5))))
    readiness_weight_runtime_missing = max(
        0, min(50, int(get_runtime_int(repo, "go_live_readiness_weight_runtime_missing", 5)))
    )
    readiness_weight_terminal_queue_failure = max(
        0, min(50, int(get_runtime_int(repo, "go_live_readiness_weight_terminal_queue_failure", 10)))
    )
    readiness_weight_queue_execute_exception = max(
        0, min(50, int(get_runtime_int(repo, "go_live_readiness_weight_queue_execute_exception", 5)))
    )
    readiness_penalty_terminal_queue_failure_max = max(
        0, min(100, int(get_runtime_int(repo, "go_live_readiness_penalty_terminal_queue_failure_max", 30)))
    )
    readiness_penalty_queue_execute_exception_max = max(
        0, min(100, int(get_runtime_int(repo, "go_live_readiness_penalty_queue_execute_exception_max", 20)))
    )
    readiness_penalty_integration_warnings_warn = max(
        0, min(100, int(get_runtime_int(repo, "go_live_readiness_penalty_integration_warnings_warn", 10)))
    )
    readiness_penalty_integration_warnings_critical = max(
        0,
        min(100, int(get_runtime_int(repo, "go_live_readiness_penalty_integration_warnings_critical", 20))),
    )
    warnings_warn_threshold = max(1, int(get_runtime_int(repo, "health_integration_warnings_warn_24h", 10)))
    warnings_critical_threshold = max(
        warnings_warn_threshold,
        int(get_runtime_int(repo, "health_integration_warnings_critical_24h", 30)),
    )
    readiness_threshold_green = max(0, min(100, int(get_runtime_int(repo, "go_live_readiness_threshold_green", 85))))
    readiness_threshold_yellow = max(
        0,
        min(100, int(get_runtime_int(repo, "go_live_readiness_threshold_yellow", 65))),
    )
    readiness_score -= max(
        0.0,
        (100.0 - float(checklist_completion_pct)) * (float(readiness_weight_checklist_gap_pct) / 100.0),
    )
    readiness_score -= float(len(env_missing)) * float(readiness_weight_env_missing)
    readiness_score -= float(len(runtime_missing)) * float(readiness_weight_runtime_missing)
    readiness_score -= min(
        float(readiness_penalty_terminal_queue_failure_max),
        float(signal_counts["terminal_queue_failures"]) * float(readiness_weight_terminal_queue_failure),
    )
    readiness_score -= min(
        float(readiness_penalty_queue_execute_exception_max),
        float(signal_counts["queue_execute_exceptions"]) * float(readiness_weight_queue_execute_exception),
    )
    warning_count = int(signal_counts["integration_warnings"])
    if warning_count >= int(warnings_critical_threshold):
        readiness_score -= float(readiness_penalty_integration_warnings_critical)
    elif warning_count >= int(warnings_warn_threshold):
        readiness_score -= float(readiness_penalty_integration_warnings_warn)
    readiness_score = max(0.0, min(100.0, readiness_score))
    if readiness_score >= float(readiness_threshold_green):
        readiness_state = "green"
    elif readiness_score >= float(readiness_threshold_yellow):
        readiness_state = "yellow"
    else:
        readiness_state = "red"
    gr1, gr2 = st.columns(2)
    gr1.metric("Go-Live Readiness Score", f"{readiness_score:.1f}")
    gr2.metric("Go-Live Readiness State", readiness_state.upper())

    go_live_summary_df = pd.DataFrame(
        [
            {
                "environment": settings.app_env,
                "generated_at_utc": now_ts.isoformat(),
                "generated_by": str(user.username or ""),
                "alembic_version": alembic_version,
                "required_env_missing_count": int(len(env_missing)),
                "required_runtime_missing_count": int(len(runtime_missing)),
                "env_untracked_count": int(len(untracked_env_keys)),
                "queue_job_count": int(len(queue_df)),
                "critical_alert_evidence_7d_count": int(len(critical_alert_evidence_df)),
                "provider_validation_runs_30d_count": int(len(provider_validation_df)),
                "provider_validation_signoff_dev_status": provider_signoff_dev_status,
                "provider_validation_signoff_prod_status": provider_signoff_prod_status,
                "provider_validation_signoff_dev_ready": bool(provider_signoff_dev_ready),
                "provider_validation_signoff_prod_ready": bool(provider_signoff_prod_ready),
                "health_calibration_signoff_dev_status": health_calibration_dev_status,
                "health_calibration_signoff_prod_status": health_calibration_prod_status,
                "health_calibration_signoff_dev_ready": bool(health_calibration_dev_ready),
                "health_calibration_signoff_prod_ready": bool(health_calibration_prod_ready),
                "health_alert_routing_signoff_dev_status": health_alert_routing_dev_status,
                "health_alert_routing_signoff_prod_status": health_alert_routing_prod_status,
                "health_alert_routing_signoff_dev_ready": bool(health_alert_routing_dev_ready),
                "health_alert_routing_signoff_prod_ready": bool(health_alert_routing_prod_ready),
                "automation_hardening_signoff_dev_status": automation_hardening_dev_status,
                "automation_hardening_signoff_prod_status": automation_hardening_prod_status,
                "automation_hardening_signoff_dev_ready": bool(automation_hardening_dev_ready),
                "automation_hardening_signoff_prod_ready": bool(automation_hardening_prod_ready),
                "restore_drills_180d_count": int(restore_drill_180d_count),
                "restore_drills_180d_pass_count": int(restore_drill_180d_pass_count),
                "restore_drill_last_at_utc": restore_drill_last_at,
                "restore_drill_last_result": restore_drill_last_result,
                "restore_drill_last_pass_age_days": restore_drill_last_pass_age_days,
                "go_live_signoff_items_total": int(go_live_signoff_total),
                "go_live_signoff_items_approved": int(go_live_signoff_approved),
                "legal_signoff_items_total": int(legal_signoff_total),
                "legal_signoff_items_approved": int(legal_signoff_approved),
                "legal_signoff_prod_approved_count": int(legal_approved_prod),
                "legal_signoff_prod_required_count": int(legal_required_total_prod),
                "legal_signoff_prod_ready": bool(legal_ready_prod),
                "dr_checklist_snapshots_180d_count": int(dr_checklist_180d_count),
                "dr_checklist_latest_completion_percent": dr_checklist_latest_completion_pct,
                "queue_execute_exceptions_24h": int(signal_counts["queue_execute_exceptions"]),
                "terminal_queue_failures_24h": int(signal_counts["terminal_queue_failures"]),
                "integration_warnings_24h": int(signal_counts["integration_warnings"]),
                "checklist_total_items": int(checklist_total),
                "checklist_done_items": int(checklist_done),
                "checklist_in_progress_items": int(checklist_in_progress),
                "checklist_not_started_items": int(checklist_not_started),
                "checklist_completion_percent": round(float(checklist_completion_pct), 2),
                "go_live_readiness_score": round(float(readiness_score), 2),
                "go_live_readiness_state": readiness_state,
            }
        ]
    )

    go_live_pack_buffer = BytesIO()
    with zipfile.ZipFile(go_live_pack_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as pack_zip:
        pack_zip.writestr("go_live_summary.csv", go_live_summary_df.to_csv(index=False))
        pack_zip.writestr("governance_metadata.csv", pd.DataFrame(
            [{
                "environment": settings.app_env,
                "lookback_days": int(lookback_days),
                "max_rows_per_scope": int(max_rows),
                "generated_at_utc": now_ts.isoformat(),
                "generated_by": str(user.username or ""),
            }]
        ).to_csv(index=False))
        pack_zip.writestr("governance_handoff_events.csv", nav_df.to_csv(index=False))
        pack_zip.writestr("governance_workspace_feedback.csv", feedback_df.to_csv(index=False))
        pack_zip.writestr("governance_parity_followup.csv", parity_df.to_csv(index=False))
        pack_zip.writestr("governance_photo_comp.csv", comp_df.to_csv(index=False))
        pack_zip.writestr("critical_alert_evidence_7d.csv", critical_alert_evidence_df.to_csv(index=False))
        pack_zip.writestr("shipping_provider_validation_30d.csv", provider_validation_df.to_csv(index=False))
        pack_zip.writestr("shipping_provider_validation_signoffs.csv", provider_signoff_df.to_csv(index=False))
        pack_zip.writestr("system_health_calibration_signoffs.csv", health_calibration_df.to_csv(index=False))
        pack_zip.writestr("system_health_alert_routing_signoffs.csv", health_alert_routing_df.to_csv(index=False))
        pack_zip.writestr("integration_automation_hardening_signoffs.csv", automation_hardening_df.to_csv(index=False))
        pack_zip.writestr("backup_restore_drills.csv", restore_drill_df.to_csv(index=False))
        pack_zip.writestr("backup_dr_checklist_snapshots.csv", dr_checklist_df.to_csv(index=False))
        pack_zip.writestr("go_live_section_signoffs.csv", go_live_signoff_df.to_csv(index=False))
        pack_zip.writestr("commerce_legal_signoffs.csv", legal_signoff_df.to_csv(index=False))
        pack_zip.writestr("integration_error_signal_counts_24h.csv", signal_counts_df.to_csv(index=False))
        pack_zip.writestr("integration_error_signal_samples_24h.csv", signal_samples_df.to_csv(index=False))
        pack_zip.writestr("integration_queue_snapshot.csv", queue_df.to_csv(index=False))
        pack_zip.writestr("config_missing_required.csv", missing_required_df.to_csv(index=False))
        pack_zip.writestr("config_env_untracked_keys.csv", env_untracked_df.to_csv(index=False))
        pack_zip.writestr("go_live_checklist_status.csv", checklist_status_df.to_csv(index=False))
        pack_zip.writestr("GO_LIVE_CHECKLIST_snapshot.md", checklist_text)
    go_live_pack_buffer.seek(0)
    st.download_button(
        "Download Go-Live Evidence Pack (ZIP)",
        data=go_live_pack_buffer.getvalue(),
        file_name=f"go_live_evidence_pack_{settings.app_env}_{now_ts.strftime('%Y%m%d_%H%M%S')}.zip",
        mime="application/zip",
        key="admin_go_live_evidence_pack_zip_btn",
    )
    if st.button(
        "Record Evidence Capture Event",
        key="admin_go_live_evidence_capture_event_btn",
        help="Create an audit-stamped event that evidence was captured for this environment.",
    ):
        try:
            repo.record_audit_event(
                entity_type="go_live_evidence",
                entity_id=None,
                action="capture",
                actor=user.username,
                changes={
                    "environment": settings.app_env,
                    "captured_at_utc": utcnow_naive().isoformat(),
                    "captured_by": str(user.username or ""),
                    "alembic_version": alembic_version,
                    "required_env_missing_count": int(len(env_missing)),
                    "required_runtime_missing_count": int(len(runtime_missing)),
                    "queue_job_count": int(len(queue_df)),
                    "critical_alert_evidence_7d_count": int(len(critical_alert_evidence_df)),
                    "provider_validation_runs_30d_count": int(len(provider_validation_df)),
                    "provider_validation_signoff_dev_status": provider_signoff_dev_status,
                    "provider_validation_signoff_prod_status": provider_signoff_prod_status,
                    "provider_validation_signoff_dev_ready": bool(provider_signoff_dev_ready),
                    "provider_validation_signoff_prod_ready": bool(provider_signoff_prod_ready),
                    "health_calibration_signoff_dev_status": health_calibration_dev_status,
                    "health_calibration_signoff_prod_status": health_calibration_prod_status,
                    "health_calibration_signoff_dev_ready": bool(health_calibration_dev_ready),
                    "health_calibration_signoff_prod_ready": bool(health_calibration_prod_ready),
                    "health_alert_routing_signoff_dev_status": health_alert_routing_dev_status,
                    "health_alert_routing_signoff_prod_status": health_alert_routing_prod_status,
                    "health_alert_routing_signoff_dev_ready": bool(health_alert_routing_dev_ready),
                    "health_alert_routing_signoff_prod_ready": bool(health_alert_routing_prod_ready),
                    "automation_hardening_signoff_dev_status": automation_hardening_dev_status,
                    "automation_hardening_signoff_prod_status": automation_hardening_prod_status,
                    "automation_hardening_signoff_dev_ready": bool(automation_hardening_dev_ready),
                    "automation_hardening_signoff_prod_ready": bool(automation_hardening_prod_ready),
                    "restore_drills_180d_count": int(restore_drill_180d_count),
                    "restore_drills_180d_pass_count": int(restore_drill_180d_pass_count),
                    "restore_drill_last_at_utc": restore_drill_last_at,
                    "restore_drill_last_result": restore_drill_last_result,
                    "restore_drill_last_pass_age_days": restore_drill_last_pass_age_days,
                    "go_live_signoff_items_total": int(go_live_signoff_total),
                    "go_live_signoff_items_approved": int(go_live_signoff_approved),
                    "legal_signoff_items_total": int(legal_signoff_total),
                    "legal_signoff_items_approved": int(legal_signoff_approved),
                    "legal_signoff_prod_approved_count": int(legal_approved_prod),
                    "legal_signoff_prod_required_count": int(legal_required_total_prod),
                    "legal_signoff_prod_ready": bool(legal_ready_prod),
                    "dr_checklist_snapshots_180d_count": int(dr_checklist_180d_count),
                    "dr_checklist_latest_completion_percent": dr_checklist_latest_completion_pct,
                    "queue_execute_exceptions_24h": int(signal_counts["queue_execute_exceptions"]),
                    "terminal_queue_failures_24h": int(signal_counts["terminal_queue_failures"]),
                    "integration_warnings_24h": int(signal_counts["integration_warnings"]),
                    "checklist_total_items": int(checklist_total),
                    "checklist_done_items": int(checklist_done),
                    "checklist_in_progress_items": int(checklist_in_progress),
                    "checklist_not_started_items": int(checklist_not_started),
                    "checklist_completion_percent": round(float(checklist_completion_pct), 2),
                    "go_live_readiness_score": round(float(readiness_score), 2),
                    "go_live_readiness_state": readiness_state,
                },
            )
            st.success("Go-live evidence capture event recorded.")
            st.rerun()
        except Exception as exc:
            st.error(f"Unable to record go-live evidence capture event: {exc}")

    recent_capture_logs = repo.db.scalars(
        select(AuditLog)
        .where(AuditLog.entity_type == "go_live_evidence")
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(20)
    ).all()
    if recent_capture_logs:
        capture_rows: list[dict[str, Any]] = []
        for row in recent_capture_logs:
            payload = _audit_changes(row)
            capture_rows.append(
                {
                    "id": int(row.id),
                    "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
                    "actor": str(row.actor or ""),
                    "action": str(row.action or ""),
                    "env": str(payload.get("environment") or ""),
                    "checklist_completion_percent": payload.get("checklist_completion_percent"),
                    "go_live_readiness_score": payload.get("go_live_readiness_score"),
                    "go_live_readiness_state": payload.get("go_live_readiness_state"),
                    "required_env_missing_count": payload.get("required_env_missing_count"),
                    "required_runtime_missing_count": payload.get("required_runtime_missing_count"),
                    "provider_validation_signoff_dev_status": payload.get("provider_validation_signoff_dev_status"),
                    "provider_validation_signoff_prod_status": payload.get("provider_validation_signoff_prod_status"),
                    "health_calibration_signoff_dev_status": payload.get("health_calibration_signoff_dev_status"),
                    "health_calibration_signoff_prod_status": payload.get("health_calibration_signoff_prod_status"),
                    "health_alert_routing_signoff_dev_status": payload.get("health_alert_routing_signoff_dev_status"),
                    "health_alert_routing_signoff_prod_status": payload.get("health_alert_routing_signoff_prod_status"),
                    "automation_hardening_signoff_dev_status": payload.get("automation_hardening_signoff_dev_status"),
                    "automation_hardening_signoff_prod_status": payload.get("automation_hardening_signoff_prod_status"),
                    "restore_drills_180d_count": payload.get("restore_drills_180d_count"),
                    "restore_drill_last_result": payload.get("restore_drill_last_result"),
                    "go_live_signoff_items_total": payload.get("go_live_signoff_items_total"),
                    "go_live_signoff_items_approved": payload.get("go_live_signoff_items_approved"),
                    "legal_signoff_items_total": payload.get("legal_signoff_items_total"),
                    "legal_signoff_items_approved": payload.get("legal_signoff_items_approved"),
                    "legal_signoff_prod_approved_count": payload.get("legal_signoff_prod_approved_count"),
                    "legal_signoff_prod_required_count": payload.get("legal_signoff_prod_required_count"),
                    "legal_signoff_prod_ready": payload.get("legal_signoff_prod_ready"),
                    "dr_checklist_snapshots_180d_count": payload.get("dr_checklist_snapshots_180d_count"),
                    "dr_checklist_latest_completion_percent": payload.get("dr_checklist_latest_completion_percent"),
                    "queue_execute_exceptions_24h": payload.get("queue_execute_exceptions_24h"),
                    "terminal_queue_failures_24h": payload.get("terminal_queue_failures_24h"),
                }
            )
        st.caption("Recent Evidence Capture Events")
        st.dataframe(pd.DataFrame(capture_rows), use_container_width=True, hide_index=True)

    st.markdown("#### Go-Live Section Sign-Off Tracker")
    st.caption(
        "Track owner/date/evidence completion per checklist item. Latest status per item is included in the evidence pack."
    )
    checklist_item_options: list[tuple[str, str, str]] = []
    for _state, raw_label in checklist_match_rows:
        label = str(raw_label or "").strip()
        if not label:
            continue
        section_key = "general"
        item_key = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:120] or "item"
        if ")" in label and label.split(")", 1)[0].strip().isdigit():
            section_key = f"section_{label.split(')', 1)[0].strip()}"
        checklist_item_options.append((label, section_key, item_key))
    option_map = {f"{label} [{section_key}:{item_key}]": (section_key, item_key, label) for label, section_key, item_key in checklist_item_options}
    default_option = next(iter(option_map.keys()), "manual [general:manual_item]")

    with st.form("admin_go_live_section_signoff_form"):
        gsf1, gsf2 = st.columns(2)
        with gsf1:
            selected_item = st.selectbox(
                "Checklist Item",
                options=list(option_map.keys()) if option_map else [default_option],
                index=0,
            )
            default_section_key, default_item_key, default_label = option_map.get(
                selected_item, ("general", "manual_item", "manual")
            )
            signoff_section_key = st.text_input("Section Key", value=default_section_key)
            signoff_item_key = st.text_input("Item Key", value=default_item_key)
            signoff_label = st.text_input("Item Label", value=default_label)
        with gsf2:
            signoff_status = st.selectbox("Status", options=["approved", "blocked", "needs_followup"], index=0)
            signoff_owner = st.text_input("Owner", value=str(user.username or ""))
            signoff_date = st.date_input("Sign-Off Date", value=utcnow_naive().date())
            signoff_evidence_link = st.text_input("Evidence Link", placeholder="ticket/runbook/artifact URL")
        signoff_notes = st.text_area("Notes", placeholder="Any follow-up or context.")
        save_section_signoff = st.form_submit_button("Record Checklist Item Sign-Off")
    if save_section_signoff:
        try:
            repo.record_audit_event(
                entity_type="go_live_section_signoff",
                entity_id=None,
                action="record",
                actor=user.username,
                changes={
                    "section_key": str(signoff_section_key or "").strip().lower(),
                    "item_key": str(signoff_item_key or "").strip().lower(),
                    "item_label": str(signoff_label or "").strip(),
                    "status": str(signoff_status or "").strip().lower(),
                    "owner": str(signoff_owner or "").strip(),
                    "signoff_date": str(signoff_date.isoformat()),
                    "evidence_link": str(signoff_evidence_link or "").strip(),
                    "notes": str(signoff_notes or "").strip(),
                },
            )
            st.success("Checklist item sign-off recorded.")
            st.rerun()
        except Exception as exc:
            st.error(f"Unable to record checklist item sign-off: {exc}")

    if not go_live_signoff_df.empty:
        st.dataframe(go_live_signoff_df, use_container_width=True, hide_index=True)
        st.download_button(
            "Download Go-Live Sign-Off CSV",
            data=go_live_signoff_df.to_csv(index=False).encode("utf-8"),
            file_name=f"go_live_section_signoffs_{settings.app_env}_{now_ts.strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
            key="admin_go_live_section_signoff_download_csv_btn",
        )
    else:
        st.caption("No checklist item sign-offs recorded yet.")

    st.markdown("#### Commerce Legal Sign-Off Tracker")
    st.caption(
        "Capture policy-level legal/compliance approvals (owner/date/evidence) for tax, retention, and marketplace readiness."
    )
    legal_coverage_rows: list[dict[str, Any]] = []
    for target_env in ["dev", "prod"]:
        for policy_key, policy_label in legal_policy_catalog:
            latest = latest_legal_signoff_by_key.get(f"{target_env}::{policy_key}") or {}
            legal_coverage_rows.append(
                {
                    "environment": target_env,
                    "policy_key": policy_key,
                    "policy_label": policy_label,
                    "status": str(latest.get("status") or "missing"),
                    "owner": str(latest.get("owner") or ""),
                    "signoff_date": str(latest.get("signoff_date") or ""),
                    "evidence_link": str(latest.get("evidence_link") or ""),
                }
            )
    legal_coverage_df = pd.DataFrame(legal_coverage_rows)
    st.dataframe(legal_coverage_df, use_container_width=True, hide_index=True)
    ls1, ls2 = st.columns([2, 1])
    with ls1:
        seed_target = st.selectbox(
            "Seed Missing Policy Rows For",
            options=["prod", "dev", "dev+prod"],
            index=0,
            key="admin_commerce_legal_signoff_seed_target",
        )
    with ls2:
        if st.button("Seed Missing Legal Sign-Off Items", key="admin_commerce_legal_signoff_seed_btn"):
            target_envs = ["dev", "prod"] if seed_target == "dev+prod" else [seed_target]
            seeded_count = 0
            try:
                for target_env in target_envs:
                    for policy_key, policy_label in legal_policy_catalog:
                        composite_key = f"{target_env}::{policy_key}"
                        if composite_key in latest_legal_signoff_by_key:
                            continue
                        repo.record_audit_event(
                            entity_type="commerce_legal_signoff",
                            entity_id=None,
                            action="seed_missing",
                            actor=user.username,
                            changes={
                                "target_env": target_env,
                                "policy_key": str(policy_key or "").strip().lower(),
                                "policy_label": str(policy_label or "").strip(),
                                "status": "needs_followup",
                                "owner": str(user.username or "").strip(),
                                "signoff_date": str(utcnow_naive().date().isoformat()),
                                "evidence_link": "",
                                "notes": "Auto-seeded missing legal sign-off item from Admin tracker.",
                                "seeded": True,
                            },
                        )
                        seeded_count += 1
                if seeded_count <= 0:
                    st.info("No missing legal sign-off items to seed for the selected target.")
                else:
                    st.success(f"Seeded {seeded_count} missing legal sign-off item(s).")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to seed legal sign-off items: {exc}")
    qa1, qa2, qa3 = st.columns(3)
    with qa1:
        quick_env = st.selectbox(
            "Quick Approve Env",
            options=["prod", "dev"],
            index=0,
            key="admin_commerce_legal_signoff_quick_env",
        )
    with qa2:
        quick_policy = st.selectbox(
            "Quick Approve Policy",
            options=[f"{label} [{key}]" for key, label in legal_policy_catalog],
            index=0,
            key="admin_commerce_legal_signoff_quick_policy",
        )
        quick_policy_key = str(quick_policy.rsplit("[", 1)[-1].rstrip("]")).strip().lower()
        quick_policy_label = next(
            (label for key, label in legal_policy_catalog if key == quick_policy_key),
            quick_policy,
        )
    with qa3:
        if st.button("Quick Mark Approved", key="admin_commerce_legal_signoff_quick_approve_btn"):
            try:
                repo.record_audit_event(
                    entity_type="commerce_legal_signoff",
                    entity_id=None,
                    action="quick_approve",
                    actor=user.username,
                    changes={
                        "target_env": str(quick_env or "").strip().lower(),
                        "policy_key": str(quick_policy_key or "").strip().lower(),
                        "policy_label": str(quick_policy_label or "").strip(),
                        "status": "approved",
                        "owner": str(user.username or "").strip(),
                        "signoff_date": str(utcnow_naive().date().isoformat()),
                        "evidence_link": "",
                        "notes": "Quick approved from Commerce Legal Sign-Off coverage table.",
                        "quick_action": True,
                    },
                )
                st.success("Legal sign-off quick-approved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to quick-approve legal sign-off: {exc}")

    legal_policy_options = {
        f"{label} [{key}]": (key, label)
        for key, label in legal_policy_catalog
    }
    with st.form("admin_commerce_legal_signoff_form"):
        lsf1, lsf2 = st.columns(2)
        with lsf1:
            selected_policy = st.selectbox(
                "Policy",
                options=list(legal_policy_options.keys()),
                index=0,
            )
            selected_policy_key, selected_policy_label = legal_policy_options.get(
                selected_policy,
                ("tax_treatment", "Tax treatment policy"),
            )
            legal_target_env = st.selectbox("Environment", options=["dev", "prod"], index=1)
            legal_signoff_date = st.date_input("Sign-Off Date", value=utcnow_naive().date())
            legal_owner = st.text_input("Owner", value=str(user.username or ""))
        with lsf2:
            legal_status = st.selectbox("Status", options=["approved", "blocked", "needs_followup"], index=0)
            legal_evidence_link = st.text_input("Evidence Link", placeholder="ticket/runbook/artifact URL")
            legal_policy_notes = st.text_area("Notes", placeholder="Policy assumptions, reviewer comments, open risks.")
        legal_record_submit = st.form_submit_button("Record Commerce Legal Sign-Off")
    if legal_record_submit:
        try:
            repo.record_audit_event(
                entity_type="commerce_legal_signoff",
                entity_id=None,
                action="record",
                actor=user.username,
                changes={
                    "target_env": str(legal_target_env or "").strip().lower(),
                    "policy_key": str(selected_policy_key or "").strip().lower(),
                    "policy_label": str(selected_policy_label or "").strip(),
                    "status": str(legal_status or "").strip().lower(),
                    "owner": str(legal_owner or "").strip(),
                    "signoff_date": str(legal_signoff_date.isoformat()),
                    "evidence_link": str(legal_evidence_link or "").strip(),
                    "notes": str(legal_policy_notes or "").strip(),
                },
            )
            st.success("Commerce legal sign-off recorded.")
            st.rerun()
        except Exception as exc:
            st.error(f"Unable to record commerce legal sign-off: {exc}")

    if not legal_signoff_df.empty:
        st.dataframe(legal_signoff_df, use_container_width=True, hide_index=True)
        st.download_button(
            "Download Commerce Legal Sign-Off CSV",
            data=legal_signoff_df.to_csv(index=False).encode("utf-8"),
            file_name=f"commerce_legal_signoffs_{settings.app_env}_{now_ts.strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
            key="admin_commerce_legal_signoff_download_csv_btn",
        )
    else:
        st.caption("No commerce legal sign-offs recorded yet.")

    st.markdown("#### Governance Snapshot Runner")
    st.caption(
        "Record a point-in-time governance snapshot event for audit/review cadence. "
        "This does not enqueue background jobs yet; it captures current counts and scope config."
    )
    g1, g2 = st.columns(2)
    with g1:
        if st.button(
            "Record Governance Snapshot Event",
            key="admin_governance_export_hub_record_snapshot_btn",
            use_container_width=True,
        ):
            try:
                counts = _record_governance_snapshot_event(
                    repo,
                    actor=user.username,
                    lookback_days=int(lookback_days),
                    max_rows=int(max_rows),
                    source="admin_governance_exports",
                    download_intent=False,
                )
                st.success(
                    "Governance snapshot recorded. "
                    f"handoff={counts['handoff_events']} "
                    f"feedback={counts['workspace_feedback_events']} "
                    f"parity={counts['parity_followup_events']} "
                    f"comp={counts['photo_comp_events']}"
                )
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to record governance snapshot: {exc}")
    with g2:
        if st.button(
            "Record + Download Combined Bundle",
            key="admin_governance_export_hub_record_and_download_btn",
            use_container_width=True,
        ):
            try:
                counts = _record_governance_snapshot_event(
                    repo,
                    actor=user.username,
                    lookback_days=int(lookback_days),
                    max_rows=int(max_rows),
                    source="admin_governance_exports",
                    download_intent=True,
                )
                st.success(
                    "Governance snapshot recorded. "
                    f"handoff={counts['handoff_events']} "
                    f"feedback={counts['workspace_feedback_events']} "
                    f"parity={counts['parity_followup_events']} "
                    f"comp={counts['photo_comp_events']}. "
                    "Use the bundle download button above."
                )
            except Exception as exc:
                st.error(f"Unable to record governance snapshot: {exc}")

    st.markdown("#### Recent Governance Snapshots")
    snapshot_logs = repo.db.scalars(
        select(AuditLog)
        .where(
            AuditLog.entity_type == "governance_export",
            AuditLog.action == "snapshot",
            AuditLog.created_at >= cutoff,
        )
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(int(max_rows))
    ).all()
    snapshot_rows: list[dict[str, Any]] = []
    for row in snapshot_logs:
        payload = _audit_changes(row)
        counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
        snapshot_rows.append(
            {
                "id": int(row.id),
                "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
                "actor": str(row.actor or ""),
                "environment": str(payload.get("environment") or settings.app_env),
                "lookback_days": int(payload.get("lookback_days") or 0),
                "max_rows_per_scope": int(payload.get("max_rows_per_scope") or 0),
                "source": str(payload.get("source") or "unknown"),
                "scheduled": bool(payload.get("scheduled") or False),
                "handoff_events": int(counts.get("handoff_events") or 0),
                "workspace_feedback_events": int(counts.get("workspace_feedback_events") or 0),
                "parity_followup_events": int(counts.get("parity_followup_events") or 0),
                "photo_comp_events": int(counts.get("photo_comp_events") or 0),
                "download_intent": bool(payload.get("download_intent") or False),
            }
        )
    if snapshot_rows:
        snapshot_df = pd.DataFrame(snapshot_rows)
        source_options = sorted(
            {
                str(v).strip()
                for v in snapshot_df["source"].dropna().tolist()
                if str(v).strip()
            }
        )
        sf1, sf2 = st.columns(2)
        with sf1:
            selected_sources = st.multiselect(
                "Source Filter",
                options=source_options,
                default=source_options,
                key="admin_governance_snapshot_source_filter",
            )
        with sf2:
            scheduled_filter = st.selectbox(
                "Scheduled Filter",
                options=["all", "scheduled_only", "manual_only"],
                index=0,
                key="admin_governance_snapshot_scheduled_filter",
            )
        filtered_snapshot_df = snapshot_df
        if selected_sources:
            filtered_snapshot_df = filtered_snapshot_df[filtered_snapshot_df["source"].astype(str).isin(selected_sources)]
        if scheduled_filter == "scheduled_only":
            filtered_snapshot_df = filtered_snapshot_df[filtered_snapshot_df["scheduled"] == True]  # noqa: E712
        elif scheduled_filter == "manual_only":
            filtered_snapshot_df = filtered_snapshot_df[filtered_snapshot_df["scheduled"] == False]  # noqa: E712
        st.dataframe(filtered_snapshot_df, use_container_width=True, hide_index=True)
        st.download_button(
            "Download Governance Snapshot History CSV",
            data=filtered_snapshot_df.to_csv(index=False).encode("utf-8"),
            file_name=(
                f"governance_snapshot_history_{settings.app_env}_"
                f"{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv"
            ),
            mime="text/csv",
            key="admin_governance_export_hub_snapshot_history_csv_btn",
        )
    else:
        st.caption("No governance snapshot events in selected lookback window.")


def render_admin(repo: InventoryRepository) -> None:
    user = current_user()
    users = repo.list_app_users(active_only=False)
    st.subheader("Admin")
    env_source_label = "Local .env" if uses_env_file(settings.app_env) else "K8s Process Env"
    env_source_color = "green" if uses_env_file(settings.app_env) else "blue"
    st.caption(f"Env Source: :{env_source_color}[{env_source_label}]")
    st.caption("Manage app users, roles, and permissions.")
    render_help_panel(
        section_title="Admin",
        goal="Control user-role assignments and permission policies in one place.",
        steps=[
            "Create or update users and assign role memberships.",
            "Set role-to-permission mappings for viewer/ops/admin workflows.",
            "Keep at least one admin role active for governance continuity.",
            "All changes are written to audit log with signed-in identity.",
        ],
        roadmap_phase="v0.2 Operations Foundation",
    )

    st.caption(f"Signed in as `{user.username}` ({user.role}).")
    if users and not ensure_permission(user, "manage_settings", "Admin Access"):
        st.info("Admin access is required to manage users and permissions.")
        return

    st.markdown("### Config Health Summary")
    env_file_mode = uses_env_file(settings.app_env)
    env_defaults_summary = read_env_file(".env.example")
    tracked_env_keys = set(env_defaults_summary.keys())
    env_values_summary = (
        read_env_file(".env")
        if env_file_mode
        else read_process_env_values(tracked_keys=tracked_env_keys, include_untracked_editable=True)
    )
    required_env = required_env_keys()
    missing_env_required = [
        key
        for key in sorted(required_env)
        if key not in env_values_summary or not str(env_values_summary.get(key, "")).strip()
    ]
    untracked_env_keys = sorted([k for k in env_values_summary.keys() if k not in env_defaults_summary])
    env_missing_or_empty_all = [
        key
        for key in sorted(env_defaults_summary.keys())
        if key not in env_values_summary or not str(env_values_summary.get(key, "")).strip()
    ]
    env_required_total = max(1, len(required_env))
    env_required_ok = env_required_total - len(missing_env_required)

    runtime_seed_defaults_summary = _runtime_setting_seed_defaults()
    required_runtime = required_runtime_keys()
    try:
        runtime_rows_summary = repo.list_runtime_settings(environment=settings.app_env, active_only=False)
    except Exception:
        runtime_rows_summary = []
    runtime_by_key_summary = {str(row.key): row for row in runtime_rows_summary}
    missing_runtime_required = [
        key
        for key in sorted(required_runtime)
        if key not in runtime_by_key_summary or not bool(getattr(runtime_by_key_summary[key], "is_active", False))
    ]
    runtime_missing_or_inactive_all = [
        str(item.get("key") or "")
        for item in runtime_seed_defaults_summary
        if (
            str(item.get("key") or "") not in runtime_by_key_summary
            or not bool(getattr(runtime_by_key_summary[str(item.get("key") or "")], "is_active", False))
        )
    ]
    runtime_custom_untracked_keys = sorted(
        [key for key in runtime_by_key_summary.keys() if key not in {str(item.get("key") or "") for item in runtime_seed_defaults_summary}]
    )
    runtime_required_total = max(1, len(required_runtime))
    runtime_required_ok = runtime_required_total - len(missing_runtime_required)

    h1, h2, h3, h4, h5, h6, h7, h8 = st.columns(8)
    h1.metric("Env Required OK", f"{env_required_ok}/{env_required_total}")
    h2.metric("Runtime Required OK", f"{runtime_required_ok}/{runtime_required_total}")
    h3.metric("Env Required Missing", f"{len(missing_env_required)}")
    h4.metric("Runtime Required Missing/Inactive", f"{len(missing_runtime_required)}")
    h5.metric("Env Missing/Empty (All)", f"{len(env_missing_or_empty_all)}")
    h6.metric("Runtime Missing/Inactive (All)", f"{len(runtime_missing_or_inactive_all)}")
    h7.metric("Env Untracked Keys", f"{len(untracked_env_keys)}")
    h8.metric("Runtime Untracked Keys", f"{len(runtime_custom_untracked_keys)}")

    config_health_snapshot = {
        "environment": settings.app_env,
        "generated_at_utc": utcnow_naive().isoformat(),
        "required": {
            "env": {
                "ok": env_required_ok,
                "total": env_required_total,
                "missing_or_empty": missing_env_required,
            },
            "runtime": {
                "ok": runtime_required_ok,
                "total": runtime_required_total,
                "missing_or_inactive": missing_runtime_required,
            },
        },
        "all_tracked": {
            "env_missing_or_empty_count": len(env_missing_or_empty_all),
            "env_missing_or_empty_keys": env_missing_or_empty_all[:200],
            "env_untracked_count": len(untracked_env_keys),
            "env_untracked_keys": untracked_env_keys[:200],
            "runtime_missing_or_inactive_count": len(runtime_missing_or_inactive_all),
            "runtime_missing_or_inactive_keys": runtime_missing_or_inactive_all[:200],
            "runtime_untracked_count": len(runtime_custom_untracked_keys),
            "runtime_untracked_keys": runtime_custom_untracked_keys[:200],
        },
    }
    st.download_button(
        "Download Config Health Snapshot (JSON)",
        data=json.dumps(config_health_snapshot, indent=2).encode("utf-8"),
        file_name=f"config_health_snapshot_{settings.app_env}.json",
        mime="application/json",
        key="admin_config_health_snapshot_download",
    )

    hf1, hf2 = st.columns(2)
    with hf1:
        if st.button(
            "Auto-Fix Required Env Keys",
            key="admin_top_autofix_required_env_btn",
            disabled=(len(missing_env_required) == 0 or not env_file_mode),
        ):
            try:
                fixed = _apply_required_env_defaults(
                    env_path=".env",
                    required_keys=required_env,
                    env_values=env_values_summary,
                    recommended_defaults=env_defaults_summary,
                )
                if fixed:
                    st.success(f"Auto-fixed {fixed} required env key(s).")
                else:
                    st.info("No required env keys were auto-fixed.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to auto-fix required env keys: {exc}")
    if not env_file_mode:
        st.caption(
            "Env auto-fix is disabled for non-local environments. "
            "Update Kubernetes Secret/ConfigMap values and redeploy/restart workloads."
        )
    with hf2:
        if st.button(
            "Auto-Fix Required Runtime Keys",
            key="admin_top_autofix_required_runtime_btn",
            disabled=(len(missing_runtime_required) == 0),
        ):
            try:
                fixed = _apply_required_runtime_defaults(
                    repo=repo,
                    actor=user.username,
                    required_keys=required_runtime,
                    runtime_rows=runtime_rows_summary,
                    seed_defaults=runtime_seed_defaults_summary,
                )
                if fixed:
                    st.success(f"Auto-fixed {fixed} required runtime key(s).")
                else:
                    st.info("No required runtime keys were auto-fixed.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to auto-fix required runtime keys: {exc}")
    bf1, bf2 = st.columns(2)
    with bf1:
        if st.button(
            "Apply Missing + Empty Env Defaults",
            key="admin_top_apply_all_env_defaults_btn",
            disabled=(len(env_missing_or_empty_all) == 0 or not env_file_mode),
        ):
            try:
                fixed = _apply_all_env_defaults(
                    env_path=".env",
                    env_values=env_values_summary,
                    recommended_defaults=env_defaults_summary,
                )
                if fixed:
                    st.success(f"Applied {fixed} env default value(s).")
                else:
                    st.info("No env defaults were applied.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to apply env defaults: {exc}")
    with bf2:
        if st.button(
            "Apply Missing + Inactive Runtime Defaults",
            key="admin_top_apply_all_runtime_defaults_btn",
            disabled=(len(runtime_missing_or_inactive_all) == 0),
        ):
            try:
                fixed = _apply_all_runtime_defaults(
                    repo=repo,
                    actor=user.username,
                    runtime_rows=runtime_rows_summary,
                    seed_defaults=runtime_seed_defaults_summary,
                )
                if fixed:
                    st.success(f"Applied {fixed} runtime default update(s).")
                else:
                    st.info("No runtime defaults were applied.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to apply runtime defaults: {exc}")
    st.caption(
        "Detailed coverage, exports, and bulk default actions are available in `Env Config` and `Runtime Settings` tabs."
    )

    (
        tab_users,
        tab_perms,
        tab_migrations,
        tab_maintenance,
        tab_backups,
        tab_ebay_verify,
        tab_ai_runtime,
        tab_env_config,
        tab_runtime_settings,
        tab_integrations,
        tab_comp_config,
        tab_saved_filters,
        tab_sync_jobs,
        tab_governance_exports,
        tab_system_health,
    ) = st.tabs(
        [
            "Users",
            "Role Permissions",
            "Migrations",
            "Maintenance",
            "Backups",
            "eBay Verify",
            "AI Runtime",
            "Env Config",
            "Runtime Settings",
            "Integrations",
            "Comp Config",
            "Saved Filters",
            "Sync Jobs",
            "Governance Exports",
            "System Health",
        ]
    )

    with tab_users:
        st.markdown("### User Directory")
        with st.expander("Auth Session Debug", expanded=False):
            snapshot = auth_debug_snapshot()
            st.caption("Use this to verify remember-token/session restore behavior across restarts.")
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Session Authenticated", "yes" if bool(snapshot.get("auth_authenticated_session")) else "no")
            m2.metric("Remember Enabled", "yes" if bool(snapshot.get("auth_remember_enabled_session")) else "no")
            m3.metric("Query Token Present", "yes" if bool(snapshot.get("query_token_present")) else "no")
            m4.metric("Query Token Valid", "yes" if bool(snapshot.get("query_token_valid")) else "no")
            if not bool(snapshot.get("query_token_present")) and bool(snapshot.get("auth_remember_enabled_session")):
                st.warning(
                    "Remember is enabled but URL query token is missing. "
                    "Navigate once after sign-in or re-sign-in with Remember enabled to mint token."
                )
            st.dataframe(
                pd.DataFrame([snapshot]),
                use_container_width=True,
                hide_index=True,
            )
        if not users:
            st.warning("No app users found. Bootstrap the first admin account.")
            with st.form("bootstrap_first_admin_form"):
                b1, b2, b3 = st.columns(3)
                with b1:
                    bootstrap_username = st.text_input("Admin Username", value="admin")
                with b2:
                    bootstrap_display_name = st.text_input("Display Name", value="Administrator")
                with b3:
                    bootstrap_email = st.text_input("Email", value="")
                bp1, bp2 = st.columns(2)
                with bp1:
                    bootstrap_password = st.text_input("Admin Password", type="password")
                with bp2:
                    bootstrap_password_confirm = st.text_input("Confirm Password", type="password")
                bootstrap_submit = st.form_submit_button("Bootstrap First Admin User")
            if bootstrap_submit:
                if not bootstrap_username.strip():
                    st.error("Admin username is required.")
                elif bootstrap_password != bootstrap_password_confirm:
                    st.error("Passwords do not match.")
                else:
                    try:
                        row = repo.upsert_app_user(
                            username=bootstrap_username.strip(),
                            role="admin",
                            display_name=bootstrap_display_name.strip(),
                            email=bootstrap_email.strip(),
                            password=bootstrap_password,
                            is_active=True,
                            actor=user.username,
                        )
                        if not repo.list_role_permissions():
                            for role_name, perms in DEFAULT_PERMISSIONS.items():
                                repo.set_role_permissions(role_name, set(perms), actor=user.username)
                        st.success(f"Bootstrapped admin user `{row.username}`.")
                    except ValueError as exc:
                        st.error(str(exc))
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "id": u.id,
                        "username": u.username,
                        "display_name": u.display_name,
                        "email": u.email,
                        "role": u.role,
                        "password_set": bool(u.password_hash),
                        "is_active": u.is_active,
                    }
                    for u in users
                ]
            ),
            use_container_width=True,
        )

        st.markdown("### Add/Update User")
        with st.form("admin_upsert_user_form", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            with c1:
                username = st.text_input("Username")
            with c2:
                role = st.selectbox("Role", sorted(DEFAULT_PERMISSIONS.keys()))
            with c3:
                is_active = st.checkbox("Active", value=True)
            d1, d2 = st.columns(2)
            with d1:
                display_name = st.text_input("Display Name")
            with d2:
                email = st.text_input("Email")
            password = st.text_input("Password (Required for new users, min 8 chars)", type="password")
            submit = st.form_submit_button("Save User")

        if submit:
            try:
                existing_usernames = {u.username for u in users}
                is_new_user = username.strip() not in existing_usernames
                if is_new_user and not password.strip():
                    st.error("Password is required when creating a new user.")
                else:
                    row = repo.upsert_app_user(
                        username=username.strip(),
                        role=role,
                        display_name=display_name.strip(),
                        email=email.strip(),
                        password=password,
                        is_active=is_active,
                        actor=user.username,
                    )
                    st.success(f"Saved user `{row.username}`.")
            except ValueError as exc:
                st.error(str(exc))

        if users:
            st.markdown("### Edit Existing User")
            user_map = {f"#{u.id} | {u.username}": u for u in users}
            selected_key = st.selectbox("Select User", list(user_map.keys()))
            selected = user_map[selected_key]
            with st.form("admin_edit_user_form"):
                e1, e2, e3 = st.columns(3)
                with e1:
                    edit_role = st.selectbox(
                        "Role",
                        sorted(DEFAULT_PERMISSIONS.keys()),
                        index=sorted(DEFAULT_PERMISSIONS.keys()).index(selected.role)
                        if selected.role in DEFAULT_PERMISSIONS
                        else 0,
                    )
                with e2:
                    edit_active = st.checkbox("Active", value=selected.is_active)
                with e3:
                    edit_display_name = st.text_input("Display Name", value=selected.display_name)
                edit_email = st.text_input("Email", value=selected.email)
                edit_password = st.text_input("Reset Password (Optional)", type="password")
                update_submit = st.form_submit_button("Update User")

            if update_submit:
                try:
                    repo.update_app_user(
                        selected.id,
                        {
                            "role": edit_role,
                            "is_active": edit_active,
                            "display_name": edit_display_name.strip(),
                            "email": edit_email.strip(),
                        },
                        actor=user.username,
                    )
                    if edit_password.strip():
                        repo.set_app_user_password(selected.id, edit_password, actor=user.username)
                    st.success("User updated.")
                except ValueError as exc:
                    st.error(str(exc))

    with tab_perms:
        st.markdown("### Role Permission Matrix")
        permission_map = repo.list_role_permissions()
        role_names = sorted(set(DEFAULT_PERMISSIONS.keys()) | set(permission_map.keys()))
        all_permissions = _all_permission_options()

        matrix_rows = []
        for role_name in role_names:
            effective = permission_map.get(role_name, DEFAULT_PERMISSIONS.get(role_name, set()))
            row = {"role": role_name}
            for perm in all_permissions:
                row[perm] = perm in effective
            matrix_rows.append(row)
        st.dataframe(pd.DataFrame(matrix_rows), use_container_width=True)

        st.markdown("### Edit Role Permissions")
        selected_role = st.selectbox("Role", role_names)
        selected_current = permission_map.get(
            selected_role,
            DEFAULT_PERMISSIONS.get(selected_role, set()),
        )
        with st.form("admin_role_permissions_form"):
            selected_permissions = st.multiselect(
                "Permissions",
                all_permissions,
                default=sorted(selected_current),
            )
            save = st.form_submit_button("Save Role Permissions")

        if save:
            repo.set_role_permissions(selected_role, set(selected_permissions), actor=user.username)
            st.success(f"Updated permissions for role `{selected_role}`.")

    with tab_migrations:
        st.markdown("### Database Migrations")
        st.caption("Inspect Alembic revision status and run targeted upgrades.")
        if not users:
            st.info("Bootstrap the first admin user before running migrations from the UI.")
        else:
            current_rev = _get_current_db_revision(repo)
            st.metric("Current DB Revision", current_rev)

            try:
                history_rows = _migration_history_rows()
            except Exception as exc:
                history_rows = []
                st.error(f"Unable to read migration history: {exc}")

            if history_rows:
                st.dataframe(pd.DataFrame(history_rows), use_container_width=True)
                target_options = ["head"] + [row["revision"] for row in history_rows]
            else:
                target_options = ["head"]

            with st.form("admin_migration_upgrade_form"):
                target_revision = st.selectbox(
                    "Upgrade Target Revision",
                    options=target_options,
                    help="Choose `head` for latest, or choose a specific revision ID.",
                )
                confirm_upgrade = st.checkbox("I understand this changes DB schema.")
                run_upgrade = st.form_submit_button("Run Upgrade")

            if run_upgrade:
                if not confirm_upgrade:
                    st.error("Confirm schema-change acknowledgement first.")
                else:
                    try:
                        migrate_upgrade(target_revision)
                        st.success(f"Migration upgrade completed to `{target_revision}`.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Migration failed: {exc}")

            st.markdown("### Rollback / Downgrade")
            if settings.app_env.lower() == "prod":
                st.warning("Rollback is disabled from UI in `APP_ENV=prod`. Use controlled ops runbook.")
            else:
                with st.form("admin_migration_downgrade_form"):
                    downgrade_target = st.selectbox(
                        "Downgrade Target Revision",
                        options=["-1", "base"] + [row["revision"] for row in history_rows],
                        help="`-1` rolls back one step. `base` rolls back all migrations.",
                    )
                    confirm_downgrade = st.checkbox(
                        "I understand rollback can break app behavior and may require data recovery."
                    )
                    run_downgrade = st.form_submit_button("Run Downgrade")

                if run_downgrade:
                    if not confirm_downgrade:
                        st.error("Confirm rollback acknowledgement first.")
                    else:
                        try:
                            migrate_downgrade(downgrade_target)
                            st.success(f"Migration downgrade completed to `{downgrade_target}`.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Downgrade failed: {exc}")

    with tab_maintenance:
        st.markdown("### Data Seeding")
        st.caption("Seed development fixtures for local/dev environments.")
        if settings.app_env.lower() == "prod":
            st.warning("Seeding is disabled in `APP_ENV=prod`.")
        else:
            with st.form("admin_seed_form"):
                seed_mode = st.radio(
                    "Seed Mode",
                    options=["append_only", "wipe_seed_tables_then_seed", "wipe_operational_then_seed"],
                    format_func=_seed_mode_label,
                )
                confirm_seed = st.checkbox("I understand this modifies data.")
                run_seed = st.form_submit_button("Run Seed")

            if run_seed:
                if not confirm_seed:
                    st.error("Confirm seed acknowledgement first.")
                else:
                    try:
                        if seed_mode == "wipe_operational_then_seed":
                            _wipe_operational_data(
                                repo,
                                include_shipping_presets=False,
                                include_document_templates=False,
                                include_audit_logs=False,
                            )
                        counts = seed_dev_data(wipe=(seed_mode == "wipe_seed_tables_then_seed"))
                        st.success(
                            "Seed complete: "
                            f"lots={counts['lots']}, products={counts['products']}, assignments={counts['assignments']}, "
                            f"listings={counts['listings']}, sales={counts['sales']}, media={counts['media']}"
                        )
                    except Exception as exc:
                        st.error(f"Seed failed: {exc}")

        st.markdown("### Operational Data Reset")
        st.caption("Wipe operational tables while keeping app users and role permissions.")
        if settings.app_env.lower() == "prod":
            st.warning("Operational reset is disabled in `APP_ENV=prod`.")
        else:
            with st.form("admin_wipe_operational_form"):
                include_shipping_presets = st.checkbox("Also wipe shipping presets", value=False)
                include_document_templates = st.checkbox("Also wipe document templates", value=False)
                include_audit_logs = st.checkbox("Also wipe audit logs", value=False)
                wipe_phrase = st.text_input("Type WIPE to confirm")
                run_wipe = st.form_submit_button("Wipe Operational Data")

            if run_wipe:
                if wipe_phrase.strip() != "WIPE":
                    st.error("Type `WIPE` exactly to confirm.")
                else:
                    try:
                        deleted_counts = _wipe_operational_data(
                            repo,
                            include_shipping_presets=include_shipping_presets,
                            include_document_templates=include_document_templates,
                            include_audit_logs=include_audit_logs,
                        )
                        deleted_summary = ", ".join(
                            f"{table}={count}" for table, count in sorted(deleted_counts.items())
                        )
                        st.success(f"Operational data wipe completed. Deleted rows: {deleted_summary}")
                    except Exception as exc:
                        repo.db.rollback()
                        st.error(f"Operational data wipe failed: {exc}")

    with tab_backups:
        st.markdown("### Database Backups")
        st.caption("Create SQL dumps, upload to S3, and run guarded restores.")
        policy_enabled = get_runtime_bool(repo, "backup_policy_enabled", False)
        policy_upload_to_s3 = get_runtime_bool(repo, "backup_policy_upload_to_s3", True)
        policy_cadence_hours = max(1, int(get_runtime_int(repo, "backup_policy_cadence_hours", 24)))
        policy_retention_days = max(1, int(get_runtime_int(repo, "backup_policy_retention_days", 30)))
        policy_drill_interval_days = max(1, int(get_runtime_int(repo, "backup_restore_drill_interval_days", 30)))
        policy_rto_target_minutes = max(1, int(get_runtime_int(repo, "backup_restore_rto_target_minutes", 60)))
        policy_owner = get_runtime_str(repo, "backup_policy_owner", "").strip()

        st.markdown("### Backup Policy")
        st.caption("Environment-scoped policy settings for cadence, retention, and restore-drill objectives.")
        with st.form("admin_backup_policy_form"):
            bp1, bp2 = st.columns(2)
            with bp1:
                backup_policy_enabled = st.checkbox(
                    "Backup Policy Enabled",
                    value=bool(policy_enabled),
                    key="admin_backup_policy_enabled",
                )
                backup_policy_upload_to_s3 = st.checkbox(
                    "Policy Requires S3 Upload",
                    value=bool(policy_upload_to_s3),
                    key="admin_backup_policy_upload_to_s3",
                )
                backup_policy_owner = st.text_input(
                    "Policy Owner",
                    value=policy_owner,
                    placeholder="ops@goldenstackers.com or on-call team",
                    key="admin_backup_policy_owner",
                )
            with bp2:
                backup_policy_cadence_hours = st.number_input(
                    "Backup Cadence (hours)",
                    min_value=1,
                    max_value=720,
                    value=int(policy_cadence_hours),
                    step=1,
                    key="admin_backup_policy_cadence_hours",
                )
                backup_policy_retention_days = st.number_input(
                    "Retention (days)",
                    min_value=1,
                    max_value=3650,
                    value=int(policy_retention_days),
                    step=1,
                    key="admin_backup_policy_retention_days",
                )
                backup_restore_drill_interval_days = st.number_input(
                    "Restore Drill Interval (days)",
                    min_value=1,
                    max_value=365,
                    value=int(policy_drill_interval_days),
                    step=1,
                    key="admin_backup_restore_drill_interval_days",
                )
                backup_restore_rto_target_minutes = st.number_input(
                    "Restore RTO Target (minutes)",
                    min_value=1,
                    max_value=10080,
                    value=int(policy_rto_target_minutes),
                    step=1,
                    key="admin_backup_restore_rto_target_minutes",
                )
            save_backup_policy = st.form_submit_button("Save Backup Policy")

        if save_backup_policy:
            try:
                updates = [
                    ("backup_policy_enabled", "true" if backup_policy_enabled else "false", "bool"),
                    ("backup_policy_upload_to_s3", "true" if backup_policy_upload_to_s3 else "false", "bool"),
                    ("backup_policy_owner", str(backup_policy_owner or "").strip(), "str"),
                    ("backup_policy_cadence_hours", str(int(backup_policy_cadence_hours)), "int"),
                    ("backup_policy_retention_days", str(int(backup_policy_retention_days)), "int"),
                    ("backup_restore_drill_interval_days", str(int(backup_restore_drill_interval_days)), "int"),
                    ("backup_restore_rto_target_minutes", str(int(backup_restore_rto_target_minutes)), "int"),
                ]
                descriptions = {
                    "backup_policy_enabled": "Enable scheduled backup policy reporting/tracking for this environment.",
                    "backup_policy_upload_to_s3": "Whether backups should be uploaded to S3 by policy.",
                    "backup_policy_owner": "Primary owner/team accountable for backup policy and drill execution.",
                    "backup_policy_cadence_hours": "Expected backup cadence in hours for compliance and readiness checks.",
                    "backup_policy_retention_days": "Expected backup retention window in days.",
                    "backup_restore_drill_interval_days": "Maximum target days between successful restore drills.",
                    "backup_restore_rto_target_minutes": "Target restore recovery-time objective (minutes) used for drill evidence.",
                }
                for key, value, value_type in updates:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=key,
                        value=value,
                        value_type=value_type,
                        description=descriptions.get(key, "Backup policy runtime setting."),
                        is_active=True,
                        actor=user.username,
                    )
                st.success("Backup policy settings saved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to save backup policy settings: {exc}")

        tools = pg_tools_status()
        tool_df = pd.DataFrame(
            [
                {"tool": "pg_dump", "available": tools["pg_dump"]},
                {"tool": "psql", "available": tools["psql"]},
            ]
        )
        st.dataframe(tool_df, use_container_width=True)
        if not tools["pg_dump"]:
            st.warning("`pg_dump` not available in app runtime. Install PostgreSQL client tools in image.")

        s3_enabled = s3_backup_enabled()
        st.caption(f"S3 backup target: `{settings.s3_bucket}` ({'enabled' if s3_enabled else 'disabled'})")

        st.markdown("### Create Backup")
        with st.form("admin_backup_create_form"):
            include_drop = st.checkbox("Include DROP statements (`--clean --if-exists`)", value=True)
            upload_to_s3 = st.checkbox("Upload backup to S3 after dump", value=s3_enabled, disabled=not s3_enabled)
            run_backup = st.form_submit_button("Create Backup Dump")

        if run_backup:
            try:
                backup = create_backup_dump(include_drop_statements=include_drop)
                backup_bytes = backup.file_path.read_bytes()
                st.session_state["admin_backup_name"] = backup.file_name
                st.session_state["admin_backup_bytes"] = backup_bytes
                st.success(f"Backup created: `{backup.file_name}` ({backup.size_bytes} bytes).")
                if upload_to_s3:
                    key = upload_backup_to_s3(backup.file_path)
                    st.success(f"Uploaded to S3 key `{key}`.")
            except Exception as exc:
                st.error(f"Backup failed: {exc}")

        backup_name = st.session_state.get("admin_backup_name")
        backup_bytes = st.session_state.get("admin_backup_bytes")
        if backup_name and backup_bytes:
            st.download_button(
                "Download Last Backup",
                data=backup_bytes,
                file_name=backup_name,
                mime="application/sql",
                key="admin_backup_download",
            )

        st.markdown("### S3 Backups")
        if s3_enabled:
            refresh = st.button("Refresh S3 Backup List")
            if refresh or "admin_backup_s3_rows" not in st.session_state:
                try:
                    st.session_state["admin_backup_s3_rows"] = list_backups_in_s3()
                except Exception as exc:
                    st.error(f"Unable to list S3 backups: {exc}")
                    st.session_state["admin_backup_s3_rows"] = []
            s3_rows = st.session_state.get("admin_backup_s3_rows", [])
            if s3_rows:
                st.dataframe(pd.DataFrame(s3_rows), use_container_width=True)
            else:
                st.info("No backups found in S3 prefix.")
        else:
            st.info("Enable S3 configuration to use backup upload/list/restore from bucket.")

        st.markdown("### Restore")
        st.caption("Restore is blocked in prod. Prefer maintenance window and app downtime.")
        if settings.app_env.lower() == "prod":
            st.warning("Restore is disabled in `APP_ENV=prod`.")
        else:
            restore_source = st.radio(
                "Restore Source",
                options=["upload_sql_file", "s3_backup_key"],
                format_func=lambda x: "Upload SQL file" if x == "upload_sql_file" else "S3 backup key",
            )

            uploaded_restore = None
            selected_s3_key = ""
            if restore_source == "upload_sql_file":
                uploaded_restore = st.file_uploader("SQL Dump File", type=["sql"])
            else:
                s3_rows = st.session_state.get("admin_backup_s3_rows", [])
                key_options = [row.get("key", "") for row in s3_rows if row.get("key")]
                if key_options:
                    selected_s3_key = st.selectbox("S3 Backup Key", options=key_options)
                else:
                    st.info("Refresh S3 backup list above to select a key.")

            with st.form("admin_backup_restore_form"):
                confirm_restore = st.checkbox("I understand restore can overwrite data and interrupt the app.")
                restore_phrase = st.text_input("Type RESTORE to confirm")
                run_restore = st.form_submit_button("Run Restore")

            if run_restore:
                if not confirm_restore or restore_phrase.strip() != "RESTORE":
                    st.error("Confirm restore acknowledgement and type `RESTORE` exactly.")
                else:
                    try:
                        repo.db.rollback()
                        if restore_source == "upload_sql_file":
                            if uploaded_restore is None:
                                raise RuntimeError("Upload a SQL file to restore.")
                            temp_path = Path(f"/tmp/{uploaded_restore.name}")
                            temp_path.write_bytes(uploaded_restore.getvalue())
                            restore_dump_file(temp_path)
                        else:
                            if not selected_s3_key:
                                raise RuntimeError("Select an S3 backup key to restore.")
                            downloaded = download_backup_from_s3(selected_s3_key)
                            restore_dump_file(downloaded)
                        st.success("Restore completed. Reloading app state.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Restore failed: {exc}")

        st.markdown("### Restore Drill Evidence")
        st.caption("Track restore drills with outcome, duration, and source details for disaster recovery auditability.")
        recent_drill_logs = repo.db.scalars(
            select(AuditLog)
            .where(AuditLog.entity_type == "backup_restore_drill")
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(200)
        ).all()
        drill_rows: list[dict[str, Any]] = []
        for row in recent_drill_logs:
            payload = _audit_changes(row)
            result = str(payload.get("result") or "").strip().lower()
            duration_minutes = payload.get("duration_minutes")
            duration_minutes_int = int(duration_minutes) if str(duration_minutes or "").isdigit() else None
            drill_rows.append(
                {
                    "id": int(row.id),
                    "recorded_at_utc": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
                    "actor": str(row.actor or ""),
                    "target_env": str(payload.get("target_env") or settings.app_env),
                    "drill_date": str(payload.get("drill_date") or ""),
                    "result": result,
                    "source_type": str(payload.get("source_type") or ""),
                    "source_ref": str(payload.get("source_ref") or ""),
                    "duration_minutes": duration_minutes_int,
                    "rto_target_minutes": payload.get("rto_target_minutes"),
                    "rto_met": payload.get("rto_met"),
                    "notes": str(payload.get("notes") or "")[:240],
                }
            )
        drill_df = pd.DataFrame(drill_rows)
        latest_pass_days: int | None = None
        if not drill_df.empty:
            pass_rows = drill_df[drill_df["result"].astype(str) == "pass"]
            if not pass_rows.empty:
                latest_pass_iso = str(pass_rows.iloc[0].get("recorded_at_utc") or "").strip()
                try:
                    latest_pass_ts = datetime.fromisoformat(latest_pass_iso)
                    latest_pass_days = max(0, (utcnow_naive() - latest_pass_ts).days)
                except Exception:
                    latest_pass_days = None
        d1, d2, d3 = st.columns(3)
        d1.metric("Restore Drill Events", int(len(drill_df)))
        d2.metric("Latest Pass Age (days)", "n/a" if latest_pass_days is None else int(latest_pass_days))
        d3.metric("Drill SLA (days)", int(policy_drill_interval_days))
        if latest_pass_days is not None and latest_pass_days > int(policy_drill_interval_days):
            st.warning(
                f"Restore drill SLA breached: last passing drill is {latest_pass_days} days old "
                f"(target <= {int(policy_drill_interval_days)} days)."
            )

        with st.form("admin_backup_restore_drill_event_form"):
            r1, r2 = st.columns(2)
            with r1:
                drill_date = st.date_input("Drill Date", value=datetime.now().date())
                drill_result = st.selectbox("Result", options=["pass", "partial", "fail"], index=0)
                drill_target_env = st.text_input("Target Environment", value=settings.app_env)
                drill_source_type = st.selectbox(
                    "Restore Source Type",
                    options=["s3_backup_key", "upload_sql_file", "local_file", "other"],
                    index=0,
                )
            with r2:
                drill_source_ref = st.text_input("Restore Source Reference", placeholder="s3://bucket/key or filename")
                drill_duration_minutes = st.number_input(
                    "Restore Duration (minutes)",
                    min_value=0,
                    max_value=10080,
                    value=0,
                    step=1,
                )
                drill_rto_target_minutes = st.number_input(
                    "RTO Target (minutes)",
                    min_value=1,
                    max_value=10080,
                    value=int(policy_rto_target_minutes),
                    step=1,
                )
            drill_notes = st.text_area(
                "Notes / Recovery Evidence",
                placeholder="What was restored, validation checks performed, issues, follow-ups.",
            )
            record_drill_event = st.form_submit_button("Record Restore Drill Event")
        if record_drill_event:
            try:
                duration_value = int(drill_duration_minutes)
                rto_target_value = int(drill_rto_target_minutes)
                repo.record_audit_event(
                    entity_type="backup_restore_drill",
                    entity_id=None,
                    action="record",
                    actor=user.username,
                    changes={
                        "target_env": str(drill_target_env or settings.app_env).strip(),
                        "drill_date": str(drill_date.isoformat()),
                        "result": str(drill_result or "").strip().lower(),
                        "source_type": str(drill_source_type or "").strip(),
                        "source_ref": str(drill_source_ref or "").strip(),
                        "duration_minutes": duration_value,
                        "rto_target_minutes": rto_target_value,
                        "rto_met": bool(duration_value <= rto_target_value),
                        "notes": str(drill_notes or "").strip(),
                    },
                )
                st.success("Restore drill event recorded.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to record restore drill event: {exc}")

        if drill_df.empty:
            st.caption("No restore drill evidence recorded yet.")
        else:
            st.dataframe(drill_df, use_container_width=True, hide_index=True)
            st.download_button(
                "Download Restore Drill Evidence CSV",
                data=drill_df.to_csv(index=False).encode("utf-8"),
                file_name=f"backup_restore_drills_{settings.app_env}.csv",
                mime="text/csv",
                key="admin_backup_restore_drills_download",
            )

        st.markdown("### DR Checklist + SLA Reporting")
        st.caption("Record environment-specific DR checklist snapshots and view restore-drill SLA coverage by environment.")
        with st.form("admin_backup_dr_checklist_form"):
            c1, c2 = st.columns(2)
            with c1:
                checklist_target_env = st.selectbox(
                    "Checklist Target Environment",
                    options=["local", "dev", "prod", settings.app_env],
                    index=0,
                )
                checklist_owner = st.text_input(
                    "Checklist Owner",
                    placeholder="name/team",
                )
                checklist_evidence_link = st.text_input(
                    "Evidence Link",
                    placeholder="runbook link, ticket, or artifact URL",
                )
            with c2:
                item_policy_reviewed = st.checkbox("Backup policy reviewed for target environment", value=True)
                item_recent_backup_verified = st.checkbox("Recent backup artifact verified", value=True)
                item_restore_drill_within_sla = st.checkbox("Restore drill is within SLA window", value=True)
                item_restore_validation_smoke = st.checkbox("Post-restore validation smoke tests completed", value=True)
                item_rto_documented = st.checkbox("RTO/RPO notes documented", value=True)
            checklist_notes = st.text_area(
                "Checklist Notes",
                placeholder="Open gaps, owners, remediation ETA, and sign-off comments.",
            )
            record_dr_checklist = st.form_submit_button("Record DR Checklist Snapshot")
        if record_dr_checklist:
            try:
                items = {
                    "backup_policy_reviewed": bool(item_policy_reviewed),
                    "recent_backup_verified": bool(item_recent_backup_verified),
                    "restore_drill_within_sla": bool(item_restore_drill_within_sla),
                    "restore_validation_smoke_tests": bool(item_restore_validation_smoke),
                    "rto_rpo_documented": bool(item_rto_documented),
                }
                completed_count = sum(1 for v in items.values() if bool(v))
                total_count = len(items)
                completion_pct = round((float(completed_count) / float(total_count) * 100.0), 2) if total_count else 0.0
                repo.record_audit_event(
                    entity_type="backup_dr_checklist",
                    entity_id=None,
                    action="snapshot",
                    actor=user.username,
                    changes={
                        "target_env": str(checklist_target_env or settings.app_env).strip().lower(),
                        "owner": str(checklist_owner or "").strip(),
                        "evidence_link": str(checklist_evidence_link or "").strip(),
                        "items": items,
                        "completed_count": int(completed_count),
                        "total_count": int(total_count),
                        "completion_percent": float(completion_pct),
                        "notes": str(checklist_notes or "").strip(),
                    },
                )
                st.success("DR checklist snapshot recorded.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to record DR checklist snapshot: {exc}")

        if not drill_df.empty:
            sla_df = drill_df.copy()
            sla_df["target_env"] = sla_df["target_env"].astype(str).str.strip().replace("", settings.app_env)
            sla_df["result"] = sla_df["result"].astype(str).str.strip().str.lower()
            env_rows: list[dict[str, Any]] = []
            for env_name in sorted({str(v).strip() for v in sla_df["target_env"].tolist() if str(v).strip()}):
                env_df = sla_df[sla_df["target_env"] == env_name].copy()
                pass_df = env_df[env_df["result"] == "pass"].copy()
                latest_event = str(env_df["recorded_at_utc"].iloc[0] or "") if not env_df.empty else ""
                latest_pass = str(pass_df["recorded_at_utc"].iloc[0] or "") if not pass_df.empty else ""
                latest_pass_age_days: int | None = None
                if latest_pass:
                    try:
                        latest_pass_ts = datetime.fromisoformat(latest_pass)
                        latest_pass_age_days = max(0, (utcnow_naive() - latest_pass_ts).days)
                    except Exception:
                        latest_pass_age_days = None
                pass_count = int(len(pass_df))
                total_count = int(len(env_df))
                pass_rate_pct = round((float(pass_count) / float(total_count) * 100.0), 2) if total_count > 0 else 0.0
                sla_status = (
                    "breach"
                    if (latest_pass_age_days is None or latest_pass_age_days > int(policy_drill_interval_days))
                    else "ok"
                )
                env_rows.append(
                    {
                        "target_env": env_name,
                        "drills_total": total_count,
                        "drills_pass": pass_count,
                        "pass_rate_percent": pass_rate_pct,
                        "latest_event_at_utc": latest_event,
                        "latest_pass_at_utc": latest_pass,
                        "latest_pass_age_days": latest_pass_age_days,
                        "sla_target_days": int(policy_drill_interval_days),
                        "sla_status": sla_status,
                    }
                )
            if env_rows:
                env_sla_df = pd.DataFrame(env_rows).sort_values(["sla_status", "target_env"], ascending=[True, True])
                st.caption("Restore Drill SLA by Environment")
                st.dataframe(env_sla_df, use_container_width=True, hide_index=True)
                st.download_button(
                    "Download DR SLA Report CSV",
                    data=env_sla_df.to_csv(index=False).encode("utf-8"),
                    file_name=f"backup_restore_sla_{settings.app_env}.csv",
                    mime="text/csv",
                    key="admin_backup_restore_sla_download",
                )

        checklist_logs = repo.db.scalars(
            select(AuditLog)
            .where(AuditLog.entity_type == "backup_dr_checklist")
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(100)
        ).all()
        checklist_rows: list[dict[str, Any]] = []
        for row in checklist_logs:
            payload = _audit_changes(row)
            items = payload.get("items") if isinstance(payload.get("items"), dict) else {}
            checklist_rows.append(
                {
                    "id": int(row.id),
                    "recorded_at_utc": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
                    "actor": str(row.actor or ""),
                    "target_env": str(payload.get("target_env") or ""),
                    "owner": str(payload.get("owner") or ""),
                    "evidence_link": str(payload.get("evidence_link") or ""),
                    "completed_count": payload.get("completed_count"),
                    "total_count": payload.get("total_count"),
                    "completion_percent": payload.get("completion_percent"),
                    "backup_policy_reviewed": items.get("backup_policy_reviewed"),
                    "recent_backup_verified": items.get("recent_backup_verified"),
                    "restore_drill_within_sla": items.get("restore_drill_within_sla"),
                    "restore_validation_smoke_tests": items.get("restore_validation_smoke_tests"),
                    "rto_rpo_documented": items.get("rto_rpo_documented"),
                    "notes": str(payload.get("notes") or "")[:220],
                }
            )
        checklist_df = pd.DataFrame(checklist_rows)
        if checklist_df.empty:
            st.caption("No DR checklist snapshots recorded yet.")
        else:
            st.caption("Recent DR Checklist Snapshots")
            st.dataframe(checklist_df, use_container_width=True, hide_index=True)
            st.download_button(
                "Download DR Checklist Snapshots CSV",
                data=checklist_df.to_csv(index=False).encode("utf-8"),
                file_name=f"backup_dr_checklist_snapshots_{settings.app_env}.csv",
                mime="text/csv",
                key="admin_backup_dr_checklist_download",
            )

    with tab_ebay_verify:
        st.markdown("### eBay API Verification")
        st.caption("Validate eBay credentials and confirm token/API calls succeed from this runtime.")
        client = EbayClient()

        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "field": "Environment",
                        "value": settings.ebay_environment,
                    },
                    {
                        "field": "Client ID",
                        "value": _mask_secret(settings.ebay_client_id),
                    },
                    {
                        "field": "Client Secret",
                        "value": _mask_secret(settings.ebay_client_secret),
                    },
                    {
                        "field": "RU Name",
                        "value": settings.ebay_ru_name or "(not set)",
                    },
                    {
                        "field": "User Access Token",
                        "value": "set" if settings.ebay_user_access_token.strip() else "(not set)",
                    },
                    {
                        "field": "Configured",
                        "value": "yes" if client.is_configured() else "no",
                    },
                ]
            ),
            use_container_width=True,
        )

        if not client.is_configured():
            st.warning("Set EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, and EBAY_RU_NAME before running verification.")
        else:
            st.caption("Step 1: Validate app keys using client-credentials token grant.")
            with st.form("admin_ebay_app_token_verify_form"):
                scope = st.selectbox(
                    "Scope",
                    options=EbayClient.SCOPES,
                    index=0,
                    help="Use base `api_scope` first for key validation.",
                )
                run_app_verify = st.form_submit_button("Verify App Token")

            if run_app_verify:
                try:
                    token_payload = client.fetch_application_token(scopes=[scope])
                    st.success("App token request succeeded. eBay keys are valid for this environment.")
                    st.json(
                        {
                            "token_type": token_payload.get("token_type"),
                            "expires_in": token_payload.get("expires_in"),
                            "scope": token_payload.get("scope"),
                        }
                    )
                except Exception as exc:
                    st.error(f"App token verification failed: {exc}")

            st.caption("Step 2: Optional user-token API check (Sell Account privileges endpoint).")
            verify_user_token = st.text_area(
                "Access Token",
                height=120,
                key="admin_ebay_verify_user_token",
                value=settings.ebay_user_access_token,
                help="Paste a user OAuth access token from eBay OAuth code exchange.",
            )
            if st.button("Verify User Token API Access", key="admin_ebay_verify_user_token_button"):
                if not verify_user_token.strip():
                    st.error("Paste an access token first.")
                else:
                    try:
                        privileges = client.get_account_privileges(verify_user_token.strip())
                        st.success("User token API call succeeded.")
                        st.json(privileges)
                    except Exception as exc:
                        st.error(f"User token verification failed: {exc}")

    with tab_ai_runtime:
        st.markdown("### AI Provider Runtime")
        st.caption(
            "Configure OpenAI/LocalAI runtime profiles in DB. "
            "Comp Tool uses the default active profile for this environment."
        )
        try:
            ai_rows = repo.list_ai_provider_configs(environment=settings.app_env, active_only=False)
        except Exception as exc:
            st.error(
                "AI provider config table is not available yet. "
                "Run database migrations first (`docker compose run --rm migrate`)."
            )
            st.caption(f"Details: {exc}")
            ai_rows = []

        if ai_rows:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "id": row.id,
                            "environment": row.environment,
                            "name": row.name,
                            "provider": row.provider,
                            "model": row.model,
                            "multimodal_model": row.multimodal_model,
                            "base_url": row.base_url,
                            "endpoint_type": row.endpoint_type,
                            "api_key": _mask_secret(row.api_key),
                            "temperature": float(row.temperature),
                            "max_output_tokens": row.max_output_tokens,
                            "timeout_seconds": row.timeout_seconds,
                            "is_default": bool(row.is_default),
                            "is_active": bool(row.is_active),
                        }
                        for row in ai_rows
                    ]
                ),
                use_container_width=True,
            )
        else:
            st.info("No AI runtime profiles found for this environment.")

        st.markdown("### Add/Update AI Runtime Profile")
        create_models_state_key = "admin_ai_create_model_options"
        create_model_options = list(st.session_state.get(create_models_state_key) or [])
        create_model_choice_options = ["(manual entry)"] + create_model_options
        st.caption(f"Endpoint model options loaded: `{len(create_model_options)}`")
        with st.form("admin_ai_runtime_upsert_form", clear_on_submit=False):
            c1, c2, c3 = st.columns(3)
            with c1:
                profile_name = st.text_input("Profile Name", value="default-openai")
            with c2:
                provider = st.selectbox("Provider", options=["openai", "localai"], index=0)
            with c3:
                endpoint_type = st.selectbox(
                    "Endpoint Type",
                    options=["responses", "chat_completions"],
                    index=0,
                    help="Use `responses` for OpenAI responses API. LocalAI usually uses `chat_completions`.",
                )
            d1, d2 = st.columns(2)
            with d1:
                selected_create_model = st.selectbox(
                    "Model (from endpoint)",
                    options=create_model_choice_options,
                    index=0,
                    help="Use Query /models to populate this list, or use manual entry.",
                )
            with d2:
                base_url = st.text_input("Base URL", value="https://api.openai.com/v1")
            create_model_manual = st.text_input(
                "Model (manual override)",
                value="gpt-4o-mini",
                help="If set, this value is used instead of the dropdown.",
            )
            selected_create_mm_model = st.selectbox(
                "Multimodal Model (from endpoint)",
                options=create_model_choice_options,
                index=0,
                help="Optional vision model for image/camera tools.",
            )
            create_mm_model_manual = st.text_input(
                "Multimodal Model (manual override, optional)",
                value="",
                help="Leave blank to use Text Model.",
            )
            api_key = st.text_input(
                "API Key / Token (optional for LocalAI)",
                value="",
                type="password",
                help="Stored in DB for runtime use. When updating an existing profile, blank keeps current key.",
            )
            e1, e2, e3 = st.columns(3)
            with e1:
                temperature = st.number_input("Temperature", min_value=0.0, max_value=2.0, value=0.2, step=0.1)
            with e2:
                max_output_tokens = st.number_input(
                    "Max Output Tokens", min_value=1, max_value=4096, value=600, step=50
                )
            with e3:
                timeout_seconds = st.number_input(
                    "Timeout Seconds", min_value=5, max_value=300, value=60, step=5
                )
            notes = st.text_area("Notes", value="")
            f1, f2 = st.columns(2)
            with f1:
                is_default = st.checkbox("Set default for this environment", value=False)
            with f2:
                is_active = st.checkbox("Active", value=True)
            s1, s2 = st.columns(2)
            with s1:
                query_models_create = st.form_submit_button("Query /models")
            with s2:
                save_profile = st.form_submit_button("Save AI Runtime Profile")

        if query_models_create:
            try:
                token_for_query = (api_key or "").strip()
                if provider == "openai" and not token_for_query:
                    st.error("Provide API key/token to query OpenAI `/models`.")
                else:
                    loaded = fetch_available_models(
                        base_url=(base_url or "").strip(),
                        api_key=token_for_query,
                        timeout_seconds=int(timeout_seconds),
                    )
                    st.session_state[create_models_state_key] = loaded
                    st.success(f"Loaded {len(loaded)} model(s) from endpoint.")
                    st.rerun()
            except Exception as exc:
                st.error(f"Unable to load models from endpoint: {exc}")

        if save_profile:
            try:
                resolved_model = (create_model_manual or "").strip()
                if not resolved_model and selected_create_model != "(manual entry)":
                    resolved_model = selected_create_model
                resolved_mm_model = (create_mm_model_manual or "").strip()
                if not resolved_mm_model and selected_create_mm_model != "(manual entry)":
                    resolved_mm_model = selected_create_mm_model
                if not resolved_mm_model:
                    resolved_mm_model = resolved_model
                if not resolved_model:
                    st.error("Text model is required. Select from dropdown or provide manual override.")
                else:
                    row = repo.upsert_ai_provider_config(
                        environment=settings.app_env,
                        name=profile_name.strip(),
                        provider=provider,
                        model=resolved_model,
                        multimodal_model=resolved_mm_model,
                        base_url=base_url.strip(),
                        endpoint_type=endpoint_type,
                        api_key=api_key.strip(),
                        temperature=Decimal(str(temperature)),
                        max_output_tokens=int(max_output_tokens),
                        timeout_seconds=int(timeout_seconds),
                        notes=notes.strip(),
                        is_default=bool(is_default),
                        is_active=bool(is_active),
                        actor=user.username,
                    )
                    st.success(f"Saved AI runtime profile `{row.name}`.")
                    st.rerun()
            except Exception as exc:
                st.error(f"Unable to save profile: {exc}")

        if ai_rows:
            st.markdown("### Manage Existing Profile")
            profile_map = {
                f"#{row.id} | {row.name} | {row.provider} | default={row.is_default} | active={row.is_active}": row
                for row in ai_rows
            }
            selected_key = st.selectbox("Profile", options=list(profile_map.keys()), key="admin_ai_profile_select")
            selected_profile = profile_map[selected_key]

            selected_id_state_key = "admin_ai_edit_selected_profile_id"
            selected_id = int(selected_profile.id)

            def _load_selected_ai_profile_into_state() -> None:
                st.session_state["admin_ai_edit_name"] = (selected_profile.name or "").strip()
                st.session_state["admin_ai_edit_provider"] = (selected_profile.provider or "openai").strip().lower()
                st.session_state["admin_ai_edit_endpoint"] = (
                    selected_profile.endpoint_type or "responses"
                ).strip().lower()
                st.session_state["admin_ai_edit_model"] = (selected_profile.model or "").strip()
                st.session_state["admin_ai_edit_mm_model"] = (
                    (selected_profile.multimodal_model or "").strip()
                    or (selected_profile.model or "").strip()
                )
                st.session_state["admin_ai_edit_base_url"] = (selected_profile.base_url or "").strip()
                st.session_state["admin_ai_edit_api_key"] = ""
                st.session_state["admin_ai_edit_temp"] = float(selected_profile.temperature)
                st.session_state["admin_ai_edit_max_tokens"] = int(selected_profile.max_output_tokens)
                st.session_state["admin_ai_edit_timeout"] = int(selected_profile.timeout_seconds)
                st.session_state["admin_ai_edit_notes"] = (selected_profile.notes or "").strip()
                st.session_state["admin_ai_edit_default"] = bool(selected_profile.is_default)
                st.session_state["admin_ai_edit_active"] = bool(selected_profile.is_active)
                st.session_state[selected_id_state_key] = selected_id

            previous_selected_id = int(st.session_state.get(selected_id_state_key) or 0)
            if previous_selected_id != selected_id:
                _load_selected_ai_profile_into_state()

            st.caption("Edit all fields on the selected profile. Leave API key blank to keep existing key.")
            edit_models_state_key = f"admin_ai_edit_model_options_{selected_profile.id}"
            edit_model_options = list(st.session_state.get(edit_models_state_key) or [])
            default_edit_model = str(st.session_state.get("admin_ai_edit_model") or "").strip()
            default_edit_mm_model = str(st.session_state.get("admin_ai_edit_mm_model") or "").strip()
            if default_edit_model and default_edit_model not in edit_model_options:
                edit_model_options.append(default_edit_model)
            if default_edit_mm_model and default_edit_mm_model not in edit_model_options:
                edit_model_options.append(default_edit_mm_model)
            edit_model_options = sorted({m for m in edit_model_options if m})
            edit_model_choice_options = ["(manual entry)"] + edit_model_options
            st.caption(f"Endpoint model options loaded: `{len(edit_model_options)}`")

            with st.form(f"admin_ai_edit_form_{selected_profile.id}"):
                h1, h2, h3 = st.columns(3)
                with h1:
                    edit_name = st.text_input(
                        "Profile Name",
                        key="admin_ai_edit_name",
                    )
                with h2:
                    edit_provider = st.selectbox(
                        "Provider",
                        options=["openai", "localai"],
                        key="admin_ai_edit_provider",
                    )
                with h3:
                    edit_endpoint = st.selectbox(
                        "Endpoint Type",
                        options=["responses", "chat_completions"],
                        key="admin_ai_edit_endpoint",
                    )

                i1, i2 = st.columns(2)
                with i1:
                    edit_model_pick = st.selectbox(
                        "Text Model (from endpoint)",
                        options=edit_model_choice_options,
                        index=edit_model_choice_options.index(default_edit_model)
                        if default_edit_model in edit_model_choice_options
                        else 0,
                    )
                with i2:
                    edit_multimodal_model_pick = st.selectbox(
                        "Multimodal Model (from endpoint)",
                        options=edit_model_choice_options,
                        index=edit_model_choice_options.index(default_edit_mm_model)
                        if default_edit_mm_model in edit_model_choice_options
                        else 0,
                    )
                edit_model = st.text_input(
                    "Text Model (manual override)",
                    key="admin_ai_edit_model",
                    help="If set, this value is used instead of the dropdown.",
                )
                edit_multimodal_model = st.text_input(
                    "Multimodal Model (manual override)",
                    key="admin_ai_edit_mm_model",
                    help="Leave blank to use Text Model.",
                )

                edit_base_url = st.text_input(
                    "Base URL",
                    key="admin_ai_edit_base_url",
                )
                edit_api_key = st.text_input(
                    "API Key / Token (optional)",
                    type="password",
                    help="Leave blank to keep current API key.",
                    key="admin_ai_edit_api_key",
                )

                j1, j2, j3 = st.columns(3)
                with j1:
                    edit_temperature = st.number_input(
                        "Temperature",
                        min_value=0.0,
                        max_value=2.0,
                        step=0.1,
                        key="admin_ai_edit_temp",
                    )
                with j2:
                    edit_max_output_tokens = st.number_input(
                        "Max Output Tokens",
                        min_value=1,
                        max_value=4096,
                        step=50,
                        key="admin_ai_edit_max_tokens",
                    )
                with j3:
                    edit_timeout_seconds = st.number_input(
                        "Timeout Seconds",
                        min_value=5,
                        max_value=300,
                        step=5,
                        key="admin_ai_edit_timeout",
                    )

                edit_notes = st.text_area(
                    "Notes",
                    key="admin_ai_edit_notes",
                )
                k1, k2 = st.columns(2)
                with k1:
                    edit_is_default = st.checkbox(
                        "Set default for this environment",
                        key="admin_ai_edit_default",
                    )
                with k2:
                    edit_is_active = st.checkbox(
                        "Active",
                        key="admin_ai_edit_active",
                    )
                q1, q2 = st.columns(2)
                with q1:
                    query_models_edit = st.form_submit_button("Query /models For Selected")
                with q2:
                    save_existing_profile = st.form_submit_button("Save Changes to Selected Profile")

            if query_models_edit:
                try:
                    token_for_query = (edit_api_key or "").strip() or str(selected_profile.api_key or "").strip()
                    if edit_provider == "openai" and not token_for_query:
                        st.error("Provide API key/token to query OpenAI `/models`.")
                    else:
                        loaded = fetch_available_models(
                            base_url=(edit_base_url or "").strip(),
                            api_key=token_for_query,
                            timeout_seconds=int(edit_timeout_seconds),
                        )
                        st.session_state[edit_models_state_key] = loaded
                        st.success(f"Loaded {len(loaded)} model(s) for selected profile.")
                        st.rerun()
                except Exception as exc:
                    st.error(f"Unable to load models from endpoint: {exc}")

            if save_existing_profile:
                try:
                    resolved_edit_model = (edit_model or "").strip()
                    if not resolved_edit_model and edit_model_pick != "(manual entry)":
                        resolved_edit_model = edit_model_pick
                    resolved_edit_mm_model = (edit_multimodal_model or "").strip()
                    if not resolved_edit_mm_model and edit_multimodal_model_pick != "(manual entry)":
                        resolved_edit_mm_model = edit_multimodal_model_pick
                    if not resolved_edit_mm_model:
                        resolved_edit_mm_model = resolved_edit_model
                    if not resolved_edit_model:
                        st.error("Text model is required. Select from dropdown or provide manual override.")
                    else:
                        updates = {
                            "name": edit_name.strip(),
                            "provider": edit_provider,
                            "endpoint_type": edit_endpoint,
                            "model": resolved_edit_model,
                            "multimodal_model": resolved_edit_mm_model,
                            "base_url": edit_base_url.strip().rstrip("/"),
                            "temperature": Decimal(str(edit_temperature)),
                            "max_output_tokens": int(edit_max_output_tokens),
                            "timeout_seconds": int(edit_timeout_seconds),
                            "notes": edit_notes.strip(),
                            "is_default": bool(edit_is_default),
                            "is_active": bool(edit_is_active),
                        }
                        if edit_api_key.strip():
                            updates["api_key"] = edit_api_key.strip()
                        repo.update_ai_provider_config(selected_profile.id, updates, actor=user.username)
                        st.success("Selected AI runtime profile updated.")
                        st.rerun()
                except Exception as exc:
                    st.error(f"Unable to update selected profile: {exc}")

            if st.button("Test Selected Profile", key=f"admin_ai_test_profile_{selected_profile.id}"):
                try:
                    test_payload = validate_llm_runtime_config(
                        LLMRuntimeConfig(
                            source="db",
                            enabled=bool(selected_profile.is_active),
                            provider=(selected_profile.provider or "openai").strip().lower(),
                            model=(selected_profile.model or "").strip(),
                            multimodal_model=((selected_profile.multimodal_model or "").strip() or (selected_profile.model or "").strip()),
                            base_url=(selected_profile.base_url or "").strip().rstrip("/"),
                            endpoint_type=(selected_profile.endpoint_type or "responses").strip().lower(),
                            api_key=(selected_profile.api_key or "").strip(),
                            temperature=float(selected_profile.temperature),
                            max_output_tokens=int(selected_profile.max_output_tokens),
                            timeout_seconds=int(selected_profile.timeout_seconds),
                        )
                    )
                    st.success("AI runtime test succeeded.")
                    st.json(test_payload)
                except Exception as exc:
                    st.error(f"AI runtime test failed: {exc}")

            with st.form(f"admin_ai_delete_form_{selected_profile.id}"):
                confirm_delete_ai = st.checkbox("I understand this deletes the selected profile.")
                delete_phrase_ai = st.text_input("Type DELETE to confirm")
                delete_submit_ai = st.form_submit_button("Delete Selected Profile")
            if delete_submit_ai:
                if not confirm_delete_ai or delete_phrase_ai.strip() != "DELETE":
                    st.error("Confirm deletion and type `DELETE` exactly.")
                else:
                    try:
                        repo.delete_ai_provider_config_by_id(config_id=selected_profile.id, actor=user.username)
                        st.success("AI runtime profile deleted.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Unable to delete profile: {exc}")

        st.divider()
        _render_voice_runtime_editor(repo, user)

        st.divider()
        _render_ai_domain_toggles_editor(repo, user)

        st.divider()
        st.markdown("### Comp AI Prompt Templates")
        st.caption(
            "Edit the prompt instruction and system message used by Comp Tool AI summaries. "
            "Changes apply immediately (runtime settings with env/default fallback)."
        )
        current_system_message_row = repo.get_runtime_setting(
            environment=settings.app_env,
            key="comp_llm_system_message",
            active_only=False,
        )
        current_instruction_row = repo.get_runtime_setting(
            environment=settings.app_env,
            key="comp_llm_instruction_template",
            active_only=False,
        )
        current_system_message = (
            (current_system_message_row.value if current_system_message_row else DEFAULT_COMP_SYSTEM_MESSAGE) or ""
        )
        current_instruction = (
            (current_instruction_row.value if current_instruction_row else DEFAULT_COMP_INSTRUCTION) or ""
        )

        with st.form("admin_comp_prompt_templates_form"):
            edited_system_message = st.text_area(
                "System Message",
                value=current_system_message,
                height=100,
            )
            edited_instruction = st.text_area(
                "Instruction Template",
                value=current_instruction,
                height=220,
            )
            csave1, csave2 = st.columns(2)
            with csave1:
                save_prompt_templates = st.form_submit_button("Save Prompt Templates")
            with csave2:
                reset_prompt_templates = st.form_submit_button("Reset to App Defaults")

        if save_prompt_templates:
            try:
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="comp_llm_system_message",
                    value=(edited_system_message or "").strip() or DEFAULT_COMP_SYSTEM_MESSAGE,
                    value_type="str",
                    description="System message for AI comp synthesis prompts.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="comp_llm_instruction_template",
                    value=(edited_instruction or "").strip() or DEFAULT_COMP_INSTRUCTION,
                    value_type="str",
                    description="Instruction template for AI comp synthesis prompts.",
                    is_active=True,
                    actor=user.username,
                )
                st.success("Comp AI prompt templates saved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to save prompt templates: {exc}")

        if reset_prompt_templates:
            try:
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="comp_llm_system_message",
                    value=DEFAULT_COMP_SYSTEM_MESSAGE,
                    value_type="str",
                    description="System message for AI comp synthesis prompts.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="comp_llm_instruction_template",
                    value=DEFAULT_COMP_INSTRUCTION,
                    value_type="str",
                    description="Instruction template for AI comp synthesis prompts.",
                    is_active=True,
                    actor=user.username,
                )
                st.success("Comp AI prompt templates reset to defaults.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to reset prompt templates: {exc}")

        st.divider()
        st.markdown("### Ask GoldenStackers AI Refinement")
        st.caption(
            "Optional post-processing pass for chat answers using AI orchestration fallback profiles. "
            "Read-only chat guardrails still apply."
        )
        chat_refine_enabled_row = repo.get_runtime_setting(
            environment=settings.app_env,
            key="chat_ai_refine_enabled",
            active_only=False,
        )
        chat_refine_system_row = repo.get_runtime_setting(
            environment=settings.app_env,
            key="chat_ai_refine_system_message",
            active_only=False,
        )
        chat_refine_instruction_row = repo.get_runtime_setting(
            environment=settings.app_env,
            key="chat_ai_refine_instruction",
            active_only=False,
        )
        chat_refine_enabled_default = (
            str(chat_refine_enabled_row.value if chat_refine_enabled_row is not None else "false")
            .strip()
            .lower()
            in {"1", "true", "yes", "on", "y"}
        )
        chat_refine_system_default = (
            (chat_refine_system_row.value if chat_refine_system_row is not None else "")
            or (
                "You are GoldenStackers' read-only operations copilot. "
                "Preserve factual values from the provided draft answer and citations."
            )
        )
        chat_refine_instruction_default = (
            (chat_refine_instruction_row.value if chat_refine_instruction_row is not None else "")
            or (
                "Rewrite the draft answer for clarity and operator usefulness. "
                "Do not invent values. Keep output concise markdown with short bullets."
            )
        )
        with st.form("admin_chat_refine_form"):
            edited_chat_refine_enabled = st.checkbox(
                "Enable AI refinement for Ask GoldenStackers",
                value=bool(chat_refine_enabled_default),
            )
            edited_chat_refine_system = st.text_area(
                "Chat Refine System Message",
                value=str(chat_refine_system_default),
                height=110,
            )
            edited_chat_refine_instruction = st.text_area(
                "Chat Refine Instruction Template",
                value=str(chat_refine_instruction_default),
                height=200,
            )
            rc1, rc2 = st.columns(2)
            with rc1:
                save_chat_refine = st.form_submit_button("Save Chat Refinement Settings")
            with rc2:
                reset_chat_refine = st.form_submit_button("Reset Chat Refinement Defaults")

        if save_chat_refine:
            try:
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="chat_ai_refine_enabled",
                    value="true" if edited_chat_refine_enabled else "false",
                    value_type="bool",
                    description="Enable/disable orchestration-backed AI refinement pass for Ask GoldenStackers responses.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="chat_ai_refine_system_message",
                    value=(edited_chat_refine_system or "").strip()
                    or (
                        "You are GoldenStackers' read-only operations copilot. "
                        "Preserve factual values from the provided draft answer and citations."
                    ),
                    value_type="str",
                    description="System message used for Ask GoldenStackers AI refinement pass.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="chat_ai_refine_instruction",
                    value=(edited_chat_refine_instruction or "").strip()
                    or (
                        "Rewrite the draft answer for clarity and operator usefulness. "
                        "Do not invent values. Keep output concise markdown with short bullets."
                    ),
                    value_type="str",
                    description="Instruction template used for Ask GoldenStackers AI refinement pass.",
                    is_active=True,
                    actor=user.username,
                )
                st.success("Chat AI refinement settings saved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to save chat refinement settings: {exc}")

        if reset_chat_refine:
            try:
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="chat_ai_refine_enabled",
                    value="false",
                    value_type="bool",
                    description="Enable/disable orchestration-backed AI refinement pass for Ask GoldenStackers responses.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="chat_ai_refine_system_message",
                    value=(
                        "You are GoldenStackers' read-only operations copilot. "
                        "Preserve factual values from the provided draft answer and citations."
                    ),
                    value_type="str",
                    description="System message used for Ask GoldenStackers AI refinement pass.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="chat_ai_refine_instruction",
                    value=(
                        "Rewrite the draft answer for clarity and operator usefulness. "
                        "Do not invent values. Keep output concise markdown with short bullets."
                    ),
                    value_type="str",
                    description="Instruction template used for Ask GoldenStackers AI refinement pass.",
                    is_active=True,
                    actor=user.username,
                )
                st.success("Chat AI refinement settings reset to defaults.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to reset chat refinement settings: {exc}")

        st.divider()
        st.markdown("### Web-Fetched Grading/Comp Standards")
        st.caption(
            "Fetch public grading-reference signals (PCGS/NGC/ANACS/ICG pages) and store generated context "
            "into runtime settings. Values remain fully editable after save."
        )
        standards_snapshot = fetch_standards_snapshot()
        snapshot_sources = standards_snapshot.get("sources") or {}
        source_rows = []
        for key, payload in snapshot_sources.items():
            source_rows.append(
                {
                    "source": key,
                    "reachable": bool(payload.get("ok")),
                    "url": str(payload.get("url") or ""),
                }
            )
        if source_rows:
            st.dataframe(pd.DataFrame(source_rows), use_container_width=True)
        st.caption(f"Last standards snapshot UTC: `{str(standards_snapshot.get('checked_at_utc') or '')}`")
        with st.expander("Preview Generated Context From Current Snapshot", expanded=False):
            try:
                st.markdown("**Research Baseline (Curated, editable)**")
                st.text_area(
                    "Curated comp baseline",
                    value=CURATED_COMP_BASELINE,
                    height=180,
                    disabled=True,
                    key="admin_comp_rules_curated_baseline_preview",
                )
                st.text_area(
                    "Curated grading baseline",
                    value=CURATED_GRADING_BASELINE,
                    height=220,
                    disabled=True,
                    key="admin_grading_rules_curated_baseline_preview",
                )
                preview_comp = build_comp_rules_context_from_web()
                preview_grade = build_coin_grading_rules_context_from_web()
                st.markdown("**Comp Rules Context Preview**")
                st.text_area(
                    "comp_reference_rules_context (preview)",
                    value=preview_comp,
                    height=220,
                    disabled=True,
                    key="admin_comp_rules_context_preview",
                )
                st.markdown("**Coin Grading Rules Context Preview**")
                st.text_area(
                    "coin_grading_rules_context (preview)",
                    value=preview_grade,
                    height=260,
                    disabled=True,
                    key="admin_coin_grading_rules_context_preview",
                )
            except Exception as exc:
                st.error(f"Unable to build standards preview: {exc}")
        sfc1, sfc2 = st.columns([1, 2])
        with sfc1:
            refresh_snapshot = st.button(
                "Refresh Web Snapshot",
                key="admin_refresh_grading_web_snapshot",
            )
        with sfc2:
            apply_web_defaults = st.button(
                "Apply Web-Fetched Standards To Runtime",
                key="admin_apply_web_fetched_standards",
                use_container_width=True,
            )
        if refresh_snapshot:
            try:
                clear_standards_snapshot_cache()
                _ = fetch_standards_snapshot()
                st.success("Standards web snapshot refreshed.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to refresh standards snapshot: {exc}")
        if apply_web_defaults:
            try:
                clear_standards_snapshot_cache()
                comp_context = build_comp_rules_context_from_web()
                grading_context = build_coin_grading_rules_context_from_web()
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="comp_reference_rules_context",
                    value=str(comp_context or "").strip(),
                    value_type="str",
                    description="Supplemental grading/comps rule context appended to comp prompts.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="coin_grading_rules_context",
                    value=str(grading_context or "").strip(),
                    value_type="str",
                    description="Supplemental grading standards context appended to grader prompts.",
                    is_active=True,
                    actor=user.username,
                )
                st.success("Saved web-fetched standards context into runtime settings.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to apply web-fetched standards: {exc}")

        st.divider()
        st.markdown("### Prompt Template Versioning & Rollback")
        st.caption(
            "Review prompt/system-template change history from audit logs and rollback a template key to a prior value."
        )
        template_key_options = [
            "comp_llm_system_message",
            "comp_llm_instruction_template",
            "comp_reference_rules_context",
            "coin_grader_system_message",
            "coin_grader_instruction_template",
            "coin_grading_rules_context",
            "coin_identifier_system_message",
            "coin_identifier_instruction_template",
            "chat_ai_refine_system_message",
            "chat_ai_refine_instruction",
        ]
        selected_template_key = st.selectbox(
            "Template Key",
            options=template_key_options,
            key="admin_prompt_versioning_key",
        )
        template_row = repo.get_runtime_setting(
            environment=settings.app_env,
            key=selected_template_key,
            active_only=False,
        )
        if template_row is None:
            st.info("No runtime setting row exists yet for this template key in current environment.")
            current_value = ""
            current_description = "Prompt/system template runtime value."
            current_type = "str"
            history_rows: list[dict] = []
        else:
            current_value = str(template_row.value or "")
            current_description = str(template_row.description or "Prompt/system template runtime value.")
            current_type = str(template_row.value_type or "str")
            history_rows = _runtime_setting_audit_history(repo, int(template_row.id))

        st.caption(f"Current value length: `{len(current_value)}` chars")
        if history_rows:
            history_df = pd.DataFrame(
                [
                    {
                        "audit_id": row["audit_id"],
                        "created_at": row["created_at"],
                        "actor": row["actor"],
                        "action": row["action"],
                        "value_before_preview": (row["value_before"] or "")[:120],
                        "value_after_preview": (row["value_after"] or "")[:120],
                    }
                    for row in history_rows
                ]
            )
            st.dataframe(history_df, use_container_width=True)

            history_map = {
                (
                    f"#{row['audit_id']} | {row['created_at']} | {row['actor']} | {row['action']} | "
                    f"before_len={len(row.get('value_before') or '')} | after_len={len(row.get('value_after') or '')}"
                ): row
                for row in history_rows
            }
            selected_version_key = st.selectbox(
                "Select Version Event",
                options=list(history_map.keys()),
                key="admin_prompt_version_event_select",
            )
            selected_version = history_map[selected_version_key]
            before_value = str(selected_version.get("value_before") or "")
            after_value = str(selected_version.get("value_after") or "")

            v1, v2 = st.columns(2)
            with v1:
                st.text_area(
                    "Selected Before Value",
                    value=before_value,
                    height=180,
                    disabled=True,
                    key="admin_prompt_selected_before_preview",
                )
            with v2:
                st.text_area(
                    "Selected After Value",
                    value=after_value,
                    height=180,
                    disabled=True,
                    key="admin_prompt_selected_after_preview",
                )

            rb1, rb2 = st.columns(2)
            with rb1:
                rollback_before = st.button("Rollback To Selected Before Value", key="admin_prompt_rollback_before")
            with rb2:
                rollback_after = st.button("Rollback To Selected After Value", key="admin_prompt_rollback_after")

            if rollback_before or rollback_after:
                target_value = before_value if rollback_before else after_value
                if not target_value:
                    st.error("Selected rollback target value is empty for this event.")
                else:
                    try:
                        repo.upsert_runtime_setting(
                            environment=settings.app_env,
                            key=selected_template_key,
                            value=target_value,
                            value_type=current_type,
                            description=current_description,
                            is_active=True,
                            actor=user.username,
                        )
                        st.success(f"Rolled back `{selected_template_key}` successfully.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Rollback failed: {exc}")
        else:
            st.info("No audit history available yet for this template key.")

        st.divider()
        st.markdown("### AI Usage Telemetry")
        telemetry_window = st.selectbox(
            "Telemetry Window",
            options=["last_24h", "last_7d", "last_30d"],
            index=1,
            key="admin_ai_telemetry_window",
        )
        days_map = {"last_24h": 1, "last_7d": 7, "last_30d": 30}
        since_utc = utcnow_naive() - timedelta(days=int(days_map.get(telemetry_window, 7)))

        chat_logs = repo.db.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "ai_chat",
                AuditLog.created_at >= since_utc,
            )
            .order_by(AuditLog.created_at.desc())
            .limit(5000)
        ).all()
        chat_rows: list[dict] = []
        for row in chat_logs:
            try:
                payload = json.loads(row.changes_json or "{}")
            except Exception:
                payload = {}
            after = payload.get("after") if isinstance(payload, dict) else {}
            if not isinstance(after, dict):
                after = {}
            meta = after.get("metadata") if isinstance(after.get("metadata"), dict) else {}
            chat_rows.append(
                {
                    "created_at": row.created_at,
                    "actor": row.actor,
                    "intent": str(after.get("intent") or ""),
                    "elapsed_ms": int(after.get("elapsed_ms") or 0),
                    "denied": bool(after.get("denied")),
                    "input_mode": str(meta.get("input_mode") or ""),
                    "voice_provider": str(meta.get("voice_provider") or ""),
                    "voice_stt_model": str(meta.get("voice_stt_model") or ""),
                    "voice_tts_model": str(meta.get("voice_tts_model") or ""),
                    "ai_refined": bool(meta.get("ai_refined")),
                    "ai_refine_provider": str(meta.get("ai_refine_provider") or ""),
                    "ai_refine_text_model": str(meta.get("ai_refine_text_model") or ""),
                    "ai_refine_endpoint_type": str(meta.get("ai_refine_endpoint_type") or ""),
                }
            )

        chat_df = pd.DataFrame(chat_rows)
        chat_query_df = chat_df[chat_df["intent"] != "tts_playback_generated"] if not chat_df.empty else chat_df
        safe_failures = int((chat_query_df["intent"] == "safe_failure").sum()) if not chat_query_df.empty else 0
        denied_count = int(chat_query_df["denied"].sum()) if not chat_query_df.empty else 0
        avg_latency = float(chat_query_df["elapsed_ms"].mean()) if not chat_query_df.empty else 0.0
        p95_latency = (
            float(chat_query_df["elapsed_ms"].quantile(0.95))
            if not chat_query_df.empty and len(chat_query_df) >= 2
            else avg_latency
        )
        voice_prompt_count = int((chat_query_df["input_mode"] == "voice_stt").sum()) if not chat_query_df.empty else 0
        tts_event_count = int((chat_df["intent"] == "tts_playback_generated").sum()) if not chat_df.empty else 0
        refined_count = int(chat_query_df["ai_refined"].sum()) if not chat_query_df.empty else 0

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("AI Chat Queries", f"{len(chat_query_df)}")
        m2.metric("Denied Queries", f"{denied_count}")
        m3.metric("Safe Failures", f"{safe_failures}")
        m4.metric("Voice STT Prompts", f"{voice_prompt_count}")
        n1, n2, n3, n4 = st.columns(4)
        n1.metric("Avg Latency (ms)", f"{avg_latency:,.0f}")
        n2.metric("P95 Latency (ms)", f"{p95_latency:,.0f}")
        n3.metric("TTS Events", f"{tts_event_count}")
        n4.metric("AI Refined", f"{refined_count}")

        if not chat_query_df.empty:
            intent_counts = (
                chat_query_df.groupby("intent", dropna=False)
                .size()
                .reset_index(name="count")
                .sort_values("count", ascending=False)
            )
            user_counts = (
                chat_query_df.groupby("actor", dropna=False)
                .size()
                .reset_index(name="count")
                .sort_values("count", ascending=False)
            )
            voice_provider_counts = (
                chat_df[chat_df["voice_provider"] != ""]
                .groupby("voice_provider", dropna=False)
                .size()
                .reset_index(name="count")
                .sort_values("count", ascending=False)
                if not chat_df.empty
                else pd.DataFrame(columns=["voice_provider", "count"])
            )
            refine_model_counts = (
                chat_query_df[chat_query_df["ai_refine_provider"] != ""]
                .groupby(["ai_refine_provider", "ai_refine_text_model", "ai_refine_endpoint_type"], dropna=False)
                .size()
                .reset_index(name="count")
                .sort_values("count", ascending=False)
                if not chat_query_df.empty
                else pd.DataFrame(columns=["ai_refine_provider", "ai_refine_text_model", "ai_refine_endpoint_type", "count"])
            )
            c1, c2, c3, c4 = st.columns(4)
            with c1:
                st.markdown("#### Top Intents")
                st.dataframe(intent_counts.head(20), use_container_width=True)
            with c2:
                st.markdown("#### Top Users")
                st.dataframe(user_counts.head(20), use_container_width=True)
            with c3:
                st.markdown("#### Voice Provider Usage")
                st.dataframe(voice_provider_counts.head(20), use_container_width=True)
            with c4:
                st.markdown("#### AI Refinement Usage")
                st.dataframe(refine_model_counts.head(20), use_container_width=True)
        else:
            st.info("No AI chat telemetry rows in selected window.")

        coin_runs = repo.db.scalars(
            select(CoinAIRun)
            .where(
                CoinAIRun.environment == settings.app_env,
                CoinAIRun.created_at >= since_utc,
            )
            .order_by(CoinAIRun.created_at.desc())
            .limit(5000)
        ).all()
        if coin_runs:
            coin_df = pd.DataFrame(
                [
                    {
                        "created_at": row.created_at,
                        "tool_name": row.tool_name,
                        "username": row.username,
                        "product_id": row.product_id,
                        "listing_id": row.listing_id,
                    }
                    for row in coin_runs
                ]
            )
            st.markdown("#### Coin AI Tool Usage")
            st.dataframe(
                coin_df.groupby("tool_name", dropna=False).size().reset_index(name="count").sort_values(
                    "count", ascending=False
                ),
                use_container_width=True,
            )
        else:
            st.info("No coin AI runs in selected window.")

        profile_rows = repo.db.scalars(
            select(AIProviderConfig).where(AIProviderConfig.environment == settings.app_env)
        ).all()
        if profile_rows:
            profile_df = pd.DataFrame(
                [
                    {
                        "provider": row.provider,
                        "model": row.model,
                        "multimodal_model": row.multimodal_model,
                        "is_default": bool(row.is_default),
                        "is_active": bool(row.is_active),
                    }
                    for row in profile_rows
                ]
            )
            st.markdown("#### Configured Provider/Model Profiles")
            st.dataframe(profile_df, use_container_width=True)

    with tab_env_config:
        env_file_mode = uses_env_file(settings.app_env)
        st.markdown("### Environment Variables")
        if env_file_mode:
            st.caption(
                "Local mode: view and update `.env` values from Admin. "
                "Edits apply to the file immediately, but process-level env values require container restart to take effect."
            )
        else:
            st.caption(
                "Cluster mode: showing process environment values from the running container. "
                "Edits must be done via Kubernetes Secrets/ConfigMaps and applied by rollout/restart."
            )
        env_path = ".env"
        recommended_env_defaults = read_env_file(".env.example")
        tracked_env_keys = set(recommended_env_defaults.keys())
        env_values = (
            read_env_file(env_path)
            if env_file_mode
            else read_process_env_values(tracked_keys=tracked_env_keys, include_untracked_editable=True)
        )
        env_coverage_rows = _build_env_coverage_rows(env_values, recommended_env_defaults)
        if env_coverage_rows:
            env_cov_df = pd.DataFrame(env_coverage_rows)
            st.markdown("### Config Coverage (Env)")
            req_env = required_env_keys()
            env_required_issue_df = env_cov_df[
                env_cov_df["key"].isin(req_env) & env_cov_df["status"].isin(["missing", "empty"])
            ]
            untracked_env_df = env_cov_df[
                (env_cov_df["present_in_env"] == True) & (env_cov_df["tracked"] == False)
            ]
            env_ok_count = int(env_cov_df["status"].isin(["default", "set"]).sum())
            env_total_count = max(1, int(len(env_cov_df)))
            env_health_ratio = env_ok_count / env_total_count
            env_health_label, env_health_color = _health_label_and_emoji(env_health_ratio)
            st.markdown(
                f"**Env Config Health:** :{env_health_color}[{env_health_label.upper()}] "
                f"(`{env_ok_count}/{env_total_count}` = `{env_health_ratio * 100:.1f}%`)"
            )
            if not env_required_issue_df.empty:
                st.error(
                    "Config Health Warning: required env keys are missing/empty. "
                    "Fix these before relying on the environment."
                )
                if st.button(
                    "Auto-Fix Required Env Keys From .env.example",
                    key="admin_env_autofix_required_btn",
                    disabled=not env_file_mode,
                ):
                    try:
                        fixed = _apply_required_env_defaults(
                            env_path=env_path,
                            required_keys=req_env,
                            env_values=env_values,
                            recommended_defaults=recommended_env_defaults,
                        )
                        if fixed:
                            st.success(f"Auto-fixed {fixed} required env key(s).")
                        else:
                            st.info("No required env keys were auto-fixed.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Unable to auto-fix required env keys: {exc}")
                st.dataframe(
                    env_required_issue_df[["key", "status", "current_value", "recommended_default"]],
                    use_container_width=True,
                )
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Tracked Keys", f"{len(env_cov_df)}")
            c2.metric("Missing", f"{int((env_cov_df['status'] == 'missing').sum())}")
            c3.metric("Empty", f"{int((env_cov_df['status'] == 'empty').sum())}")
            c4.metric("Default/Set", f"{int((env_cov_df['status'].isin(['default', 'set'])).sum())}")
            c5, _ = st.columns(2)
            c5.metric("Untracked Env Keys", f"{int(len(untracked_env_df))}")
            missing_or_empty_env_count = int(env_cov_df["status"].isin(["missing", "empty"]).sum())
            if st.button(
                "Apply Missing + Empty Env Defaults Now",
                key="admin_env_apply_all_defaults_btn",
                disabled=(missing_or_empty_env_count == 0 or not env_file_mode),
            ):
                try:
                    fixed = _apply_all_env_defaults(
                        env_path=env_path,
                        env_values=env_values,
                        recommended_defaults=recommended_env_defaults,
                    )
                    if fixed:
                        st.success(f"Applied {fixed} env default value(s).")
                    else:
                        st.info("No env defaults were applied.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to apply env defaults: {exc}")
            st.download_button(
                "Download Env Coverage CSV",
                data=env_cov_df.to_csv(index=False).encode("utf-8"),
                file_name=f"env_coverage_{settings.app_env}.csv",
                mime="text/csv",
                key="admin_env_coverage_download_csv",
            )
            st.dataframe(
                env_cov_df[
                    [
                        "key",
                        "status",
                        "tracked",
                        "present_in_env",
                        "editable",
                        "is_sensitive",
                        "current_value",
                        "recommended_default",
                    ]
                ],
                use_container_width=True,
            )
            st.caption("Feature-flag-like env keys (bool/toggle domains):")
            flag_df = env_cov_df[
                env_cov_df["key"].str.contains("ENABLED|FEATURE|TOGGLE|_REQUIRED|ALLOW|OVERRIDE", regex=True)
            ]
            if not flag_df.empty:
                st.dataframe(
                    flag_df[["key", "status", "current_value", "recommended_default"]],
                    use_container_width=True,
                )
            else:
                st.info("No feature-flag-like env keys detected in tracked set.")
            if not untracked_env_df.empty:
                source_label = "`.env`" if env_file_mode else "running process environment"
                st.caption(
                    f"Untracked env keys (present in {source_label}, not defined in `.env.example`):"
                )
                st.dataframe(
                    untracked_env_df[["key", "current_value", "editable", "is_sensitive"]],
                    use_container_width=True,
                )
        if not env_values:
            if env_file_mode:
                st.warning("`.env` file was not found or has no key/value pairs.")
            else:
                st.warning("No relevant process environment keys detected for tracked/editable domains.")
        else:
            env_rows = []
            for key in sorted(env_values.keys()):
                raw_value = env_values.get(key, "")
                env_rows.append(
                    {
                        "key": key,
                        "value": mask_env_value(key, raw_value),
                        "editable": bool(is_editable_env_key(key)),
                    }
                )
            st.dataframe(pd.DataFrame(env_rows), use_container_width=True)

        editable_keys = [k for k in sorted(env_values.keys()) if is_editable_env_key(k)]
        st.markdown("### Edit Environment Value")
        if not env_file_mode:
            st.info(
                "Editing env vars in Admin is disabled outside local mode. "
                "Use Kubernetes Secret/ConfigMap updates for Development/Production."
            )
        if not editable_keys:
            st.info("No editable keys detected.")
        elif env_file_mode:
            selected_key = st.selectbox("Key", options=editable_keys, key="admin_env_edit_key")
            existing_value = env_values.get(selected_key, "")
            with st.form("admin_env_edit_form"):
                new_value = st.text_input(
                    "Value",
                    value=existing_value,
                    type="password" if "SECRET" in selected_key or "TOKEN" in selected_key or "KEY" in selected_key else "default",
                )
                save_env_value = st.form_submit_button("Save to .env")
            if save_env_value:
                try:
                    upsert_env_key(env_path, selected_key, new_value)
                    st.success(f"Updated `{selected_key}` in `.env`.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to update `.env`: {exc}")

        if env_file_mode:
            st.markdown("### Add New `.env` Key")
            with st.form("admin_env_add_form"):
                add_key = st.text_input("New Key")
                add_value = st.text_input("New Value")
                add_submit = st.form_submit_button("Add Key")
            if add_submit:
                normalized = (add_key or "").strip().upper()
                if not normalized:
                    st.error("Key is required.")
                elif not is_editable_env_key(normalized):
                    st.error("This key prefix is not editable from Admin.")
                else:
                    try:
                        upsert_env_key(env_path, normalized, add_value)
                        st.success(f"Added `{normalized}` to `.env`.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Unable to add `.env` key: {exc}")

            st.markdown("### Sync Missing Recommended Keys")
            st.caption("Adds missing keys from `.env.example` without overwriting current values.")
            if st.button("Add Missing Recommended Keys", key="admin_env_sync_defaults_btn"):
                try:
                    added = ensure_env_defaults(env_path, recommended_env_defaults)
                    if added:
                        st.success(f"Added {len(added)} missing keys to `.env`.")
                    else:
                        st.info("`.env` already contains all keys from `.env.example`.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to sync defaults: {exc}")

    with tab_runtime_settings:
        st.markdown("### Runtime Settings (DB)")
        st.caption(
            "These settings are environment-scoped and can override selected env-based defaults at runtime."
        )
        try:
            runtime_rows = repo.list_runtime_settings(environment=settings.app_env, active_only=False)
        except Exception as exc:
            st.error(
                "Runtime settings table is not available yet. "
                "Run database migrations first (`docker compose run --rm migrate`)."
            )
            st.caption(f"Details: {exc}")
            runtime_rows = []

        runtime_seed_defaults = _runtime_setting_seed_defaults()
        runtime_cov_rows = _build_runtime_coverage_rows(runtime_rows, runtime_seed_defaults)
        if runtime_cov_rows:
            runtime_cov_df = pd.DataFrame(runtime_cov_rows)
            st.markdown("### Config Coverage (Runtime Settings)")
            req_runtime = required_runtime_keys()
            runtime_required_issue_df = runtime_cov_df[
                runtime_cov_df["key"].isin(req_runtime) & runtime_cov_df["status"].isin(["missing", "inactive"])
            ]
            runtime_ok_count = int(runtime_cov_df["status"].isin(["default", "overridden"]).sum())
            runtime_total_count = max(1, int(len(runtime_cov_df)))
            runtime_health_ratio = runtime_ok_count / runtime_total_count
            runtime_health_label, runtime_health_color = _health_label_and_emoji(runtime_health_ratio)
            st.markdown(
                f"**Runtime Config Health:** :{runtime_health_color}[{runtime_health_label.upper()}] "
                f"(`{runtime_ok_count}/{runtime_total_count}` = `{runtime_health_ratio * 100:.1f}%`)"
            )
            if not runtime_required_issue_df.empty:
                st.error(
                    "Config Health Warning: required runtime keys are missing/inactive. "
                    "Use `Apply All Missing Runtime Defaults Now` and activate required keys."
                )
                if st.button(
                    "Auto-Fix Required Runtime Keys",
                    key="admin_runtime_autofix_required_btn",
                ):
                    try:
                        fixed = _apply_required_runtime_defaults(
                            repo=repo,
                            actor=user.username,
                            required_keys=req_runtime,
                            runtime_rows=runtime_rows,
                            seed_defaults=runtime_seed_defaults,
                        )
                        if fixed:
                            st.success(f"Auto-fixed {fixed} required runtime key(s).")
                        else:
                            st.info("No required runtime keys were auto-fixed.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Unable to auto-fix required runtime keys: {exc}")
                st.dataframe(
                    runtime_required_issue_df[["key", "status", "current_value", "expected_default", "is_active"]],
                    use_container_width=True,
                )
            r1, r2, r3, r4 = st.columns(4)
            r1.metric("Tracked Runtime Keys", f"{len(runtime_cov_df)}")
            r2.metric("Missing", f"{int((runtime_cov_df['status'] == 'missing').sum())}")
            r3.metric("Inactive", f"{int((runtime_cov_df['status'] == 'inactive').sum())}")
            r4.metric("Overridden", f"{int((runtime_cov_df['status'] == 'overridden').sum())}")
            r5, _ = st.columns(2)
            r5.metric("Custom Untracked", f"{int((runtime_cov_df['status'] == 'custom_untracked').sum())}")
            missing_runtime_count = int((runtime_cov_df["status"] == "missing").sum())
            inactive_runtime_count = int((runtime_cov_df["status"] == "inactive").sum())
            if st.button(
                "Apply Missing + Inactive Runtime Defaults Now",
                key="admin_runtime_apply_all_defaults_btn",
                disabled=((missing_runtime_count + inactive_runtime_count) == 0),
            ):
                try:
                    fixed = _apply_all_runtime_defaults(
                        repo=repo,
                        actor=user.username,
                        runtime_rows=runtime_rows,
                        seed_defaults=runtime_seed_defaults,
                    )
                    if fixed:
                        st.success(f"Applied {fixed} runtime default update(s).")
                    else:
                        st.info("No runtime defaults were applied.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to apply runtime defaults: {exc}")
            st.download_button(
                "Download Runtime Coverage CSV",
                data=runtime_cov_df.to_csv(index=False).encode("utf-8"),
                file_name=f"runtime_coverage_{settings.app_env}.csv",
                mime="text/csv",
                key="admin_runtime_coverage_download_csv",
            )
            if st.button(
                "Apply All Missing Runtime Defaults Now",
                key="admin_runtime_apply_missing_defaults_btn",
                disabled=(missing_runtime_count == 0),
            ):
                seeded_now = _seed_missing_runtime_defaults(
                    repo,
                    actor=user.username,
                    seed_defaults=runtime_seed_defaults,
                )
                if seeded_now:
                    st.success(f"Applied {seeded_now} missing runtime default(s).")
                else:
                    st.info("No missing runtime defaults were applied.")
                st.rerun()
            st.dataframe(
                runtime_cov_df[
                    [
                        "key",
                        "status",
                        "expected_type",
                        "current_type",
                        "is_active",
                        "current_value",
                        "expected_default",
                        "updated_by",
                        "updated_at",
                    ]
                ],
                use_container_width=True,
            )
            st.caption("Feature-flag-like runtime keys (bool/toggle domains):")
            runtime_flag_df = runtime_cov_df[
                runtime_cov_df["key"].str.contains("enabled|feature|toggle|required|allow|override", case=False, regex=True)
            ]
            if not runtime_flag_df.empty:
                st.dataframe(
                    runtime_flag_df[
                        ["key", "status", "is_active", "current_value", "expected_default", "description"]
                    ],
                    use_container_width=True,
                )
            else:
                st.info("No feature-flag-like runtime keys detected in tracked set.")

        if runtime_rows:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "id": row.id,
                            "environment": row.environment,
                            "key": row.key,
                            "value": row.value,
                            "value_type": row.value_type,
                            "is_active": bool(row.is_active),
                            "updated_by": row.updated_by,
                            "updated_at": row.updated_at.isoformat() if row.updated_at else "",
                        }
                        for row in runtime_rows
                    ]
                ),
                use_container_width=True,
            )
        else:
            st.info("No runtime settings found for this environment.")
        st.info("Dealer-domain parser settings now live in the `Comp Config` tab.")

        st.markdown("### Documents Handoff History (Team/Admin)")
        is_admin_user = str(user.role or "").strip().lower() == "admin"
        handoff_prefix = "documents_recent_handoffs_json__"
        handoff_setting_rows = [
            row for row in runtime_rows if str(row.key or "").strip().lower().startswith(handoff_prefix)
        ]
        if not handoff_setting_rows:
            st.caption("No persisted Documents handoff history keys found yet.")
        else:
            parsed_rows: list[dict] = []
            for setting_row in handoff_setting_rows:
                key_raw = str(setting_row.key or "").strip()
                username = key_raw[len(handoff_prefix) :].strip() if key_raw.lower().startswith(handoff_prefix) else ""
                try:
                    payload = json.loads(str(setting_row.value or "[]"))
                except Exception:
                    payload = []
                if not isinstance(payload, list):
                    continue
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    parsed_rows.append(
                        {
                            "username": username or "(unknown)",
                            "at": str(item.get("at") or ""),
                            "source_type": str(item.get("source_type") or ""),
                            "source_id": int(item.get("source_id") or 0),
                            "doc_type": str(item.get("doc_type") or "invoice"),
                            "handoff_from": str(item.get("handoff_from") or ""),
                            "setting_id": int(setting_row.id),
                            "setting_key": key_raw,
                        }
                    )
            if not is_admin_user:
                parsed_rows = [
                    row for row in parsed_rows if str(row.get("username") or "").strip().lower() == str(user.username).strip().lower()
                ]
            if not parsed_rows:
                st.caption("No handoff history rows available for your scope.")
            else:
                user_options = sorted({str(row.get("username") or "") for row in parsed_rows if str(row.get("username") or "").strip()})
                type_options = sorted({str(row.get("source_type") or "") for row in parsed_rows if str(row.get("source_type") or "").strip()})
                doc_options = sorted({str(row.get("doc_type") or "") for row in parsed_rows if str(row.get("doc_type") or "").strip()})
                h1, h2, h3 = st.columns(3)
                with h1:
                    if is_admin_user:
                        selected_users = st.multiselect(
                            "Filter User",
                            options=user_options,
                            default=[],
                            key="admin_documents_handoff_users_filter",
                        )
                    else:
                        selected_users = [str(user.username).strip().lower()]
                        st.text_input(
                            "Filter User",
                            value=str(user.username).strip().lower(),
                            disabled=True,
                            key="admin_documents_handoff_users_readonly",
                        )
                with h2:
                    selected_types = st.multiselect(
                        "Filter Source Type",
                        options=type_options,
                        default=[],
                        key="admin_documents_handoff_types_filter",
                    )
                with h3:
                    selected_doc_types = st.multiselect(
                        "Filter Doc Type",
                        options=doc_options,
                        default=[],
                        key="admin_documents_handoff_doc_types_filter",
                    )
                filtered_handoffs = []
                for row in parsed_rows:
                    if selected_users and str(row.get("username") or "") not in selected_users:
                        continue
                    if selected_types and str(row.get("source_type") or "") not in selected_types:
                        continue
                    if selected_doc_types and str(row.get("doc_type") or "") not in selected_doc_types:
                        continue
                    filtered_handoffs.append(row)
                filtered_handoffs = sorted(
                    filtered_handoffs,
                    key=lambda row: str(row.get("at") or ""),
                    reverse=True,
                )
                st.caption(f"Rows: {len(filtered_handoffs)}")
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "username": row.get("username"),
                                "at": row.get("at"),
                                "source_type": row.get("source_type"),
                                "source_id": row.get("source_id"),
                                "doc_type": row.get("doc_type"),
                                "handoff_from": row.get("handoff_from"),
                            }
                            for row in filtered_handoffs
                        ]
                    ),
                    use_container_width=True,
                    hide_index=True,
                )
                if filtered_handoffs:
                    action_map = {
                        (
                            f"{row.get('at')} | {row.get('username')} | "
                            f"{row.get('source_type')} #{int(row.get('source_id') or 0)} | "
                            f"{row.get('doc_type')} | {row.get('handoff_from')}"
                        ): row
                        for row in filtered_handoffs[:200]
                    }
                    a1, a2, a3 = st.columns([3, 1, 1])
                    with a1:
                        selected_handoff_action_label = st.selectbox(
                            "Select Handoff Context",
                            options=list(action_map.keys()),
                            key="admin_documents_handoff_action_pick",
                        )
                    selected_handoff_action = action_map[selected_handoff_action_label]
                    reason_code_options = [
                        "user_request",
                        "privacy_cleanup",
                        "policy_enforcement",
                        "data_quality_reset",
                        "security_incident",
                        "other",
                    ]
                    r1, r2 = st.columns([1, 2])
                    with r1:
                        clear_reason_code = st.selectbox(
                            "Clear Reason Code",
                            options=reason_code_options,
                            index=0,
                            key="admin_documents_handoff_clear_reason_code",
                            help="Standardized reason classification for governance reporting.",
                        )
                    with r2:
                        clear_reason_note = st.text_input(
                            "Clear Reason Note (optional)",
                            value="",
                            key="admin_documents_handoff_clear_reason_note",
                            help="Optional supporting context. Required for `other`.",
                        ).strip()
                    with a2:
                        if st.button("Open in Documents", key="admin_documents_handoff_open_btn"):
                            handoff_to_documents_draft(
                                source_type=str(selected_handoff_action.get("source_type") or ""),
                                source_id=int(selected_handoff_action.get("source_id") or 0),
                                doc_type=str(selected_handoff_action.get("doc_type") or "invoice"),
                                handoff_from="admin_documents_handoffs",
                                repo=repo,
                                actor=user.username,
                            )
                    with a3:
                        clear_label = "Clear User History" if is_admin_user else "Clear My History"
                        if st.button(clear_label, key="admin_documents_handoff_clear_user_btn"):
                            username_to_clear = str(selected_handoff_action.get("username") or "").strip().lower()
                            if not username_to_clear:
                                st.error("Cannot determine username for selected row.")
                            elif not is_admin_user and username_to_clear != str(user.username).strip().lower():
                                st.error("You can only clear your own handoff history.")
                            elif is_admin_user and username_to_clear != str(user.username).strip().lower() and not str(clear_reason_code).strip():
                                st.error("Clear reason code is required when clearing another user's history.")
                            elif str(clear_reason_code).strip().lower() == "other" and not clear_reason_note:
                                st.error("Reason note is required when reason code is `other`.")
                            else:
                                key_to_clear = f"{handoff_prefix}{username_to_clear}"
                                try:
                                    repo.upsert_runtime_setting(
                                        environment=settings.app_env,
                                        key=key_to_clear,
                                        value="[]",
                                        value_type="str",
                                        description="Recent Documents handoff contexts (per-user) for quick reopen.",
                                        is_active=True,
                                        actor=user.username,
                                    )
                                    try:
                                        repo.record_audit_event(
                                            entity_type="documents_handoff_history",
                                            entity_id=None,
                                            action="clear_history",
                                            actor=user.username,
                                            changes={
                                                "scope": "admin" if is_admin_user else "self",
                                                "target_user": username_to_clear,
                                                "environment": settings.app_env,
                                                "reason_code": str(clear_reason_code).strip().lower(),
                                                "reason_note": clear_reason_note,
                                                "reason": (
                                                    f"{str(clear_reason_code).strip().lower()}: {clear_reason_note}"
                                                    if clear_reason_note
                                                    else str(clear_reason_code).strip().lower()
                                                ),
                                            },
                                        )
                                    except Exception:
                                        pass
                                    st.success(f"Cleared handoff history for `{username_to_clear}`.")
                                    st.rerun()
                                except Exception as exc:
                                    st.error(f"Unable to clear user history: {exc}")

        st.markdown("### Documents Handoff Clear Audit Summary")
        personal_preset_store_key = (
            f"documents_handoff_clear_audit_presets_json__{str(user.username).strip().lower()}"
        )
        shared_preset_store_key = "documents_handoff_clear_audit_presets_json__shared"

        def _load_preset_map(store_key: str) -> dict[str, dict]:
            row = next((r for r in runtime_rows if str(r.key or "").strip() == store_key), None)
            if row is None:
                return {}
            raw = str(row.value or "").strip()
            if not raw:
                return {}
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return {str(k): v for k, v in parsed.items() if isinstance(v, dict)}
            except Exception:
                return {}
            return {}

        personal_presets = _load_preset_map(personal_preset_store_key)
        shared_presets = _load_preset_map(shared_preset_store_key)
        shared_default_key_store = "documents_handoff_clear_audit_default_shared_preset"
        shared_default_name = ""
        shared_default_row = next(
            (r for r in runtime_rows if str(r.key or "").strip() == shared_default_key_store),
            None,
        )
        if shared_default_row is not None:
            shared_default_name = str(shared_default_row.value or "").strip()

        def _apply_governance_preset(payload: dict) -> None:
            st.session_state["admin_documents_handoff_clear_audit_date_preset"] = str(
                payload.get("date_preset") or "Last 30d"
            )
            from_raw = str(payload.get("from_date") or "").strip()
            to_raw = str(payload.get("to_date") or "").strip()
            try:
                if from_raw:
                    st.session_state["admin_documents_handoff_clear_audit_from_date"] = datetime.fromisoformat(from_raw).date()
            except Exception:
                pass
            try:
                if to_raw:
                    st.session_state["admin_documents_handoff_clear_audit_to_date"] = datetime.fromisoformat(to_raw).date()
            except Exception:
                pass
            st.session_state["admin_documents_handoff_clear_audit_reason_filter"] = list(
                payload.get("reason_codes") or []
            )
            st.session_state["admin_documents_handoff_clear_audit_scope_filter"] = list(
                payload.get("scopes") or []
            )
            try:
                st.session_state["admin_documents_handoff_clear_audit_limit"] = int(
                    payload.get("lookback_limit") or 1000
                )
            except Exception:
                st.session_state["admin_documents_handoff_clear_audit_limit"] = 1000

        if not bool(st.session_state.get("admin_documents_handoff_default_shared_loaded")):
            default_payload = shared_presets.get(shared_default_name) if shared_default_name else None
            if isinstance(default_payload, dict):
                _apply_governance_preset(default_payload)
            st.session_state["admin_documents_handoff_default_shared_loaded"] = True

        st.markdown("#### Saved Governance Views")
        pg0, pg1, pg2, pg3 = st.columns([1, 2, 2, 2])
        with pg0:
            preset_scope = st.selectbox(
                "Preset Scope",
                options=["My Presets", "Shared Presets"],
                index=0,
                key="admin_documents_handoff_governance_preset_scope",
            )
        active_presets = personal_presets if preset_scope == "My Presets" else shared_presets
        active_store_key = personal_preset_store_key if preset_scope == "My Presets" else shared_preset_store_key
        active_description = (
            "Per-user saved governance views for Documents handoff clear-audit review."
            if preset_scope == "My Presets"
            else "Team-shared governance views for Documents handoff clear-audit review."
        )
        with pg1:
            selected_governance_preset = st.selectbox(
                "Saved Preset",
                options=["None"] + sorted(active_presets.keys()),
                key="admin_documents_handoff_governance_preset_select",
            )
        with pg2:
            new_governance_preset_name = st.text_input(
                "Preset Name",
                value="",
                key="admin_documents_handoff_governance_preset_name",
            ).strip()
        with pg3:
            st.caption("Save current filters/date settings as a reusable preset.")
            save_governance_preset = st.button(
                "Save Preset",
                key="admin_documents_handoff_governance_preset_save_btn",
            )
        lp1, lp2 = st.columns(2)
        with lp1:
            load_governance_preset = st.button(
                "Load Preset",
                key="admin_documents_handoff_governance_preset_load_btn",
            )
        with lp2:
            delete_governance_preset = st.button(
                "Delete Preset",
                key="admin_documents_handoff_governance_preset_delete_btn",
            )
        sp1, sp2 = st.columns(2)
        with sp1:
            set_team_default = st.button(
                "Set as Team Default",
                key="admin_documents_handoff_governance_set_default_btn",
                disabled=not (is_admin_user and preset_scope == "Shared Presets" and selected_governance_preset != "None"),
            )
        with sp2:
            clear_team_default = st.button(
                "Clear Team Default",
                key="admin_documents_handoff_governance_clear_default_btn",
                disabled=not is_admin_user,
            )
        st.caption(f"Current Team Default: `{shared_default_name or '(none)'}`")

        if save_governance_preset:
            if not new_governance_preset_name:
                st.error("Preset name is required.")
            elif preset_scope == "Shared Presets" and not is_admin_user:
                st.error("Only admins can save shared presets.")
            else:
                active_presets[new_governance_preset_name] = {
                    "date_preset": str(st.session_state.get("admin_documents_handoff_clear_audit_date_preset") or "Last 30d"),
                    "from_date": str(st.session_state.get("admin_documents_handoff_clear_audit_from_date") or ""),
                    "to_date": str(st.session_state.get("admin_documents_handoff_clear_audit_to_date") or ""),
                    "reason_codes": list(st.session_state.get("admin_documents_handoff_clear_audit_reason_filter") or []),
                    "scopes": list(st.session_state.get("admin_documents_handoff_clear_audit_scope_filter") or []),
                    "lookback_limit": int(st.session_state.get("admin_documents_handoff_clear_audit_limit") or 1000),
                }
                try:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=active_store_key,
                        value=json.dumps(active_presets),
                        value_type="str",
                        description=active_description,
                        is_active=True,
                        actor=user.username,
                    )
                    try:
                        repo.record_audit_event(
                            entity_type="documents_handoff_governance_preset",
                            entity_id=None,
                            action="save",
                            actor=user.username,
                            changes={
                                "scope": "shared" if preset_scope == "Shared Presets" else "personal",
                                "preset_name": new_governance_preset_name,
                                "environment": settings.app_env,
                            },
                        )
                    except Exception:
                        pass
                    st.success(f"Saved preset `{new_governance_preset_name}` ({preset_scope}).")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to save preset: {exc}")

        if load_governance_preset:
            if selected_governance_preset == "None":
                st.error("Select a preset first.")
            else:
                payload = active_presets.get(selected_governance_preset) or {}
                _apply_governance_preset(payload)
                try:
                    repo.record_audit_event(
                        entity_type="documents_handoff_governance_preset",
                        entity_id=None,
                        action="load",
                        actor=user.username,
                        changes={
                            "scope": "shared" if preset_scope == "Shared Presets" else "personal",
                            "preset_name": selected_governance_preset,
                            "environment": settings.app_env,
                        },
                    )
                except Exception:
                    pass
                st.success(f"Loaded preset `{selected_governance_preset}` ({preset_scope}).")
                st.rerun()

        if delete_governance_preset:
            if selected_governance_preset == "None":
                st.error("Select a preset first.")
            elif preset_scope == "Shared Presets" and not is_admin_user:
                st.error("Only admins can delete shared presets.")
            else:
                active_presets.pop(selected_governance_preset, None)
                try:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=active_store_key,
                        value=json.dumps(active_presets),
                        value_type="str",
                        description=active_description,
                        is_active=True,
                        actor=user.username,
                    )
                    if preset_scope == "Shared Presets" and selected_governance_preset == shared_default_name:
                        repo.upsert_runtime_setting(
                            environment=settings.app_env,
                            key=shared_default_key_store,
                            value="",
                            value_type="str",
                            description="Default team-shared governance preset for Documents handoff clear-audit panel.",
                            is_active=True,
                            actor=user.username,
                        )
                    st.session_state["admin_documents_handoff_default_shared_loaded"] = False
                    try:
                        repo.record_audit_event(
                            entity_type="documents_handoff_governance_preset",
                            entity_id=None,
                            action="delete",
                            actor=user.username,
                            changes={
                                "scope": "shared" if preset_scope == "Shared Presets" else "personal",
                                "preset_name": selected_governance_preset,
                                "environment": settings.app_env,
                            },
                        )
                    except Exception:
                        pass
                    st.success(f"Deleted preset `{selected_governance_preset}` ({preset_scope}).")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to delete preset: {exc}")

        if set_team_default:
            if not is_admin_user:
                st.error("Only admins can set team default.")
            elif preset_scope != "Shared Presets":
                st.error("Switch to Shared Presets to set team default.")
            elif selected_governance_preset == "None":
                st.error("Select a shared preset first.")
            elif selected_governance_preset not in shared_presets:
                st.error("Selected shared preset was not found.")
            else:
                try:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=shared_default_key_store,
                        value=selected_governance_preset,
                        value_type="str",
                        description="Default team-shared governance preset for Documents handoff clear-audit panel.",
                        is_active=True,
                        actor=user.username,
                    )
                    st.session_state["admin_documents_handoff_default_shared_loaded"] = False
                    try:
                        repo.record_audit_event(
                            entity_type="documents_handoff_governance_preset",
                            entity_id=None,
                            action="set_team_default",
                            actor=user.username,
                            changes={
                                "scope": "shared",
                                "preset_name": selected_governance_preset,
                                "environment": settings.app_env,
                            },
                        )
                    except Exception:
                        pass
                    st.success(f"Set team default to `{selected_governance_preset}`.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to set team default: {exc}")

        if clear_team_default:
            if not is_admin_user:
                st.error("Only admins can clear team default.")
            else:
                try:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=shared_default_key_store,
                        value="",
                        value_type="str",
                        description="Default team-shared governance preset for Documents handoff clear-audit panel.",
                        is_active=True,
                        actor=user.username,
                    )
                    st.session_state["admin_documents_handoff_default_shared_loaded"] = False
                    try:
                        repo.record_audit_event(
                            entity_type="documents_handoff_governance_preset",
                            entity_id=None,
                            action="clear_team_default",
                            actor=user.username,
                            changes={
                                "scope": "shared",
                                "environment": settings.app_env,
                            },
                        )
                    except Exception:
                        pass
                    st.success("Cleared team default preset.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to clear team default: {exc}")

        governance_review_mode_default = get_runtime_bool(
            repo,
            "documents_handoff_governance_review_mode",
            False,
        )
        governance_review_mode = st.checkbox(
            "Governance Review Mode (Shared Date Window)",
            value=bool(governance_review_mode_default),
            key="admin_documents_handoff_governance_review_mode",
            help=(
                "When enabled, both Governance Preset Audit and Clear Audit use the same date preset/range "
                "to simplify recurring reviews."
            ),
        )
        if st.button("Save Governance Review Preference", key="admin_documents_handoff_governance_review_mode_save"):
            try:
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="documents_handoff_governance_review_mode",
                    value="true" if governance_review_mode else "false",
                    value_type="bool",
                    description="When true, Admin governance clear-audit and preset-audit share one date preset/range.",
                    is_active=True,
                    actor=user.username,
                )
                st.success("Saved governance review preference.")
            except Exception as exc:
                st.error(f"Unable to save governance review preference: {exc}")

        st.markdown("#### Saved Governance Date Windows")
        window_personal_store_key = (
            f"documents_handoff_governance_window_presets_json__{str(user.username).strip().lower()}"
        )
        window_shared_store_key = "documents_handoff_governance_window_presets_json__shared"
        window_default_shared_key = "documents_handoff_governance_window_default_shared_preset"
        window_personal_presets = _load_preset_map(window_personal_store_key)
        window_shared_presets = _load_preset_map(window_shared_store_key)
        window_default_name = ""
        window_default_row = next(
            (r for r in runtime_rows if str(r.key or "").strip() == window_default_shared_key),
            None,
        )
        if window_default_row is not None:
            window_default_name = str(window_default_row.value or "").strip()

        def _apply_governance_window(payload: dict) -> None:
            date_preset = str(payload.get("date_preset") or "Last 30d")
            st.session_state["admin_documents_handoff_governance_shared_date_preset"] = date_preset
            st.session_state["admin_documents_handoff_governance_preset_audit_date_preset"] = date_preset
            st.session_state["admin_documents_handoff_clear_audit_date_preset"] = date_preset
            from_raw = str(payload.get("from_date") or "").strip()
            to_raw = str(payload.get("to_date") or "").strip()
            from_date = st.session_state.get("admin_documents_handoff_governance_shared_from_date")
            to_date = st.session_state.get("admin_documents_handoff_governance_shared_to_date")
            try:
                if from_raw:
                    from_date = datetime.fromisoformat(from_raw).date()
            except Exception:
                pass
            try:
                if to_raw:
                    to_date = datetime.fromisoformat(to_raw).date()
            except Exception:
                pass
            st.session_state["admin_documents_handoff_governance_shared_from_date"] = from_date
            st.session_state["admin_documents_handoff_governance_shared_to_date"] = to_date
            st.session_state["admin_documents_handoff_governance_preset_audit_from_date"] = from_date
            st.session_state["admin_documents_handoff_governance_preset_audit_to_date"] = to_date
            st.session_state["admin_documents_handoff_clear_audit_from_date"] = from_date
            st.session_state["admin_documents_handoff_clear_audit_to_date"] = to_date

        wp0, wp1, wp2, wp3 = st.columns([1, 2, 2, 2])
        with wp0:
            window_scope = st.selectbox(
                "Window Scope",
                options=["My Presets", "Shared Presets"],
                index=0,
                key="admin_documents_handoff_governance_window_scope",
            )
        active_window_presets = window_personal_presets if window_scope == "My Presets" else window_shared_presets
        active_window_store_key = window_personal_store_key if window_scope == "My Presets" else window_shared_store_key
        active_window_description = (
            "Per-user governance shared-date window presets."
            if window_scope == "My Presets"
            else "Team-shared governance shared-date window presets."
        )
        with wp1:
            selected_window_preset = st.selectbox(
                "Saved Window",
                options=["None"] + sorted(active_window_presets.keys()),
                key="admin_documents_handoff_governance_window_select",
            )
        with wp2:
            new_window_preset_name = st.text_input(
                "Window Name",
                value="",
                key="admin_documents_handoff_governance_window_name",
            ).strip()
        with wp3:
            save_window_preset = st.button(
                "Save Window Preset",
                key="admin_documents_handoff_governance_window_save_btn",
            )

        wl1, wl2 = st.columns(2)
        with wl1:
            load_window_preset = st.button(
                "Load Window Preset",
                key="admin_documents_handoff_governance_window_load_btn",
            )
        with wl2:
            delete_window_preset = st.button(
                "Delete Window Preset",
                key="admin_documents_handoff_governance_window_delete_btn",
            )
        wd1, wd2 = st.columns(2)
        with wd1:
            set_window_team_default = st.button(
                "Set Window Team Default",
                key="admin_documents_handoff_governance_window_set_default_btn",
                disabled=not (is_admin_user and window_scope == "Shared Presets" and selected_window_preset != "None"),
            )
        with wd2:
            clear_window_team_default = st.button(
                "Clear Window Team Default",
                key="admin_documents_handoff_governance_window_clear_default_btn",
                disabled=not is_admin_user,
            )
        st.caption(f"Current Window Team Default: `{window_default_name or '(none)'}`")

        if save_window_preset:
            if not new_window_preset_name:
                st.error("Window preset name is required.")
            elif window_scope == "Shared Presets" and not is_admin_user:
                st.error("Only admins can save shared window presets.")
            else:
                active_window_presets[new_window_preset_name] = {
                    "date_preset": str(st.session_state.get("admin_documents_handoff_governance_shared_date_preset") or "Last 30d"),
                    "from_date": str(st.session_state.get("admin_documents_handoff_governance_shared_from_date") or ""),
                    "to_date": str(st.session_state.get("admin_documents_handoff_governance_shared_to_date") or ""),
                }
                try:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=active_window_store_key,
                        value=json.dumps(active_window_presets),
                        value_type="str",
                        description=active_window_description,
                        is_active=True,
                        actor=user.username,
                    )
                    try:
                        repo.record_audit_event(
                            entity_type="documents_handoff_governance_preset",
                            entity_id=None,
                            action="window_save",
                            actor=user.username,
                            changes={
                                "scope": "shared" if window_scope == "Shared Presets" else "personal",
                                "preset_name": new_window_preset_name,
                                "environment": settings.app_env,
                            },
                        )
                    except Exception:
                        pass
                    st.success(f"Saved window preset `{new_window_preset_name}` ({window_scope}).")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to save window preset: {exc}")

        if load_window_preset:
            if selected_window_preset == "None":
                st.error("Select a window preset first.")
            else:
                _apply_governance_window(active_window_presets.get(selected_window_preset) or {})
                try:
                    repo.record_audit_event(
                        entity_type="documents_handoff_governance_preset",
                        entity_id=None,
                        action="window_load",
                        actor=user.username,
                        changes={
                            "scope": "shared" if window_scope == "Shared Presets" else "personal",
                            "preset_name": selected_window_preset,
                            "environment": settings.app_env,
                        },
                    )
                except Exception:
                    pass
                st.success(f"Loaded window preset `{selected_window_preset}` ({window_scope}).")
                st.rerun()

        if delete_window_preset:
            if selected_window_preset == "None":
                st.error("Select a window preset first.")
            elif window_scope == "Shared Presets" and not is_admin_user:
                st.error("Only admins can delete shared window presets.")
            else:
                active_window_presets.pop(selected_window_preset, None)
                try:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=active_window_store_key,
                        value=json.dumps(active_window_presets),
                        value_type="str",
                        description=active_window_description,
                        is_active=True,
                        actor=user.username,
                    )
                    if window_scope == "Shared Presets" and selected_window_preset == window_default_name:
                        repo.upsert_runtime_setting(
                            environment=settings.app_env,
                            key=window_default_shared_key,
                            value="",
                            value_type="str",
                            description="Default team-shared governance date-window preset.",
                            is_active=True,
                            actor=user.username,
                        )
                    try:
                        repo.record_audit_event(
                            entity_type="documents_handoff_governance_preset",
                            entity_id=None,
                            action="window_delete",
                            actor=user.username,
                            changes={
                                "scope": "shared" if window_scope == "Shared Presets" else "personal",
                                "preset_name": selected_window_preset,
                                "environment": settings.app_env,
                            },
                        )
                    except Exception:
                        pass
                    st.success(f"Deleted window preset `{selected_window_preset}` ({window_scope}).")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to delete window preset: {exc}")

        if set_window_team_default:
            if not is_admin_user:
                st.error("Only admins can set window team default.")
            elif window_scope != "Shared Presets":
                st.error("Switch to Shared Presets to set team default.")
            elif selected_window_preset == "None":
                st.error("Select a shared window preset first.")
            elif selected_window_preset not in window_shared_presets:
                st.error("Selected shared window preset was not found.")
            else:
                try:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=window_default_shared_key,
                        value=selected_window_preset,
                        value_type="str",
                        description="Default team-shared governance date-window preset.",
                        is_active=True,
                        actor=user.username,
                    )
                    try:
                        repo.record_audit_event(
                            entity_type="documents_handoff_governance_preset",
                            entity_id=None,
                            action="window_set_team_default",
                            actor=user.username,
                            changes={
                                "scope": "shared",
                                "preset_name": selected_window_preset,
                                "environment": settings.app_env,
                            },
                        )
                    except Exception:
                        pass
                    st.success(f"Set window team default to `{selected_window_preset}`.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to set window team default: {exc}")

        if clear_window_team_default:
            if not is_admin_user:
                st.error("Only admins can clear window team default.")
            else:
                try:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=window_default_shared_key,
                        value="",
                        value_type="str",
                        description="Default team-shared governance date-window preset.",
                        is_active=True,
                        actor=user.username,
                    )
                    try:
                        repo.record_audit_event(
                            entity_type="documents_handoff_governance_preset",
                            entity_id=None,
                            action="window_clear_team_default",
                            actor=user.username,
                            changes={
                                "scope": "shared",
                                "environment": settings.app_env,
                            },
                        )
                    except Exception:
                        pass
                    st.success("Cleared window team default preset.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to clear window team default: {exc}")

        if (
            governance_review_mode
            and not bool(st.session_state.get("admin_documents_handoff_window_default_shared_loaded"))
            and window_default_name
            and isinstance(window_shared_presets.get(window_default_name), dict)
        ):
            _apply_governance_window(window_shared_presets.get(window_default_name) or {})
            st.session_state["admin_documents_handoff_window_default_shared_loaded"] = True
        if not governance_review_mode:
            st.session_state["admin_documents_handoff_window_default_shared_loaded"] = False
        shared_from_date = None
        shared_to_date = None
        shared_invalid_range = False
        if governance_review_mode:
            st.caption("Shared date controls for both governance audit sections.")
            shared_preset_options = ["Last 7d", "Last 30d", "This Month", "Custom"]
            shared_preset = st.selectbox(
                "Governance Date Preset",
                options=shared_preset_options,
                index=1,
                key="admin_documents_handoff_governance_shared_date_preset",
            )
            shared_today = utcnow_naive().date()
            if shared_preset == "Last 7d":
                st.session_state["admin_documents_handoff_governance_shared_from_date"] = shared_today - timedelta(
                    days=6
                )
                st.session_state["admin_documents_handoff_governance_shared_to_date"] = shared_today
            elif shared_preset == "Last 30d":
                st.session_state["admin_documents_handoff_governance_shared_from_date"] = shared_today - timedelta(
                    days=29
                )
                st.session_state["admin_documents_handoff_governance_shared_to_date"] = shared_today
            elif shared_preset == "This Month":
                st.session_state["admin_documents_handoff_governance_shared_from_date"] = shared_today.replace(day=1)
                st.session_state["admin_documents_handoff_governance_shared_to_date"] = shared_today
            sd1, sd2 = st.columns(2)
            with sd1:
                shared_from_date = st.date_input(
                    "Governance From Date",
                    key="admin_documents_handoff_governance_shared_from_date",
                )
            with sd2:
                shared_to_date = st.date_input(
                    "Governance To Date",
                    key="admin_documents_handoff_governance_shared_to_date",
                )
            shared_invalid_range = shared_from_date > shared_to_date
            if shared_invalid_range:
                st.error("Governance From Date must be on or before Governance To Date.")

            # Keep section-specific state in sync for consistency across panel widgets/export actions.
            st.session_state["admin_documents_handoff_governance_preset_audit_date_preset"] = shared_preset
            st.session_state["admin_documents_handoff_governance_preset_audit_from_date"] = shared_from_date
            st.session_state["admin_documents_handoff_governance_preset_audit_to_date"] = shared_to_date
            st.session_state["admin_documents_handoff_clear_audit_date_preset"] = shared_preset
            st.session_state["admin_documents_handoff_clear_audit_from_date"] = shared_from_date
            st.session_state["admin_documents_handoff_clear_audit_to_date"] = shared_to_date

        st.markdown("#### Governance Preset Audit Summary")
        preset_audit_limit = st.number_input(
            "Preset Audit Lookback Rows",
            min_value=100,
            max_value=5000,
            value=500,
            step=100,
            key="admin_documents_handoff_governance_preset_audit_limit",
        )
        if governance_review_mode:
            st.caption("Using shared governance date controls above.")
            preset_audit_from_date = shared_from_date
            preset_audit_to_date = shared_to_date
            invalid_preset_audit_range = shared_invalid_range
        else:
            preset_audit_preset_options = ["Last 7d", "Last 30d", "This Month", "Custom"]
            preset_audit_preset = st.selectbox(
                "Preset Audit Date Preset",
                options=preset_audit_preset_options,
                index=1,
                key="admin_documents_handoff_governance_preset_audit_date_preset",
            )
            preset_audit_today = utcnow_naive().date()
            if preset_audit_preset == "Last 7d":
                st.session_state["admin_documents_handoff_governance_preset_audit_from_date"] = (
                    preset_audit_today - timedelta(days=6)
                )
                st.session_state["admin_documents_handoff_governance_preset_audit_to_date"] = preset_audit_today
            elif preset_audit_preset == "Last 30d":
                st.session_state["admin_documents_handoff_governance_preset_audit_from_date"] = (
                    preset_audit_today - timedelta(days=29)
                )
                st.session_state["admin_documents_handoff_governance_preset_audit_to_date"] = preset_audit_today
            elif preset_audit_preset == "This Month":
                st.session_state["admin_documents_handoff_governance_preset_audit_from_date"] = (
                    preset_audit_today.replace(day=1)
                )
                st.session_state["admin_documents_handoff_governance_preset_audit_to_date"] = preset_audit_today
            prd1, prd2 = st.columns(2)
            with prd1:
                preset_audit_from_date = st.date_input(
                    "Preset Audit From Date",
                    key="admin_documents_handoff_governance_preset_audit_from_date",
                )
            with prd2:
                preset_audit_to_date = st.date_input(
                    "Preset Audit To Date",
                    key="admin_documents_handoff_governance_preset_audit_to_date",
                )
            invalid_preset_audit_range = preset_audit_from_date > preset_audit_to_date
        if invalid_preset_audit_range:
            st.error("Preset audit From Date must be on or before To Date.")
        preset_audit_logs = repo.list_audit_logs(limit=int(preset_audit_limit))
        preset_event_rows: list[dict] = []
        for row in preset_audit_logs:
            if str(row.entity_type or "").strip().lower() != "documents_handoff_governance_preset":
                continue
            event_date = None
            try:
                event_date = row.created_at.date() if row.created_at is not None else None
            except Exception:
                event_date = None
            if invalid_preset_audit_range:
                continue
            if event_date is not None:
                if event_date < preset_audit_from_date or event_date > preset_audit_to_date:
                    continue
            changes_obj: dict = {}
            try:
                parsed_changes = json.loads(str(row.changes_json or "{}"))
                if isinstance(parsed_changes, dict):
                    changes_obj = parsed_changes
            except Exception:
                changes_obj = {}
            preset_event_rows.append(
                {
                    "created_at": row.created_at.isoformat() if row.created_at else "",
                    "created_date": event_date.isoformat() if event_date is not None else "",
                    "actor": str(row.actor or "").strip().lower(),
                    "action": str(row.action or "").strip().lower(),
                    "scope": str(changes_obj.get("scope") or "").strip().lower(),
                    "preset_name": str(changes_obj.get("preset_name") or "").strip(),
                    "environment": str(changes_obj.get("environment") or "").strip().lower(),
                }
            )
        if not is_admin_user:
            username_scope = str(user.username).strip().lower()
            preset_event_rows = [row for row in preset_event_rows if row.get("actor") == username_scope]
        if not preset_event_rows:
            st.caption("No governance preset audit events found in selected lookback.")
        else:
            preset_df = pd.DataFrame(preset_event_rows).sort_values("created_at", ascending=False)
            pa1, pa2, pa3 = st.columns(3)
            pa1.metric("Preset Events", int(len(preset_df)))
            pa2.metric("Actors", int(preset_df["actor"].nunique()))
            pa3.metric("Actions", int(preset_df["action"].nunique()))
            by_action_df = (
                preset_df.groupby(["action"], as_index=False)
                .size()
                .rename(columns={"size": "events"})
                .sort_values("events", ascending=False)
            )
            by_actor_df = (
                preset_df.groupby(["actor"], as_index=False)
                .size()
                .rename(columns={"size": "events"})
                .sort_values("events", ascending=False)
            )
            pca, pcb = st.columns(2)
            with pca:
                st.caption("By Action")
                st.dataframe(by_action_df, use_container_width=True, hide_index=True)
            with pcb:
                st.caption("By Actor")
                st.dataframe(by_actor_df, use_container_width=True, hide_index=True)
            st.caption("Recent preset events")
            st.dataframe(preset_df, use_container_width=True, hide_index=True)
            st.download_button(
                "Download Preset Audit CSV",
                data=preset_df.to_csv(index=False).encode("utf-8"),
                file_name=f"documents_handoff_governance_preset_audit_{settings.app_env}.csv",
                mime="text/csv",
                key="admin_documents_handoff_governance_preset_audit_download",
            )

        clear_audit_limit = st.number_input(
            "Clear Audit Lookback Rows",
            min_value=100,
            max_value=5000,
            value=1000,
            step=100,
            key="admin_documents_handoff_clear_audit_limit",
        )
        if governance_review_mode:
            st.caption("Using shared governance date controls above.")
            clear_audit_from_date = shared_from_date
            clear_audit_to_date = shared_to_date
            invalid_clear_audit_range = shared_invalid_range
        else:
            preset_options = ["Last 7d", "Last 30d", "This Month", "Custom"]
            preset = st.selectbox(
                "Date Preset",
                options=preset_options,
                index=1,
                key="admin_documents_handoff_clear_audit_date_preset",
            )
            today_local = utcnow_naive().date()
            if preset == "Last 7d":
                st.session_state["admin_documents_handoff_clear_audit_from_date"] = today_local - timedelta(days=6)
                st.session_state["admin_documents_handoff_clear_audit_to_date"] = today_local
            elif preset == "Last 30d":
                st.session_state["admin_documents_handoff_clear_audit_from_date"] = today_local - timedelta(days=29)
                st.session_state["admin_documents_handoff_clear_audit_to_date"] = today_local
            elif preset == "This Month":
                st.session_state["admin_documents_handoff_clear_audit_from_date"] = today_local.replace(day=1)
                st.session_state["admin_documents_handoff_clear_audit_to_date"] = today_local
            dr1, dr2 = st.columns(2)
            with dr1:
                clear_audit_from_date = st.date_input(
                    "From Date",
                    key="admin_documents_handoff_clear_audit_from_date",
                )
            with dr2:
                clear_audit_to_date = st.date_input(
                    "To Date",
                    key="admin_documents_handoff_clear_audit_to_date",
                )
            invalid_clear_audit_range = clear_audit_from_date > clear_audit_to_date
        if invalid_clear_audit_range:
            st.error("From Date must be on or before To Date.")
        audit_rows = repo.list_audit_logs(limit=int(clear_audit_limit))
        clear_rows: list[dict] = []
        for row in audit_rows:
            if str(row.entity_type or "").strip().lower() != "documents_handoff_history":
                continue
            if str(row.action or "").strip().lower() != "clear_history":
                continue
            changes_obj: dict = {}
            try:
                parsed_changes = json.loads(str(row.changes_json or "{}"))
                if isinstance(parsed_changes, dict):
                    changes_obj = parsed_changes
            except Exception:
                changes_obj = {}
            created_at_iso = row.created_at.isoformat() if row.created_at else ""
            created_at_date = None
            try:
                created_at_date = row.created_at.date() if row.created_at is not None else None
            except Exception:
                created_at_date = None
            if invalid_clear_audit_range:
                continue
            if created_at_date is not None and not invalid_clear_audit_range:
                if created_at_date < clear_audit_from_date or created_at_date > clear_audit_to_date:
                    continue
            clear_rows.append(
                {
                    "created_at": created_at_iso,
                    "created_date": created_at_date.isoformat() if created_at_date is not None else "",
                    "actor": str(row.actor or "").strip().lower(),
                    "scope": str(changes_obj.get("scope") or "").strip().lower(),
                    "target_user": str(changes_obj.get("target_user") or "").strip().lower(),
                    "environment": str(changes_obj.get("environment") or "").strip().lower(),
                    "reason_code": str(changes_obj.get("reason_code") or "").strip().lower(),
                    "reason_note": str(changes_obj.get("reason_note") or "").strip(),
                    "reason": str(changes_obj.get("reason") or "").strip(),
                }
            )
        if not is_admin_user:
            username_scope = str(user.username).strip().lower()
            clear_rows = [
                row
                for row in clear_rows
                if row.get("actor") == username_scope or row.get("target_user") == username_scope
            ]
        if not clear_rows:
            st.caption("No handoff clear audit events found in the selected lookback.")
        else:
            clear_df_all = pd.DataFrame(clear_rows).sort_values("created_at", ascending=False)
            reason_filter_options = sorted(
                {
                    str(value).strip()
                    for value in clear_df_all["reason_code"].fillna("").tolist()
                    if str(value).strip()
                }
            )
            scope_filter_options = sorted(
                {
                    str(value).strip()
                    for value in clear_df_all["scope"].fillna("").tolist()
                    if str(value).strip()
                }
            )
            rf1, rf2 = st.columns(2)
            with rf1:
                selected_reason_codes = st.multiselect(
                    "Filter Reason Code",
                    options=reason_filter_options,
                    default=[],
                    key="admin_documents_handoff_clear_audit_reason_filter",
                )
            with rf2:
                selected_scopes = st.multiselect(
                    "Filter Scope",
                    options=scope_filter_options,
                    default=[],
                    key="admin_documents_handoff_clear_audit_scope_filter",
                )
            clear_df = clear_df_all.copy()
            if selected_reason_codes:
                clear_df = clear_df[
                    clear_df["reason_code"].astype(str).isin([str(v) for v in selected_reason_codes])
                ]
            if selected_scopes:
                clear_df = clear_df[
                    clear_df["scope"].astype(str).isin([str(v) for v in selected_scopes])
                ]
            clear_df = clear_df.sort_values("created_at", ascending=False)
            if clear_df.empty:
                st.caption("No clear events for selected reason/scope filters.")
            else:
                ca1, ca2, ca3 = st.columns(3)
                ca1.metric("Clear Events", int(len(clear_df)))
                ca2.metric("Actors", int(clear_df["actor"].nunique()))
                ca3.metric("Targets", int(clear_df["target_user"].nunique()))
                by_actor_df = (
                    clear_df.groupby(["actor"], as_index=False)
                    .size()
                    .rename(columns={"size": "clear_events"})
                    .sort_values("clear_events", ascending=False)
                )
                by_target_df = (
                    clear_df.groupby(["target_user"], as_index=False)
                    .size()
                    .rename(columns={"size": "targeted_events"})
                    .sort_values("targeted_events", ascending=False)
                )
                by_reason_df = (
                    clear_df.assign(
                        reason_code=clear_df["reason_code"]
                        .astype(str)
                        .str.strip()
                        .replace("", "(unspecified)")
                    )
                    .groupby(["reason_code"], as_index=False)
                    .size()
                    .rename(columns={"size": "events"})
                    .sort_values("events", ascending=False)
                )
                cxa, cxb = st.columns(2)
                with cxa:
                    st.caption("By Actor")
                    st.dataframe(by_actor_df, use_container_width=True, hide_index=True)
                with cxb:
                    st.caption("By Target User")
                    st.dataframe(by_target_df, use_container_width=True, hide_index=True)
                st.caption("By Reason Code")
                st.dataframe(by_reason_df, use_container_width=True, hide_index=True)
                if not by_reason_df.empty:
                    st.bar_chart(by_reason_df.set_index("reason_code")["events"], use_container_width=True)
                st.caption("Recent clear events")
                st.dataframe(clear_df, use_container_width=True, hide_index=True)
                st.download_button(
                    "Download Clear Audit CSV",
                    data=clear_df.to_csv(index=False).encode("utf-8"),
                    file_name=f"documents_handoff_clear_audit_{settings.app_env}.csv",
                    mime="text/csv",
                    key="admin_documents_handoff_clear_audit_download",
                )
                bundle_buffer = BytesIO()
                with zipfile.ZipFile(bundle_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as bundle_zip:
                    bundle_zip.writestr("clear_events_raw.csv", clear_df.to_csv(index=False))
                    bundle_zip.writestr("clear_events_by_actor.csv", by_actor_df.to_csv(index=False))
                    bundle_zip.writestr("clear_events_by_target.csv", by_target_df.to_csv(index=False))
                    bundle_zip.writestr("clear_events_by_reason_code.csv", by_reason_df.to_csv(index=False))
                bundle_buffer.seek(0)
                st.download_button(
                    "Export Governance Bundle (ZIP)",
                    data=bundle_buffer.getvalue(),
                    file_name=f"documents_handoff_governance_bundle_{settings.app_env}.zip",
                    mime="application/zip",
                    key="admin_documents_handoff_governance_bundle_download",
                )

        st.markdown("### Seed Recommended Runtime Keys")
        if st.button("Seed Defaults From Current Env", key="admin_runtime_seed_btn"):
            seeded = _seed_missing_runtime_defaults(
                repo,
                actor=user.username,
                seed_defaults=runtime_seed_defaults,
            )
            if seeded:
                st.success(f"Seeded {seeded} runtime settings.")
                st.rerun()
            else:
                st.info("No new runtime settings were seeded.")

        st.markdown("### Add/Update Runtime Setting")
        with st.form("admin_runtime_upsert_form"):
            c1, c2 = st.columns(2)
            with c1:
                runtime_key = st.text_input("Setting Key", value="")
            with c2:
                runtime_type = st.selectbox("Value Type", options=["str", "int", "float", "bool", "json"], index=0)
            runtime_value = st.text_area("Value")
            runtime_description = st.text_input("Description")
            runtime_active = st.checkbox("Active", value=True)
            runtime_submit = st.form_submit_button("Save Runtime Setting")
        if runtime_submit:
            try:
                row = repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key=(runtime_key or "").strip(),
                    value=(runtime_value or "").strip(),
                    value_type=runtime_type,
                    description=(runtime_description or "").strip(),
                    is_active=bool(runtime_active),
                    actor=user.username,
                )
                st.success(f"Saved runtime setting `{row.key}`.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to save runtime setting: {exc}")

        if runtime_rows:
            st.markdown("### Manage Existing Runtime Setting")
            runtime_map = {
                f"#{row.id} | {row.key} | type={row.value_type} | active={row.is_active}": row
                for row in runtime_rows
            }
            selected_runtime_key = st.selectbox(
                "Select Runtime Setting",
                options=list(runtime_map.keys()),
                key="admin_runtime_setting_select",
            )
            selected_runtime = runtime_map[selected_runtime_key]

            with st.form("admin_runtime_update_selected_form"):
                selected_value = st.text_area("Value", value=selected_runtime.value)
                selected_type = st.selectbox(
                    "Value Type",
                    options=["str", "int", "float", "bool", "json"],
                    index=["str", "int", "float", "bool", "json"].index(selected_runtime.value_type)
                    if selected_runtime.value_type in {"str", "int", "float", "bool", "json"}
                    else 0,
                )
                selected_desc = st.text_input("Description", value=selected_runtime.description)
                selected_active = st.checkbox("Active", value=bool(selected_runtime.is_active))
                update_selected_runtime = st.form_submit_button("Update Selected")
            if update_selected_runtime:
                try:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=selected_runtime.key,
                        value=selected_value,
                        value_type=selected_type,
                        description=selected_desc,
                        is_active=bool(selected_active),
                        actor=user.username,
                    )
                    st.success("Runtime setting updated.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to update runtime setting: {exc}")

            with st.form("admin_runtime_delete_form"):
                confirm_runtime_delete = st.checkbox("I understand this deletes the selected runtime setting.")
                runtime_delete_phrase = st.text_input("Type DELETE to confirm")
                runtime_delete_submit = st.form_submit_button("Delete Selected Runtime Setting")
            if runtime_delete_submit:
                if not confirm_runtime_delete or runtime_delete_phrase.strip() != "DELETE":
                    st.error("Confirm deletion and type `DELETE` exactly.")
                else:
                    try:
                        repo.delete_runtime_setting_by_id(
                            setting_id=selected_runtime.id,
                            actor=user.username,
                        )
                        st.success("Runtime setting deleted.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Unable to delete runtime setting: {exc}")

    with tab_integrations:
        st.markdown("### Integrations (Google + Slack)")
        st.caption(
            "Environment-scoped integration runtime settings. "
            "These are foundation controls for GS-V05-007 (Google) and Slack notifications."
        )
        runtime_map = {str(row.key): row for row in repo.list_runtime_settings(environment=settings.app_env, active_only=False)}

        def _rv(key: str, default: str = "") -> str:
            row = runtime_map.get(key)
            if row is None:
                return default
            return str(row.value or default)

        def _rb(key: str, default: bool = False) -> bool:
            return _rv(key, "true" if default else "false").strip().lower() in {"1", "true", "yes", "on"}

        st.markdown("#### Google Workspace")
        with st.form("admin_google_integration_form"):
            g1, g2 = st.columns(2)
            with g1:
                google_enabled = st.checkbox(
                    "Enable Google Integration",
                    value=_rb("google_integration_enabled", False),
                    help="Master toggle for Gmail/Calendar/Drive integration features.",
                )
                google_client_id = st.text_input(
                    "Google OAuth Client ID",
                    value=_rv("google_oauth_client_id", ""),
                )
                google_redirect_uri = st.text_input(
                    "Google OAuth Redirect URI",
                    value=_rv("google_oauth_redirect_uri", ""),
                )
                google_scopes = st.text_area(
                    "Google Scopes CSV",
                    value=_rv(
                        "google_workspace_scopes_csv",
                        "https://www.googleapis.com/auth/gmail.send,https://www.googleapis.com/auth/calendar.events,https://www.googleapis.com/auth/drive.file",
                    ),
                    height=90,
                )
            with g2:
                google_client_secret = st.text_input(
                    "Google OAuth Client Secret",
                    value=_rv("google_oauth_client_secret", ""),
                    type="password",
                )
                google_access_token = st.text_input(
                    "Google Access Token",
                    value=_rv("google_oauth_access_token", ""),
                    type="password",
                )
                google_refresh_token = st.text_input(
                    "Google Refresh Token (Optional)",
                    value=_rv("google_oauth_refresh_token", ""),
                    type="password",
                )
                google_sender = st.text_input(
                    "Default Sender Email",
                    value=_rv("google_default_sender_email", "sales@goldenstackers.com"),
                )
                google_drive_root = st.text_input(
                    "Default Drive Folder ID (Optional)",
                    value=_rv("google_drive_root_folder_id", ""),
                )
                google_calendar_id = st.text_input(
                    "Default Calendar ID",
                    value=_rv("google_default_calendar_id", "primary"),
                )
                google_timezone = st.text_input(
                    "Default Time Zone",
                    value=_rv("google_default_timezone", "America/Denver"),
                )
                google_timeout = st.number_input(
                    "Google HTTP Timeout Seconds",
                    min_value=5,
                    max_value=120,
                    value=max(5, min(120, int(_rv("google_http_timeout_seconds", "30") or "30"))),
                    step=1,
                )
                google_queue_enabled = st.checkbox(
                    "Enable Google Retry Queue",
                    value=_rb("google_queue_enabled", True),
                )
                google_queue_max_retries = st.number_input(
                    "Google Queue Max Retries",
                    min_value=0,
                    max_value=20,
                    value=max(0, min(20, int(_rv("google_queue_max_retries", "5") or "5"))),
                    step=1,
                )
                google_backoff_base = st.number_input(
                    "Queue Backoff Base Seconds",
                    min_value=5,
                    max_value=3600,
                    value=max(5, min(3600, int(_rv("google_queue_backoff_base_seconds", "120") or "120"))),
                    step=5,
                )
                google_backoff_max = st.number_input(
                    "Queue Backoff Max Seconds",
                    min_value=5,
                    max_value=86400,
                    value=max(5, min(86400, int(_rv("google_queue_backoff_max_seconds", "3600") or "3600"))),
                    step=30,
                )
            save_google = st.form_submit_button("Save Google Integration Settings")
        if save_google:
            try:
                updates = [
                    ("google_integration_enabled", "true" if google_enabled else "false", "bool", "Master toggle for Google Workspace integration features (Gmail/Calendar/Drive)."),
                    ("google_oauth_client_id", google_client_id.strip(), "str", "Google OAuth client ID for this environment."),
                    ("google_oauth_client_secret", google_client_secret.strip(), "str", "Google OAuth client secret for this environment."),
                    ("google_oauth_redirect_uri", google_redirect_uri.strip(), "str", "Google OAuth redirect URI for this environment."),
                    ("google_workspace_scopes_csv", google_scopes.strip(), "str", "Comma-separated Google OAuth scopes requested by the app."),
                    ("google_oauth_access_token", google_access_token.strip(), "str", "Google OAuth access token for API calls (runtime-managed credential)."),
                    ("google_oauth_refresh_token", google_refresh_token.strip(), "str", "Google OAuth refresh token for future token refresh flow."),
                    ("google_default_sender_email", google_sender.strip(), "str", "Default sender email used for Gmail invoice/receipt workflows."),
                    ("google_drive_root_folder_id", google_drive_root.strip(), "str", "Optional default Google Drive folder ID for exports/uploads."),
                    ("google_default_calendar_id", google_calendar_id.strip() or "primary", "str", "Default Google Calendar ID for follow-up event creation."),
                    ("google_default_timezone", google_timezone.strip() or "America/Denver", "str", "Default timezone for Google Calendar event scheduling."),
                    ("google_http_timeout_seconds", str(int(google_timeout)), "int", "Timeout for Google API HTTP requests."),
                    ("google_queue_enabled", "true" if google_queue_enabled else "false", "bool", "Enable/disable Google integration retry queue for failed actions."),
                    ("google_queue_max_retries", str(int(google_queue_max_retries)), "int", "Maximum retry attempts per queued Google integration action."),
                    ("google_queue_backoff_base_seconds", str(int(google_backoff_base)), "int", "Base backoff seconds for exponential retry scheduling."),
                    ("google_queue_backoff_max_seconds", str(int(google_backoff_max)), "int", "Maximum backoff seconds for queued retries."),
                ]
                for key, value, value_type, description in updates:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=key,
                        value=value,
                        value_type=value_type,
                        description=description,
                        is_active=True,
                        actor=user.username,
                    )
                st.success("Google integration settings saved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to save Google integration settings: {exc}")

        st.divider()
        st.markdown("#### Shipping Queue Controls")
        with st.form("admin_shipping_queue_settings_form"):
            sq1, sq2 = st.columns(2)
            with sq1:
                shipping_queue_enabled = st.checkbox(
                    "Enable Shipping Queue",
                    value=_rb("shipping_queue_enabled", True),
                )
                shipping_label_purchase_enabled = st.checkbox(
                    "Enable Label Purchase Actions",
                    value=_rb("shipping_label_purchase_enabled", True),
                )
                shipping_label_live_provider_calls_enabled = st.checkbox(
                    "Enable Live Provider Calls",
                    value=_rb("shipping_label_live_provider_calls_enabled", False),
                    help="Keep disabled until provider adapters are fully wired and validated.",
                )
                shipping_queue_max_retries = st.number_input(
                    "Shipping Queue Max Retries",
                    min_value=0,
                    max_value=20,
                    value=max(0, min(20, int(_rv("shipping_queue_max_retries", "5") or "5"))),
                    step=1,
                )
                shipping_queue_backoff_base = st.number_input(
                    "Shipping Queue Backoff Base Seconds",
                    min_value=5,
                    max_value=3600,
                    value=max(5, min(3600, int(_rv("shipping_queue_backoff_base_seconds", "60") or "60"))),
                    step=5,
                )
                shipping_queue_backoff_max = st.number_input(
                    "Shipping Queue Backoff Max Seconds",
                    min_value=5,
                    max_value=86400,
                    value=max(5, min(86400, int(_rv("shipping_queue_backoff_max_seconds", "3600") or "3600"))),
                    step=30,
                )
            with sq2:
                shipping_label_provider_pirateship_enabled = st.checkbox(
                    "Provider: Pirate Ship",
                    value=_rb("shipping_label_provider_pirateship_enabled", True),
                )
                shipping_label_pirateship_mode = st.selectbox(
                    "Pirate Ship Adapter Mode",
                    options=["mock", "api"],
                    index=0 if (_rv("shipping_label_pirateship_mode", "mock").strip().lower() != "api") else 1,
                    help="`mock` returns generated artifacts locally. `api` calls configured Pirate Ship endpoint.",
                )
                shipping_label_pirateship_base_url = st.text_input(
                    "Pirate Ship Base URL",
                    value=_rv("shipping_label_pirateship_base_url", ""),
                )
                shipping_label_pirateship_api_key = st.text_input(
                    "Pirate Ship API Key",
                    value=_rv("shipping_label_pirateship_api_key", ""),
                    type="password",
                )
                shipping_label_pirateship_endpoint_path = st.text_input(
                    "Pirate Ship Endpoint Path",
                    value=_rv("shipping_label_pirateship_endpoint_path", "/v1/labels/purchase"),
                )
                shipping_label_pirateship_auth_scheme = st.selectbox(
                    "Pirate Ship Auth Scheme",
                    options=["bearer", "token"],
                    index=0 if (_rv("shipping_label_pirateship_auth_scheme", "bearer").strip().lower() != "token") else 1,
                )
                shipping_label_pirateship_timeout_seconds = st.number_input(
                    "Pirate Ship Timeout Seconds",
                    min_value=5,
                    max_value=120,
                    value=max(5, min(120, int(_rv("shipping_label_pirateship_timeout_seconds", "20") or "20"))),
                    step=1,
                )
                shipping_label_provider_ebay_shipping_enabled = st.checkbox(
                    "Provider: eBay Shipping",
                    value=_rb("shipping_label_provider_ebay_shipping_enabled", True),
                )
                shipping_label_provider_usps_enabled = st.checkbox(
                    "Provider: USPS",
                    value=_rb("shipping_label_provider_usps_enabled", True),
                )
                shipping_label_provider_ups_enabled = st.checkbox(
                    "Provider: UPS",
                    value=_rb("shipping_label_provider_ups_enabled", True),
                )
                shipping_label_provider_fedex_enabled = st.checkbox(
                    "Provider: FedEx",
                    value=_rb("shipping_label_provider_fedex_enabled", True),
                )
                shipping_label_provider_other_enabled = st.checkbox(
                    "Provider: Other",
                    value=_rb("shipping_label_provider_other_enabled", True),
                )
            save_shipping_queue = st.form_submit_button("Save Shipping Queue Settings")
        if save_shipping_queue:
            try:
                updates = [
                    ("shipping_queue_enabled", "true" if shipping_queue_enabled else "false", "bool", "Enable/disable shipping integration retry queue execution."),
                    ("shipping_queue_max_retries", str(int(shipping_queue_max_retries)), "int", "Default max retries for queued shipping label purchase jobs."),
                    ("shipping_queue_backoff_base_seconds", str(int(shipping_queue_backoff_base)), "int", "Base backoff seconds for shipping queue retry scheduling."),
                    ("shipping_queue_backoff_max_seconds", str(int(shipping_queue_backoff_max)), "int", "Maximum backoff seconds for shipping queue retries."),
                    ("shipping_label_purchase_enabled", "true" if shipping_label_purchase_enabled else "false", "bool", "Enable/disable shipping label purchase queue actions."),
                    ("shipping_label_live_provider_calls_enabled", "true" if shipping_label_live_provider_calls_enabled else "false", "bool", "Guardrail toggle for live external label-purchase API calls."),
                    ("shipping_label_provider_pirateship_enabled", "true" if shipping_label_provider_pirateship_enabled else "false", "bool", "Enable/disable Pirate Ship as a shipping label provider."),
                    ("shipping_label_pirateship_mode", shipping_label_pirateship_mode.strip().lower(), "str", "Pirate Ship adapter mode (`mock` or `api`) for live-provider execution path."),
                    ("shipping_label_pirateship_base_url", shipping_label_pirateship_base_url.strip(), "str", "Pirate Ship adapter base URL for API mode."),
                    ("shipping_label_pirateship_api_key", shipping_label_pirateship_api_key.strip(), "str", "Pirate Ship adapter API key/token for API mode."),
                    ("shipping_label_pirateship_endpoint_path", shipping_label_pirateship_endpoint_path.strip() or "/v1/labels/purchase", "str", "Pirate Ship adapter endpoint path (joined with base URL)."),
                    ("shipping_label_pirateship_auth_scheme", shipping_label_pirateship_auth_scheme.strip().lower() or "bearer", "str", "Pirate Ship auth scheme (`bearer` or `token`)."),
                    ("shipping_label_pirateship_timeout_seconds", str(int(shipping_label_pirateship_timeout_seconds)), "int", "Pirate Ship API timeout seconds for live mode."),
                    ("shipping_label_provider_ebay_shipping_enabled", "true" if shipping_label_provider_ebay_shipping_enabled else "false", "bool", "Enable/disable eBay Shipping as a shipping label provider."),
                    ("shipping_label_provider_usps_enabled", "true" if shipping_label_provider_usps_enabled else "false", "bool", "Enable/disable USPS as a shipping label provider."),
                    ("shipping_label_provider_ups_enabled", "true" if shipping_label_provider_ups_enabled else "false", "bool", "Enable/disable UPS as a shipping label provider."),
                    ("shipping_label_provider_fedex_enabled", "true" if shipping_label_provider_fedex_enabled else "false", "bool", "Enable/disable FedEx as a shipping label provider."),
                    ("shipping_label_provider_other_enabled", "true" if shipping_label_provider_other_enabled else "false", "bool", "Enable/disable generic/other shipping label provider jobs."),
                ]
                for key, value, value_type, description in updates:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=key,
                        value=value,
                        value_type=value_type,
                        description=description,
                        is_active=True,
                        actor=user.username,
                    )
                st.success("Shipping queue settings saved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to save shipping queue settings: {exc}")

        with st.form("admin_shipping_label_adapter_test_form"):
            st.markdown("#### Test Pirate Ship Adapter")
            st.caption("Runs a non-queue test call using current runtime config (`mock` or `api`).")
            tp1, tp2, tp3 = st.columns(3)
            with tp1:
                test_sale_id = st.number_input(
                    "Test Sale ID",
                    min_value=1,
                    max_value=10_000_000,
                    value=1,
                    step=1,
                )
            with tp2:
                test_service = st.text_input("Test Service", value="Ground Advantage")
            with tp3:
                test_package = st.text_input("Test Package Type", value="small_box")
            tq1, tq2, tq3 = st.columns(3)
            with tq1:
                test_tracking = st.text_input("Test Tracking (optional)", value="")
            with tq2:
                test_cost = st.number_input(
                    "Test Label Cost (optional)",
                    min_value=0.0,
                    value=0.0,
                    step=0.01,
                )
            with tq3:
                test_currency = st.text_input("Test Currency", value="USD")
            test_submit = st.form_submit_button("Run Pirate Ship Adapter Test")
        if test_submit:
            try:
                payload = {
                    "sale_id": int(test_sale_id),
                    "shipping_provider": "pirateship",
                    "shipping_service": test_service.strip(),
                    "shipping_package_type": test_package.strip(),
                    "tracking_number": test_tracking.strip(),
                    "shipping_label_cost": float(test_cost) if float(test_cost) > 0 else None,
                    "shipping_label_currency": (test_currency or "USD").strip() or "USD",
                }
                result = purchase_shipping_label(repo, provider="pirateship", payload=payload)
                st.success(
                    f"Adapter test succeeded. label_id={result.label_id}, tracking={result.tracking_number or 'n/a'}"
                )
                st.json(
                    {
                        "label_id": result.label_id,
                        "label_url": result.label_url,
                        "label_cost": result.label_cost,
                        "label_currency": result.label_currency,
                        "tracking_number": result.tracking_number,
                        "provider_payload": result.provider_payload or {},
                    }
                )
                repo.log_integration_event(
                    actor=user.username,
                    integration="shipping_label_adapter",
                    action="pirateship_test",
                    status="success",
                    details={
                        "mode": _rv("shipping_label_pirateship_mode", "mock").strip().lower(),
                        "sale_id": int(test_sale_id),
                        "label_id": result.label_id,
                    },
                )
            except Exception as exc:
                st.error(f"Adapter test failed: {exc}")
                try:
                    repo.log_integration_event(
                        actor=user.username,
                        integration="shipping_label_adapter",
                        action="pirateship_test",
                        status="failed",
                        details={
                            "mode": _rv("shipping_label_pirateship_mode", "mock").strip().lower(),
                            "sale_id": int(test_sale_id),
                            "error": str(exc)[:500],
                        },
                    )
                except Exception:
                    pass

        with st.form("admin_shipping_live_provider_validation_form"):
            st.markdown("#### Live Provider Validation Run")
            st.caption(
                "Guided Dev/Prod evidence run for real provider execution. "
                "This may purchase a real label if live calls are enabled."
            )
            lv1, lv2, lv3 = st.columns(3)
            with lv1:
                validation_target_env = st.selectbox(
                    "Validation Target Env",
                    options=["local", "dev", "prod"],
                    index=0 if settings.app_env == "local" else (1 if settings.app_env == "dev" else 2),
                )
            with lv2:
                validation_provider = st.selectbox(
                    "Provider",
                    options=["pirateship", "ebay_shipping", "usps", "ups", "fedex", "other"],
                    index=0,
                )
            with lv3:
                validation_sale_id = st.number_input(
                    "Sale ID",
                    min_value=1,
                    max_value=10_000_000,
                    value=1,
                    step=1,
                )
            lv4, lv5 = st.columns(2)
            with lv4:
                validation_service = st.text_input("Service", value="Ground Advantage")
            with lv5:
                validation_package = st.text_input("Package Type", value="small_box")
            validation_notes = st.text_area(
                "Validation Notes",
                value="",
                height=90,
                help="Include any test context for evidence/sign-off.",
            )
            validation_confirm_live = st.checkbox(
                "I confirm this run may purchase a real shipping label.",
                value=False,
            )
            validation_submit = st.form_submit_button("Run Live Provider Validation Now")
        if validation_submit:
            try:
                live_calls_enabled = bool(_rb("shipping_label_live_provider_calls_enabled", False))
                pirateship_mode = _rv("shipping_label_pirateship_mode", "mock").strip().lower()
                if not validation_confirm_live:
                    raise ValueError("Confirm live purchase acknowledgement before running validation.")
                if not live_calls_enabled:
                    raise ValueError(
                        "Live provider calls are disabled (`shipping_label_live_provider_calls_enabled=false`)."
                    )
                if validation_provider == "pirateship" and pirateship_mode != "api":
                    raise ValueError(
                        "Pirate Ship validation requires adapter mode `api` (current mode is not api)."
                    )
                provider_enabled_key = f"shipping_label_provider_{validation_provider}_enabled"
                if not bool(_rb(provider_enabled_key, True)):
                    raise ValueError(f"Provider is disabled by runtime setting `{provider_enabled_key}`.")
                validation_payload = {
                    "sale_id": int(validation_sale_id),
                    "shipping_provider": str(validation_provider),
                    "shipping_service": str(validation_service or "").strip(),
                    "shipping_package_type": str(validation_package or "").strip(),
                    "shipping_label_currency": "USD",
                    "validation_target_env": str(validation_target_env),
                    "validation_notes": str(validation_notes or "").strip(),
                    "dry_run": False,
                }
                queued = repo.create_integration_queue_job(
                    environment=settings.app_env,
                    integration="shipping",
                    action="purchase_label",
                    payload_json=json.dumps(validation_payload),
                    requested_by=user.username,
                    max_retries=0,
                    actor=user.username,
                )
                ok, msg = process_integration_queue_job(
                    repo,
                    job_id=int(queued.id),
                    actor=user.username,
                )
                queue_row = repo.db.get(IntegrationQueueJob, int(queued.id))
                sale_row = repo.db.get(Sale, int(validation_sale_id))
                details = {
                    "target_env": str(validation_target_env),
                    "runtime_env": settings.app_env,
                    "provider": str(validation_provider),
                    "sale_id": int(validation_sale_id),
                    "queue_job_id": int(queued.id),
                    "queue_status": str(getattr(queue_row, "status", "") or ""),
                    "message": str(msg or ""),
                    "live_calls_enabled": bool(live_calls_enabled),
                    "pirateship_mode": str(pirateship_mode),
                    "validation_notes": str(validation_notes or "").strip(),
                }
                if sale_row is not None:
                    details.update(
                        {
                            "label_id": str(getattr(sale_row, "shipping_label_id", "") or ""),
                            "label_url": str(getattr(sale_row, "shipping_label_url", "") or ""),
                            "label_cost": (
                                float(getattr(sale_row, "shipping_label_cost"))
                                if getattr(sale_row, "shipping_label_cost", None) is not None
                                else None
                            ),
                            "label_currency": str(getattr(sale_row, "shipping_label_currency", "") or ""),
                            "tracking_number": str(getattr(sale_row, "tracking_number", "") or ""),
                            "tracking_status": str(getattr(sale_row, "tracking_status", "") or ""),
                        }
                    )
                repo.log_integration_event(
                    actor=user.username,
                    integration="shipping_provider_validation",
                    action="live_validation_run",
                    status="success" if ok else "failed",
                    details=details,
                )
                if ok:
                    st.success(f"Live provider validation succeeded. queue_job_id={queued.id}")
                else:
                    st.error(f"Live provider validation failed: {msg}")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to run live provider validation: {exc}")
                try:
                    repo.log_integration_event(
                        actor=user.username,
                        integration="shipping_provider_validation",
                        action="live_validation_run",
                        status="failed",
                        details={
                            "target_env": str(validation_target_env),
                            "runtime_env": settings.app_env,
                            "provider": str(validation_provider),
                            "sale_id": int(validation_sale_id),
                            "validation_notes": str(validation_notes or "").strip(),
                            "error": str(exc)[:500],
                        },
                    )
                except Exception:
                    pass

        st.markdown("#### Recent Live Provider Validation Runs")
        validation_rows = repo.db.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "integration_event",
                AuditLog.created_at >= (utcnow_naive() - timedelta(days=30)),
            )
            .order_by(AuditLog.created_at.desc())
            .limit(1000)
        ).all()
        validation_events: list[dict[str, Any]] = []
        for row in validation_rows:
            try:
                payload = json.loads(row.changes_json or "{}")
            except Exception:
                payload = {}
            after = payload.get("after") if isinstance(payload, dict) else {}
            if not isinstance(after, dict):
                after = {}
            if str(after.get("integration") or "").strip().lower() != "shipping_provider_validation":
                continue
            details = after.get("details") if isinstance(after.get("details"), dict) else {}
            validation_events.append(
                {
                    "created_at": row.created_at.isoformat() if row.created_at else "",
                    "actor": str(row.actor or ""),
                    "status": str(after.get("status") or ""),
                    "target_env": str(details.get("target_env") or ""),
                    "provider": str(details.get("provider") or ""),
                    "sale_id": details.get("sale_id"),
                    "queue_job_id": details.get("queue_job_id"),
                    "queue_status": str(details.get("queue_status") or ""),
                    "label_id": str(details.get("label_id") or ""),
                    "tracking_number": str(details.get("tracking_number") or ""),
                    "message": str(details.get("message") or ""),
                    "notes": str(details.get("validation_notes") or ""),
                    "error": str(details.get("error") or "")[:220],
                }
            )
        if validation_events:
            validation_df = pd.DataFrame(validation_events)
            st.dataframe(validation_df, use_container_width=True, hide_index=True)
            st.download_button(
                "Download Live Provider Validation CSV",
                data=validation_df.to_csv(index=False).encode("utf-8"),
                file_name=f"shipping_provider_validation_{settings.app_env}_{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv",
                key="admin_shipping_provider_validation_download_csv_btn",
            )
        else:
            st.caption("No live provider validation runs in last 30 days.")

        st.markdown("#### Validation Sign-Off (Dev/Prod)")
        st.caption(
            "Record explicit sign-off evidence for live-provider validation per environment to close go-live requirements."
        )
        with st.form("admin_shipping_provider_validation_signoff_form"):
            sv1, sv2 = st.columns(2)
            with sv1:
                signoff_target_env = st.selectbox(
                    "Sign-Off Environment",
                    options=["dev", "prod"],
                    index=0,
                    key="admin_shipping_provider_validation_signoff_target_env",
                )
                signoff_date = st.date_input(
                    "Sign-Off Date",
                    value=utcnow_naive().date(),
                    key="admin_shipping_provider_validation_signoff_date",
                )
                signoff_owner = st.text_input(
                    "Owner",
                    value=str(user.username or ""),
                    key="admin_shipping_provider_validation_signoff_owner",
                )
            with sv2:
                signoff_status = st.selectbox(
                    "Sign-Off Status",
                    options=["approved", "blocked", "needs_followup"],
                    index=0,
                    key="admin_shipping_provider_validation_signoff_status",
                )
                signoff_evidence_link = st.text_input(
                    "Evidence Link",
                    placeholder="ticket/runbook/artifact URL",
                    key="admin_shipping_provider_validation_signoff_evidence_link",
                )
            signoff_notes = st.text_area(
                "Sign-Off Notes",
                placeholder="What was validated, rollback path, outstanding risks.",
                key="admin_shipping_provider_validation_signoff_notes",
            )
            create_signoff = st.form_submit_button("Record Validation Sign-Off")
        if create_signoff:
            try:
                repo.record_audit_event(
                    entity_type="shipping_provider_validation_signoff",
                    entity_id=None,
                    action="record",
                    actor=user.username,
                    changes={
                        "target_env": str(signoff_target_env or "").strip().lower(),
                        "signoff_date": str(signoff_date.isoformat()),
                        "owner": str(signoff_owner or "").strip(),
                        "status": str(signoff_status or "").strip().lower(),
                        "evidence_link": str(signoff_evidence_link or "").strip(),
                        "notes": str(signoff_notes or "").strip(),
                    },
                )
                st.success("Validation sign-off recorded.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to record validation sign-off: {exc}")

        signoff_logs = repo.db.scalars(
            select(AuditLog)
            .where(AuditLog.entity_type == "shipping_provider_validation_signoff")
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(200)
        ).all()
        signoff_rows: list[dict[str, Any]] = []
        for row in signoff_logs:
            payload = _audit_changes(row)
            signoff_rows.append(
                {
                    "recorded_at": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
                    "actor": str(row.actor or ""),
                    "target_env": str(payload.get("target_env") or ""),
                    "signoff_date": str(payload.get("signoff_date") or ""),
                    "owner": str(payload.get("owner") or ""),
                    "status": str(payload.get("status") or ""),
                    "evidence_link": str(payload.get("evidence_link") or ""),
                    "notes": str(payload.get("notes") or "")[:220],
                }
            )
        if signoff_rows:
            signoff_df = pd.DataFrame(signoff_rows)
            st.dataframe(signoff_df, use_container_width=True, hide_index=True)
            latest_status_by_env: dict[str, str] = {}
            for env in ["dev", "prod"]:
                latest = next(
                    (
                        row
                        for row in signoff_rows
                        if str(row.get("target_env") or "").strip().lower() == env
                    ),
                    None,
                )
                latest_status_by_env[env] = str((latest or {}).get("status") or "")
            s1, s2 = st.columns(2)
            s1.metric("Dev Sign-Off", latest_status_by_env.get("dev") or "missing")
            s2.metric("Prod Sign-Off", latest_status_by_env.get("prod") or "missing")
            st.download_button(
                "Download Validation Sign-Off CSV",
                data=signoff_df.to_csv(index=False).encode("utf-8"),
                file_name=f"shipping_provider_validation_signoff_{settings.app_env}_{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv",
                key="admin_shipping_provider_validation_signoff_download_csv_btn",
            )
        else:
            st.caption("No validation sign-off records yet.")

        st.markdown("#### Recent Shipping Adapter Test Events")
        shipping_adapter_rows = repo.db.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "integration_event",
                AuditLog.created_at >= (utcnow_naive() - timedelta(days=14)),
            )
            .order_by(AuditLog.created_at.desc())
            .limit(500)
        ).all()
        shipping_adapter_events: list[dict[str, str]] = []
        for row in shipping_adapter_rows:
            try:
                payload = json.loads(row.changes_json or "{}")
            except Exception:
                payload = {}
            after = payload.get("after") if isinstance(payload, dict) else {}
            if not isinstance(after, dict):
                after = {}
            integration_name = str(after.get("integration") or "").strip().lower()
            if integration_name != "shipping_label_adapter":
                continue
            details = after.get("details") if isinstance(after.get("details"), dict) else {}
            shipping_adapter_events.append(
                {
                    "created_at": row.created_at.isoformat() if row.created_at else "",
                    "actor": row.actor,
                    "action": str(after.get("action") or row.action or ""),
                    "status": str(after.get("status") or ""),
                    "mode": str(details.get("mode") or ""),
                    "sale_id": str(details.get("sale_id") or ""),
                    "label_id": str(details.get("label_id") or ""),
                    "error": str(details.get("error") or "")[:220],
                }
            )
        if shipping_adapter_events:
            st.dataframe(pd.DataFrame(shipping_adapter_events), use_container_width=True, hide_index=True)
        else:
            st.caption("No shipping adapter test events in last 14 days.")

        st.markdown("#### Integration Automation Rules (Preview)")
        st.caption(
            "Define environment-scoped rule records (conditions/effects JSON) with audit trail. "
            "Execution engine wiring is the next step."
        )
        with st.form("admin_integration_automation_runtime_form"):
            ia1, ia2 = st.columns(2)
            with ia1:
                integration_automation_dry_run_enabled = st.checkbox(
                    "Automation Dry-Run Mode",
                    value=_rb("integration_automation_dry_run_enabled", True),
                    help="When enabled, matched rules are logged but updates/block effects are not persisted.",
                )
            with ia2:
                integration_automation_execute_approval_required_enabled = st.checkbox(
                    "Allow Requires-Approval Rules To Execute",
                    value=_rb("integration_automation_execute_approval_required_enabled", False),
                    help="When disabled, requires_approval rules are logged as approval-gated only.",
                )
            save_automation_runtime = st.form_submit_button("Save Automation Runtime Settings")
        if save_automation_runtime:
            try:
                updates = [
                    (
                        "integration_automation_dry_run_enabled",
                        "true" if integration_automation_dry_run_enabled else "false",
                        "bool",
                        "When true, automation rules are evaluated/logged but rule effects are not persisted.",
                    ),
                    (
                        "integration_automation_execute_approval_required_enabled",
                        "true" if integration_automation_execute_approval_required_enabled else "false",
                        "bool",
                        "When true, rules marked requires_approval may auto-apply in execution engine.",
                    ),
                ]
                for key, value, value_type, description in updates:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=key,
                        value=value,
                        value_type=value_type,
                        description=description,
                        is_active=True,
                        actor=user.username,
                    )
                st.success("Automation runtime settings saved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to save automation runtime settings: {exc}")

        with st.form("admin_create_integration_automation_rule_form", clear_on_submit=True):
            ar1, ar2, ar3, ar4 = st.columns(4)
            with ar1:
                rule_name = st.text_input("Rule Name", value="")
            with ar2:
                rule_integration = st.selectbox("Integration", options=["shipping", "google", "slack"], index=0)
            with ar3:
                rule_action = st.text_input("Action", value="purchase_label")
            with ar4:
                rule_trigger_status = st.selectbox(
                    "Trigger Status",
                    options=["queued", "running", "failed", "success"],
                    index=0,
                )
            rb1, rb2 = st.columns(2)
            with rb1:
                rule_requires_approval = st.checkbox("Requires Approval", value=True)
            with rb2:
                rule_is_active = st.checkbox("Active", value=True)
            rule_conditions_json = st.text_area(
                "Conditions JSON",
                value='{"all":[{"field":"payload.shipping_provider","op":"eq","value":"pirateship"}]}',
                height=120,
            )
            rule_effect_json = st.text_area(
                "Effect JSON",
                value='{"type":"queue_update","set":{"priority":"high"}}',
                height=120,
            )
            create_rule_submit = st.form_submit_button("Create Automation Rule")
        if create_rule_submit:
            try:
                repo.create_integration_automation_rule(
                    environment=settings.app_env,
                    integration=rule_integration,
                    action=rule_action,
                    name=rule_name,
                    trigger_status=rule_trigger_status,
                    conditions_json=rule_conditions_json,
                    effect_json=rule_effect_json,
                    requires_approval=rule_requires_approval,
                    is_active=rule_is_active,
                    actor=user.username,
                )
                st.success("Automation rule created.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to create automation rule: {exc}")

        automation_rules = repo.list_integration_automation_rules(
            environment=settings.app_env,
            active_only=False,
            limit=500,
        )
        if automation_rules:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "id": r.id,
                            "integration": r.integration,
                            "action": r.action,
                            "name": r.name,
                            "trigger_status": r.trigger_status,
                            "requires_approval": r.requires_approval,
                            "is_active": r.is_active,
                            "created_by": r.created_by,
                            "updated_by": r.updated_by,
                            "created_at": r.created_at.isoformat() if r.created_at else "",
                            "updated_at": r.updated_at.isoformat() if r.updated_at else "",
                        }
                        for r in automation_rules
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )
            rule_options = {f"#{r.id} | {r.integration}:{r.action} | {r.name}": r for r in automation_rules}
            selected_rule_key = st.selectbox(
                "Edit/Delete Rule",
                options=list(rule_options.keys()),
                key="admin_integration_automation_rule_selected",
            )
            selected_rule = rule_options[selected_rule_key]

            with st.expander("Rule Impact Preview", expanded=False):
                st.caption(
                    "Estimate current queue-job matches for this rule using its integration/action/trigger/conditions."
                )
                p1, p2 = st.columns(2)
                with p1:
                    preview_scan_limit = st.number_input(
                        "Scan Limit",
                        min_value=25,
                        max_value=5000,
                        value=1000,
                        step=25,
                        key=f"admin_rule_preview_scan_limit_{selected_rule.id}",
                        help="Maximum queue jobs scanned in this environment for impact estimate.",
                    )
                with p2:
                    preview_sample_limit = st.number_input(
                        "Sample Rows",
                        min_value=5,
                        max_value=200,
                        value=25,
                        step=5,
                        key=f"admin_rule_preview_sample_limit_{selected_rule.id}",
                    )
                if st.button("Run Impact Preview", key=f"admin_rule_preview_btn_{selected_rule.id}"):
                    try:
                        preview = preview_rule_impact(
                            repo,
                            environment=settings.app_env,
                            integration=str(selected_rule.integration or ""),
                            action=str(selected_rule.action or ""),
                            trigger_status=str(selected_rule.trigger_status or ""),
                            conditions_json=str(selected_rule.conditions_json or "{}"),
                            scan_limit=int(preview_scan_limit),
                            sample_limit=int(preview_sample_limit),
                        )
                        st.success("Rule impact preview complete.")
                        m1, m2, m3, m4 = st.columns(4)
                        m1.metric("Candidates", int(preview.get("candidate_jobs") or 0))
                        m2.metric("Matched", int(preview.get("matched_jobs") or 0))
                        m3.metric("Match Rate", f"{(float(preview.get('match_rate') or 0.0) * 100):.1f}%")
                        m4.metric("Payload Parse Errors", int(preview.get("payload_parse_errors") or 0))
                        sample_rows = preview.get("samples") or []
                        if sample_rows:
                            st.dataframe(pd.DataFrame(sample_rows), use_container_width=True, hide_index=True)
                        else:
                            st.caption("No matching jobs found in current queue snapshot.")
                    except Exception as exc:
                        st.error(f"Unable to preview rule impact: {exc}")

            with st.form("admin_edit_integration_automation_rule_form"):
                er1, er2, er3, er4 = st.columns(4)
                with er1:
                    edit_rule_name = st.text_input("Rule Name", value=selected_rule.name)
                with er2:
                    edit_rule_integration = st.selectbox(
                        "Integration",
                        options=["shipping", "google", "slack"],
                        index=max(0, ["shipping", "google", "slack"].index(selected_rule.integration))
                        if selected_rule.integration in {"shipping", "google", "slack"}
                        else 0,
                    )
                with er3:
                    edit_rule_action = st.text_input("Action", value=selected_rule.action)
                with er4:
                    edit_rule_trigger_status = st.selectbox(
                        "Trigger Status",
                        options=["queued", "running", "failed", "success"],
                        index=max(0, ["queued", "running", "failed", "success"].index(selected_rule.trigger_status))
                        if selected_rule.trigger_status in {"queued", "running", "failed", "success"}
                        else 0,
                    )
                eb1, eb2 = st.columns(2)
                with eb1:
                    edit_rule_requires_approval = st.checkbox(
                        "Requires Approval",
                        value=bool(selected_rule.requires_approval),
                    )
                with eb2:
                    edit_rule_is_active = st.checkbox("Active", value=bool(selected_rule.is_active))
                edit_rule_conditions_json = st.text_area(
                    "Conditions JSON",
                    value=selected_rule.conditions_json or "{}",
                    height=120,
                )
                edit_rule_effect_json = st.text_area(
                    "Effect JSON",
                    value=selected_rule.effect_json or "{}",
                    height=120,
                )
                e1, e2 = st.columns(2)
                with e1:
                    update_rule_submit = st.form_submit_button("Save Rule")
                with e2:
                    delete_rule_submit = st.form_submit_button("Delete Rule")
            if update_rule_submit:
                try:
                    repo.update_integration_automation_rule(
                        selected_rule.id,
                        {
                            "name": edit_rule_name.strip(),
                            "integration": edit_rule_integration.strip().lower(),
                            "action": edit_rule_action.strip().lower(),
                            "trigger_status": edit_rule_trigger_status.strip().lower(),
                            "conditions_json": (edit_rule_conditions_json or "{}").strip() or "{}",
                            "effect_json": (edit_rule_effect_json or "{}").strip() or "{}",
                            "requires_approval": bool(edit_rule_requires_approval),
                            "is_active": bool(edit_rule_is_active),
                        },
                        actor=user.username,
                    )
                    st.success("Automation rule updated.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to update automation rule: {exc}")
            if delete_rule_submit:
                try:
                    repo.delete_integration_automation_rule(
                        rule_id=selected_rule.id,
                        actor=user.username,
                    )
                    st.success("Automation rule deleted.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to delete automation rule: {exc}")
        else:
            st.caption("No integration automation rules configured yet.")

        st.markdown("#### Automation Approvals")
        approval_rule_rows = [
            row
            for row in repo.list_integration_automation_rules(
                environment=settings.app_env,
                active_only=True,
                limit=500,
            )
            if bool(getattr(row, "requires_approval", False))
        ]
        if approval_rule_rows:
            rule_opt_map = {
                f"#{r.id} | {r.integration}:{r.action} | {r.name}": r
                for r in approval_rule_rows
            }
            with st.form("admin_create_automation_approval_form", clear_on_submit=True):
                selected_approval_rule_key = st.selectbox(
                    "Rule",
                    options=list(rule_opt_map.keys()),
                )
                ap1, ap2, ap3 = st.columns(3)
                with ap1:
                    queue_job_id_input = st.number_input(
                        "Queue Job ID (optional, 0=any)",
                        min_value=0,
                        max_value=10_000_000,
                        value=0,
                        step=1,
                    )
                with ap2:
                    expires_in_hours = st.number_input(
                        "Expires In Hours (0=never)",
                        min_value=0,
                        max_value=24 * 365,
                        value=24,
                        step=1,
                    )
                with ap3:
                    approval_actor = st.text_input("Approved By", value=user.username)
                approval_notes = st.text_area("Approval Notes", value="", height=90)
                create_approval_submit = st.form_submit_button("Create Approval")
            if create_approval_submit:
                try:
                    selected_rule = rule_opt_map[selected_approval_rule_key]
                    expires_at = (
                        utcnow_naive() + timedelta(hours=int(expires_in_hours))
                        if int(expires_in_hours) > 0
                        else None
                    )
                    repo.create_integration_automation_approval(
                        environment=settings.app_env,
                        rule_id=int(selected_rule.id),
                        queue_job_id=int(queue_job_id_input) if int(queue_job_id_input) > 0 else None,
                        notes=approval_notes.strip(),
                        approved_by=approval_actor.strip() or user.username,
                        approved_at=utcnow_naive(),
                        expires_at=expires_at,
                        actor=user.username,
                    )
                    st.success("Automation approval created.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to create automation approval: {exc}")
        else:
            st.caption("No active rules requiring approval.")

        approvals = repo.list_integration_automation_approvals(
            environment=settings.app_env,
            active_only=False,
            limit=500,
        )
        if approvals:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "id": a.id,
                            "rule_id": a.rule_id,
                            "queue_job_id": a.queue_job_id,
                            "status": a.status,
                            "is_active": a.is_active,
                            "approved_by": a.approved_by,
                            "approved_at": a.approved_at.isoformat() if a.approved_at else "",
                            "expires_at": a.expires_at.isoformat() if a.expires_at else "",
                            "notes": a.notes,
                        }
                        for a in approvals
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )
            active_approval_map = {
                f"#{a.id} | rule={a.rule_id} | job={a.queue_job_id or 'any'} | {a.status}": a
                for a in approvals
                if bool(a.is_active) and str(a.status or "").strip().lower() == "approved"
            }
            if active_approval_map:
                revoke_key = st.selectbox(
                    "Revoke Active Approval",
                    options=list(active_approval_map.keys()),
                    key="admin_revoke_automation_approval_key",
                )
                if st.button("Revoke Approval", key="admin_revoke_automation_approval_btn"):
                    try:
                        repo.revoke_integration_automation_approval(
                            approval_id=int(active_approval_map[revoke_key].id),
                            actor=user.username,
                        )
                        st.success("Approval revoked.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Unable to revoke approval: {exc}")
        else:
            st.caption("No automation approvals yet.")

        st.markdown("#### Recent Automation Engine Events")
        automation_audit_rows = repo.db.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "integration_event",
                AuditLog.created_at >= (utcnow_naive() - timedelta(days=14)),
            )
            .order_by(AuditLog.created_at.desc())
            .limit(500)
        ).all()
        automation_events: list[dict[str, Any]] = []
        for row in automation_audit_rows:
            try:
                payload = json.loads(row.changes_json or "{}")
            except Exception:
                payload = {}
            after = payload.get("after") if isinstance(payload, dict) else {}
            if not isinstance(after, dict):
                after = {}
            integration_name = str(after.get("integration") or "").strip().lower()
            if integration_name != "integration_automation":
                continue
            details = after.get("details") if isinstance(after.get("details"), dict) else {}
            automation_events.append(
                {
                    "created_at": row.created_at.isoformat() if row.created_at else "",
                    "actor": row.actor,
                    "action": str(after.get("action") or row.action or ""),
                    "status": str(after.get("status") or ""),
                    "job_id": details.get("job_id"),
                    "integration": details.get("integration"),
                    "action_name": details.get("action_name"),
                    "trigger_status": details.get("trigger_status"),
                    "dry_run": details.get("dry_run"),
                    "blocked": details.get("blocked"),
                    "matched_rules": len(details.get("matched_rule_ids") or []),
                    "applied_rules": len(details.get("applied_rule_ids") or []),
                    "approval_gated_rules": len(details.get("approval_gated_rule_ids") or []),
                    "blocked_reason": str(details.get("blocked_reason") or "")[:160],
                    "matched_rule_ids": details.get("matched_rule_ids") or [],
                    "approval_gated_rule_ids": details.get("approval_gated_rule_ids") or [],
                    "details_raw": details,
                }
            )
        if automation_events:
            st.dataframe(pd.DataFrame(automation_events), use_container_width=True, hide_index=True)
        else:
            st.caption("No automation engine events in last 14 days.")

        triage_candidates = [
            row
            for row in automation_events
            if bool(row.get("blocked"))
            or int(row.get("approval_gated_rules") or 0) > 0
            or str(row.get("status") or "").strip().lower() == "failed"
        ]
        st.markdown("#### Automation Failure Triage")
        if not triage_candidates:
            st.caption("No blocked/approval-gated automation events to triage.")
        else:
            triage_map = {
                (
                    f"{row.get('created_at') or ''} | job={row.get('job_id') or 'n/a'} | "
                    f"blocked={row.get('blocked')} | gated={row.get('approval_gated_rules')}"
                ): row
                for row in triage_candidates
            }
            selected_triage_key = st.selectbox(
                "Select Automation Event",
                options=list(triage_map.keys()),
                key="admin_automation_triage_event_key",
            )
            selected_triage = triage_map[selected_triage_key]
            st.json(selected_triage.get("details_raw") or {})
            with st.expander("Replay Rule Simulation (Read-Only)", expanded=False):
                st.caption(
                    "Re-evaluate this queue job against current automation rules to compare with historical event outcome."
                )
                sim_job_id = int(selected_triage.get("job_id") or 0)
                sim_state_key = "admin_automation_triage_last_simulation"
                sim_include_inactive = st.checkbox(
                    "Include inactive rules in simulation",
                    value=False,
                    key="admin_automation_triage_sim_include_inactive",
                )
                if st.button("Run Replay Simulation", key="admin_automation_triage_simulate_btn"):
                    try:
                        if sim_job_id <= 0:
                            st.error("Selected event does not contain a queue job id.")
                        else:
                            simulation = simulate_rule_evaluation_for_job(
                                repo,
                                environment=settings.app_env,
                                job_id=sim_job_id,
                                trigger_status=str(selected_triage.get("trigger_status") or ""),
                                include_inactive=bool(sim_include_inactive),
                            )
                            st.session_state[sim_state_key] = simulation
                    except Exception as exc:
                        st.error(f"Unable to run replay simulation: {exc}")
                simulation = st.session_state.get(sim_state_key)
                simulation_for_selected = (
                    isinstance(simulation, dict) and int(simulation.get("job_id") or 0) == sim_job_id
                )
                if simulation_for_selected:
                    s1, s2, s3 = st.columns(3)
                    s1.metric("Rules Considered", int(simulation.get("rules_considered") or 0))
                    s2.metric("Matched Now", int(simulation.get("matched_rules") or 0))
                    s3.metric("Would Apply Now", int(simulation.get("would_apply_rules") or 0))
                    st.caption(
                        f"Approval-gated now: {int(simulation.get('approval_gated_rules') or 0)}"
                    )
                    sim_rows = simulation.get("rows") or []
                    if sim_rows:
                        st.dataframe(pd.DataFrame(sim_rows), use_container_width=True, hide_index=True)
                    else:
                        st.caption("No rules considered for this job context.")

                    blocked_now = any(
                        bool(row.get("would_apply")) and str(row.get("effect_type") or "") == "block_execute"
                        for row in sim_rows
                    )
                    historical = {
                        "matched_rules": int(selected_triage.get("matched_rules") or 0),
                        "applied_rules": int(selected_triage.get("applied_rules") or 0),
                        "approval_gated_rules": int(selected_triage.get("approval_gated_rules") or 0),
                        "blocked": bool(selected_triage.get("blocked")),
                    }
                    replay_now = {
                        "matched_rules": int(simulation.get("matched_rules") or 0),
                        "applied_rules": int(simulation.get("would_apply_rules") or 0),
                        "approval_gated_rules": int(simulation.get("approval_gated_rules") or 0),
                        "blocked": bool(blocked_now),
                    }
                    drift_rows: list[dict[str, Any]] = []
                    for metric in ("matched_rules", "applied_rules", "approval_gated_rules"):
                        before = int(historical.get(metric) or 0)
                        now = int(replay_now.get(metric) or 0)
                        drift_rows.append(
                            {
                                "metric": metric,
                                "historical": before,
                                "replay_now": now,
                                "delta": now - before,
                            }
                        )
                    drift_rows.append(
                        {
                            "metric": "blocked",
                            "historical": historical["blocked"],
                            "replay_now": replay_now["blocked"],
                            "delta": "changed" if historical["blocked"] != replay_now["blocked"] else "same",
                        }
                    )
                    st.markdown("##### Drift Check")
                    st.dataframe(pd.DataFrame(drift_rows), use_container_width=True, hide_index=True)
                    drift_detected = any(
                        (
                            (row.get("metric") == "blocked" and str(row.get("delta")) == "changed")
                            or (
                                row.get("metric") != "blocked"
                                and int(row.get("delta") or 0) != 0
                            )
                        )
                        for row in drift_rows
                    )
                    if drift_detected:
                        st.warning(
                            "Replay differs from historical outcome. Rule set, approvals, or queue context likely changed."
                        )
                    else:
                        st.success("Replay matches historical outcome for key automation metrics.")
                    if st.button("Log Drift Event", key="admin_automation_triage_log_drift_btn"):
                        try:
                            repo.log_integration_event(
                                actor=user.username,
                                integration="integration_automation",
                                action="drift_detected" if drift_detected else "drift_clear",
                                status="warning" if drift_detected else "success",
                                details={
                                    "job_id": int(sim_job_id),
                                    "trigger_status": str(selected_triage.get("trigger_status") or ""),
                                    "historical": historical,
                                    "replay_now": replay_now,
                                    "drift_detected": bool(drift_detected),
                                },
                            )
                            st.success("Drift event logged.")
                        except Exception as exc:
                            st.error(f"Unable to log drift event: {exc}")

                    sim_approval_hours = st.number_input(
                        "Simulation Approval TTL Hours",
                        min_value=0,
                        max_value=24 * 365,
                        value=24,
                        step=1,
                        key="admin_automation_triage_sim_approval_hours",
                    )
                    if st.button("Approve Gated From Simulation", key="admin_automation_triage_sim_approve_btn"):
                        try:
                            created_count = 0
                            for sim_row in sim_rows:
                                if not bool(sim_row.get("approval_gated")):
                                    continue
                                try:
                                    rule_id = int(sim_row.get("rule_id") or 0)
                                except Exception:
                                    continue
                                if rule_id <= 0:
                                    continue
                                if repo.has_active_integration_automation_approval(
                                    environment=settings.app_env,
                                    rule_id=rule_id,
                                    queue_job_id=sim_job_id if sim_job_id > 0 else None,
                                    as_of=utcnow_naive(),
                                ):
                                    continue
                                expires_at = (
                                    utcnow_naive() + timedelta(hours=int(sim_approval_hours))
                                    if int(sim_approval_hours) > 0
                                    else None
                                )
                                repo.create_integration_automation_approval(
                                    environment=settings.app_env,
                                    rule_id=rule_id,
                                    queue_job_id=sim_job_id if sim_job_id > 0 else None,
                                    notes="Created from replay simulation quick action.",
                                    approved_by=user.username,
                                    approved_at=utcnow_naive(),
                                    expires_at=expires_at,
                                    actor=user.username,
                                )
                                created_count += 1
                            st.success(f"Created {created_count} approval record(s) from simulation.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Unable to create approvals from simulation: {exc}")
            t1, t2, t3 = st.columns(3)
            with t1:
                approval_hours = st.number_input(
                    "Approval TTL Hours",
                    min_value=0,
                    max_value=24 * 365,
                    value=24,
                    step=1,
                    key="admin_automation_triage_approval_hours",
                )
                if st.button("Approve Gated Rules", key="admin_automation_triage_approve_btn"):
                    try:
                        created_count = 0
                        queue_job_id = selected_triage.get("job_id")
                        for rule_id_raw in selected_triage.get("approval_gated_rule_ids") or []:
                            try:
                                rule_id = int(rule_id_raw)
                            except Exception:
                                continue
                            if repo.has_active_integration_automation_approval(
                                environment=settings.app_env,
                                rule_id=rule_id,
                                queue_job_id=int(queue_job_id) if queue_job_id else None,
                                as_of=utcnow_naive(),
                            ):
                                continue
                            expires_at = (
                                utcnow_naive() + timedelta(hours=int(approval_hours))
                                if int(approval_hours) > 0
                                else None
                            )
                            repo.create_integration_automation_approval(
                                environment=settings.app_env,
                                rule_id=rule_id,
                                queue_job_id=int(queue_job_id) if queue_job_id else None,
                                notes="Created from automation triage quick action.",
                                approved_by=user.username,
                                approved_at=utcnow_naive(),
                                expires_at=expires_at,
                                actor=user.username,
                            )
                            created_count += 1
                        st.success(f"Created {created_count} approval record(s).")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Unable to approve gated rules: {exc}")
            with t2:
                if st.button("Retry Job Now", key="admin_automation_triage_retry_btn"):
                    try:
                        job_id = int(selected_triage.get("job_id") or 0)
                        if job_id <= 0:
                            st.error("Selected event does not contain a queue job id.")
                        else:
                            repo.update_integration_queue_job(
                                job_id,
                                {
                                    "status": "queued",
                                    "next_attempt_at": utcnow_naive(),
                                },
                                actor=user.username,
                            )
                            ok, msg = process_integration_queue_job(
                                repo,
                                job_id=job_id,
                                actor=user.username,
                            )
                            if ok:
                                st.success("Retry succeeded.")
                            else:
                                st.warning(f"Retry completed with failure: {msg}")
                            st.rerun()
                    except Exception as exc:
                        st.error(f"Unable to retry selected job: {exc}")
            with t3:
                if st.button("Disable Matched Rules", key="admin_automation_triage_disable_rules_btn"):
                    try:
                        disabled = 0
                        for rule_id_raw in selected_triage.get("matched_rule_ids") or []:
                            try:
                                rule_id = int(rule_id_raw)
                            except Exception:
                                continue
                            repo.update_integration_automation_rule(
                                rule_id,
                                {"is_active": False},
                                actor=user.username,
                            )
                            disabled += 1
                        st.success(f"Disabled {disabled} rule(s).")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Unable to disable matched rules: {exc}")

        st.markdown("#### Automation Hardening Sign-Off (Dev/Prod)")
        st.caption(
            "Record explicit production-hardening acceptance for automation guardrails, approval policy, and runbook readiness."
        )
        with st.form("admin_automation_hardening_signoff_form"):
            ah1, ah2 = st.columns(2)
            with ah1:
                hardening_target_env = st.selectbox(
                    "Sign-Off Environment",
                    options=["dev", "prod"],
                    index=0,
                    key="admin_automation_hardening_signoff_target_env",
                )
                hardening_date = st.date_input(
                    "Sign-Off Date",
                    value=utcnow_naive().date(),
                    key="admin_automation_hardening_signoff_date",
                )
                hardening_owner = st.text_input(
                    "Owner",
                    value=str(user.username or ""),
                    key="admin_automation_hardening_signoff_owner",
                )
            with ah2:
                hardening_status = st.selectbox(
                    "Sign-Off Status",
                    options=["approved", "blocked", "needs_followup"],
                    index=0,
                    key="admin_automation_hardening_signoff_status",
                )
                hardening_evidence_link = st.text_input(
                    "Evidence Link",
                    placeholder="runbook/ticket/review link",
                    key="admin_automation_hardening_signoff_evidence_link",
                )
            hh1, hh2, hh3 = st.columns(3)
            with hh1:
                hardening_guardrails_verified = st.checkbox("Guardrails verified", value=True)
            with hh2:
                hardening_approval_policy_reviewed = st.checkbox("Approval policy reviewed", value=True)
            with hh3:
                hardening_runbook_signed_off = st.checkbox("Runbook signed off", value=True)
            hardening_notes = st.text_area(
                "Hardening Notes",
                placeholder="Summary of checks, residual risks, and actions.",
                key="admin_automation_hardening_signoff_notes",
            )
            save_hardening_signoff = st.form_submit_button("Record Hardening Sign-Off")
        if save_hardening_signoff:
            try:
                repo.record_audit_event(
                    entity_type="integration_automation_hardening_signoff",
                    entity_id=None,
                    action="record",
                    actor=user.username,
                    changes={
                        "target_env": str(hardening_target_env or "").strip().lower(),
                        "signoff_date": str(hardening_date.isoformat()),
                        "owner": str(hardening_owner or "").strip(),
                        "status": str(hardening_status or "").strip().lower(),
                        "evidence_link": str(hardening_evidence_link or "").strip(),
                        "guardrails_verified": bool(hardening_guardrails_verified),
                        "approval_policy_reviewed": bool(hardening_approval_policy_reviewed),
                        "runbook_signed_off": bool(hardening_runbook_signed_off),
                        "notes": str(hardening_notes or "").strip(),
                    },
                )
                st.success("Automation hardening sign-off recorded.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to record automation hardening sign-off: {exc}")

        hardening_logs = repo.db.scalars(
            select(AuditLog)
            .where(AuditLog.entity_type == "integration_automation_hardening_signoff")
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(200)
        ).all()
        hardening_rows: list[dict[str, Any]] = []
        latest_hardening_by_env: dict[str, str] = {}
        for row in hardening_logs:
            payload = _audit_changes(row)
            target_env = str(payload.get("target_env") or "").strip().lower()
            status = str(payload.get("status") or "").strip().lower()
            hardening_rows.append(
                {
                    "recorded_at": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
                    "actor": str(row.actor or ""),
                    "target_env": target_env,
                    "signoff_date": str(payload.get("signoff_date") or ""),
                    "owner": str(payload.get("owner") or ""),
                    "status": status,
                    "guardrails_verified": bool(payload.get("guardrails_verified")),
                    "approval_policy_reviewed": bool(payload.get("approval_policy_reviewed")),
                    "runbook_signed_off": bool(payload.get("runbook_signed_off")),
                    "evidence_link": str(payload.get("evidence_link") or ""),
                    "notes": str(payload.get("notes") or "")[:220],
                }
            )
            if target_env and target_env not in latest_hardening_by_env:
                latest_hardening_by_env[target_env] = status
        if hardening_rows:
            hardening_df = pd.DataFrame(hardening_rows)
            h1, h2 = st.columns(2)
            h1.metric("Automation Hardening Dev", latest_hardening_by_env.get("dev") or "missing")
            h2.metric("Automation Hardening Prod", latest_hardening_by_env.get("prod") or "missing")
            st.dataframe(hardening_df, use_container_width=True, hide_index=True)
            st.download_button(
                "Download Hardening Sign-Off CSV",
                data=hardening_df.to_csv(index=False).encode("utf-8"),
                file_name=f"integration_automation_hardening_signoff_{settings.app_env}_{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv",
                key="admin_automation_hardening_signoff_download_csv_btn",
            )
        else:
            st.caption("No automation hardening sign-off records yet.")

        st.divider()
        st.markdown("#### Slack Notifications")
        p1, p2 = st.columns(2)
        with p1:
            if st.button("Apply Recommended Channel Presets (Current Env)", key="admin_slack_apply_env_presets_btn"):
                try:
                    updated = _apply_slack_channel_presets(
                        repo,
                        actor=user.username,
                        env_name=settings.app_env,
                    )
                    st.success(f"Applied {updated} Slack channel preset key(s) for env `{settings.app_env}`.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to apply Slack channel presets: {exc}")
        with p2:
            st.caption("Presets seed channels like `#gs-<env>-ops`, `#gs-<env>-sync`, `#gs-<env>-error`.")
        with st.form("admin_slack_integration_form"):
            s1, s2 = st.columns(2)
            with s1:
                slack_enabled = st.checkbox(
                    "Enable Slack Notifications",
                    value=_rb("slack_notifications_enabled", False),
                )
                slack_default_channel = st.text_input(
                    "Default Slack Channel",
                    value=_rv("slack_default_channel", ""),
                    help="Example: #ops-alerts",
                )
                slack_notify_sync = st.checkbox(
                    "Notify Sync Failures",
                    value=_rb("slack_notify_sync_failures", True),
                )
                slack_notify_shipping = st.checkbox(
                    "Notify Shipping Exceptions",
                    value=_rb("slack_notify_shipping_exceptions", True),
                )
                slack_notify_daily = st.checkbox(
                    "Send Daily Summary",
                    value=_rb("slack_notify_daily_summary", False),
                )
                slack_notify_queue_failures = st.checkbox(
                    "Notify Google Queue Failures",
                    value=_rb("slack_notify_google_queue_failures", True),
                )
                slack_notify_integration_queue_failures = st.checkbox(
                    "Notify Integration Queue Failures",
                    value=_rb("slack_notify_integration_queue_failures", True),
                )
                slack_notify_parity_decisions = st.checkbox(
                    "Notify Parity Decisions",
                    value=_rb("slack_notify_parity_decisions", False),
                )
                slack_notify_followup_overdue = st.checkbox(
                    "Notify Follow-up Overdue",
                    value=_rb("slack_notify_followup_overdue", False),
                )
                slack_notify_system_health_critical = st.checkbox(
                    "Notify System Health Critical",
                    value=_rb("slack_notify_system_health_critical", False),
                )
                slack_daily_cron = st.text_input(
                    "Daily Summary Cron (UTC)",
                    value=_rv("slack_daily_summary_cron", "0 16 * * *"),
                )
                slack_timeout = st.number_input(
                    "Slack HTTP Timeout Seconds",
                    min_value=3,
                    max_value=60,
                    value=max(3, min(60, int(_rv("slack_http_timeout_seconds", "15") or "15"))),
                    step=1,
                )
                slack_queue_enabled = st.checkbox(
                    "Enable Slack Retry Queue",
                    value=_rb("slack_queue_enabled", True),
                )
                slack_queue_max_retries = st.number_input(
                    "Slack Queue Max Retries",
                    min_value=0,
                    max_value=20,
                    value=max(0, min(20, int(_rv("slack_queue_max_retries", "5") or "5"))),
                    step=1,
                )
                slack_queue_backoff_base = st.number_input(
                    "Slack Queue Backoff Base Seconds",
                    min_value=5,
                    max_value=3600,
                    value=max(5, min(3600, int(_rv("slack_queue_backoff_base_seconds", "60") or "60"))),
                    step=5,
                )
                slack_queue_backoff_max = st.number_input(
                    "Slack Queue Backoff Max Seconds",
                    min_value=5,
                    max_value=86400,
                    value=max(5, min(86400, int(_rv("slack_queue_backoff_max_seconds", "3600") or "3600"))),
                    step=30,
                )
                health_auto_alert_critical_enabled = st.checkbox(
                    "Auto-Alert Health Critical Signals",
                    value=_rb("health_auto_alert_critical_enabled", False),
                    help="When enabled, System Health can auto-send Slack critical alerts on threshold breach.",
                )
                health_auto_alert_cooldown_minutes = st.number_input(
                    "Health Critical Alert Cooldown Minutes",
                    min_value=5,
                    max_value=24 * 60,
                    value=max(5, min(24 * 60, int(_rv("health_auto_alert_cooldown_minutes", "60") or "60"))),
                    step=5,
                )
            with s2:
                slack_bot_token = st.text_input(
                    "Slack Bot Token",
                    value=_rv("slack_bot_token", ""),
                    type="password",
                )
                slack_signing_secret = st.text_input(
                    "Slack Signing Secret",
                    value=_rv("slack_signing_secret", ""),
                    type="password",
                )
                slack_channel_sync_failures = st.text_input(
                    "Channel Override: Sync Failures",
                    value=_rv("slack_channel_sync_failures", ""),
                    help="Optional: route sync failure alerts to this channel.",
                )
                slack_channel_google_queue_failures = st.text_input(
                    "Channel Override: Google Queue Failures",
                    value=_rv("slack_channel_google_queue_failures", ""),
                )
                slack_channel_integration_queue_failures = st.text_input(
                    "Channel Override: Integration Queue Failures",
                    value=_rv("slack_channel_integration_queue_failures", ""),
                )
                slack_channel_parity_decision = st.text_input(
                    "Channel Override: Parity Decisions",
                    value=_rv("slack_channel_parity_decision", ""),
                )
                slack_channel_followup_overdue = st.text_input(
                    "Channel Override: Follow-up Overdue",
                    value=_rv("slack_channel_followup_overdue", ""),
                )
                slack_channel_warning = st.text_input(
                    "Channel Override: Warning Severity",
                    value=_rv("slack_channel_warning", ""),
                )
                slack_channel_error = st.text_input(
                    "Channel Override: Error Severity",
                    value=_rv("slack_channel_error", ""),
                )
                slack_channel_critical = st.text_input(
                    "Channel Override: Critical Severity",
                    value=_rv("slack_channel_critical", ""),
                )
                slack_channel_system_health_critical = st.text_input(
                    "Channel Override: System Health Critical",
                    value=_rv("slack_channel_system_health_critical", ""),
                )
                slack_template_sync_failures = st.text_area(
                    "Template: Sync Failures",
                    value=_rv(
                        "slack_template_sync_failures",
                        (
                            ":warning: *GoldenStackers* sync run `{job_name}` `{status}`\n"
                            "- Env: `{env}`\n"
                            "- Run: `#{run_id}`\n"
                            "- Processed: `{processed}`\n"
                            "- Failed: `{failed}`\n"
                            "- Actor: `{actor}`"
                        ),
                    ),
                    height=140,
                )
                slack_template_google_queue_failures = st.text_area(
                    "Template: Google Queue Failures",
                    value=_rv(
                        "slack_template_google_queue_failures",
                        (
                            ":warning: *GoldenStackers* Google queue job failed permanently\n"
                            "- Env: `{env}`\n"
                            "- Job: `#{job_id}` `{action}`\n"
                            "- Retries: `{retry_count}/{max_retries}`\n"
                            "- Error: `{error}`"
                        ),
                    ),
                    height=130,
                )
                slack_template_integration_queue_failures = st.text_area(
                    "Template: Integration Queue Failures",
                    value=_rv(
                        "slack_template_integration_queue_failures",
                        (
                            ":warning: *GoldenStackers* integration queue job failed permanently\n"
                            "- Env: `{env}`\n"
                            "- Integration: `{integration}`\n"
                            "- Job: `#{job_id}` `{action}`\n"
                            "- Retries: `{retry_count}/{max_retries}`\n"
                            "- Error: `{error}`"
                        ),
                    ),
                    height=130,
                )
                slack_template_parity_decision = st.text_area(
                    "Template: Parity Decision",
                    value=_rv(
                        "slack_template_parity_decision",
                        (
                            ":clipboard: *GoldenStackers* parity release decision `{decision}`\n"
                            "- Env: `{env}`\n"
                            "- Snapshot: `#{snapshot_id}`\n"
                            "- Actor: `{actor}`\n"
                            "- Note: `{note}`"
                        ),
                    ),
                    height=130,
                )
                slack_template_followup_overdue = st.text_area(
                    "Template: Follow-up Overdue",
                    value=_rv(
                        "slack_template_followup_overdue",
                        (
                            ":rotating_light: *GoldenStackers* rollout follow-up overdue\n"
                            "- Env: `{env}`\n"
                            "- Task: `{task_key}`\n"
                            "- Title: `{title}`\n"
                            "- Owner: `{owner}`\n"
                            "- Due: `{due_date}`\n"
                            "- Priority: `{priority}`"
                        ),
                    ),
                    height=130,
                )
                slack_template_system_health_critical = st.text_area(
                    "Template: System Health Critical",
                    value=_rv(
                        "slack_template_system_health_critical",
                        (
                            ":rotating_light: *GoldenStackers* System Health critical signals detected\n"
                            "- Env: `{env}`\n"
                            "- Critical Signals: `{critical_signals}`\n"
                            "- Queue Execute Exceptions: `{queue_execute_exceptions}`\n"
                            "- Terminal Queue Failures: `{terminal_queue_failures}`\n"
                            "- Integration Warnings: `{integration_warnings}`"
                        ),
                    ),
                    height=130,
                )
            save_slack = st.form_submit_button("Save Slack Notification Settings")
        if save_slack:
            try:
                updates = [
                    ("slack_notifications_enabled", "true" if slack_enabled else "false", "bool", "Master toggle for Slack notifications."),
                    ("slack_default_channel", slack_default_channel.strip(), "str", "Default Slack channel for operational notifications (for example #ops-alerts)."),
                    ("slack_notify_sync_failures", "true" if slack_notify_sync else "false", "bool", "Send Slack notifications for sync failures/partial runs."),
                    ("slack_notify_shipping_exceptions", "true" if slack_notify_shipping else "false", "bool", "Send Slack notifications for shipping exceptions."),
                    ("slack_notify_daily_summary", "true" if slack_notify_daily else "false", "bool", "Send one daily Slack operational summary message."),
                    ("slack_notify_google_queue_failures", "true" if slack_notify_queue_failures else "false", "bool", "Send Slack notifications when Google integration queue jobs hit terminal failure."),
                    ("slack_notify_integration_queue_failures", "true" if slack_notify_integration_queue_failures else "false", "bool", "Send Slack notifications when any integration queue job hits terminal failure."),
                    ("slack_notify_parity_decisions", "true" if slack_notify_parity_decisions else "false", "bool", "Send Slack notifications when workspace parity release decisions are recorded."),
                    ("slack_notify_followup_overdue", "true" if slack_notify_followup_overdue else "false", "bool", "Allow sending Slack notifications for overdue workspace rollout follow-up tasks."),
                    ("slack_notify_system_health_critical", "true" if slack_notify_system_health_critical else "false", "bool", "Send Slack notifications when System Health critical-signal thresholds are breached."),
                    ("slack_daily_summary_cron", slack_daily_cron.strip(), "str", "Cron expression for daily summary schedule (UTC)."),
                    ("slack_http_timeout_seconds", str(int(slack_timeout)), "int", "Timeout for Slack API requests."),
                    ("slack_queue_enabled", "true" if slack_queue_enabled else "false", "bool", "Enable/disable Slack delivery retry queue on post failures."),
                    ("slack_queue_max_retries", str(int(slack_queue_max_retries)), "int", "Maximum retry attempts per queued Slack delivery."),
                    ("slack_queue_backoff_base_seconds", str(int(slack_queue_backoff_base)), "int", "Base backoff seconds for Slack retry queue scheduling."),
                    ("slack_queue_backoff_max_seconds", str(int(slack_queue_backoff_max)), "int", "Maximum backoff seconds for Slack retry queue scheduling."),
                    ("health_auto_alert_critical_enabled", "true" if health_auto_alert_critical_enabled else "false", "bool", "Enable automatic System Health critical-signal alert dispatch."),
                    ("health_auto_alert_cooldown_minutes", str(int(health_auto_alert_cooldown_minutes)), "int", "Cooldown minutes before repeating identical System Health critical alerts."),
                    ("slack_bot_token", slack_bot_token.strip(), "str", "Slack Bot OAuth token used for posting notifications."),
                    ("slack_signing_secret", slack_signing_secret.strip(), "str", "Slack signing secret for future interactive/event verification."),
                    ("slack_channel_sync_failures", slack_channel_sync_failures.strip(), "str", "Optional channel override for sync failure alerts."),
                    ("slack_channel_google_queue_failures", slack_channel_google_queue_failures.strip(), "str", "Optional channel override for Google queue failure alerts."),
                    ("slack_channel_integration_queue_failures", slack_channel_integration_queue_failures.strip(), "str", "Optional channel override for integration queue failure alerts."),
                    ("slack_channel_parity_decision", slack_channel_parity_decision.strip(), "str", "Optional channel override for parity release decision alerts."),
                    ("slack_channel_followup_overdue", slack_channel_followup_overdue.strip(), "str", "Optional channel override for overdue rollout follow-up alerts."),
                    ("slack_channel_warning", slack_channel_warning.strip(), "str", "Optional channel override for warning-severity alerts."),
                    ("slack_channel_error", slack_channel_error.strip(), "str", "Optional channel override for error-severity alerts."),
                    ("slack_channel_critical", slack_channel_critical.strip(), "str", "Optional channel override for critical-severity alerts."),
                    ("slack_channel_system_health_critical", slack_channel_system_health_critical.strip(), "str", "Optional channel override for System Health critical alerts."),
                    ("slack_template_sync_failures", slack_template_sync_failures.strip(), "str", "Template for sync failure/partial Slack alerts."),
                    ("slack_template_google_queue_failures", slack_template_google_queue_failures.strip(), "str", "Template for terminal Google queue failure Slack alerts."),
                    ("slack_template_integration_queue_failures", slack_template_integration_queue_failures.strip(), "str", "Template for terminal integration queue failure Slack alerts."),
                    ("slack_template_parity_decision", slack_template_parity_decision.strip(), "str", "Template for workspace parity release decision alerts."),
                    ("slack_template_followup_overdue", slack_template_followup_overdue.strip(), "str", "Template for overdue workspace rollout follow-up alerts."),
                    ("slack_template_system_health_critical", slack_template_system_health_critical.strip(), "str", "Template for System Health critical threshold alerts."),
                ]
                for key, value, value_type, description in updates:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key=key,
                        value=value,
                        value_type=value_type,
                        description=description,
                        is_active=True,
                        actor=user.username,
                    )
                st.success("Slack notification settings saved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to save Slack notification settings: {exc}")

        with st.form("admin_slack_test_send_form"):
            test_channel = st.text_input("Test Channel (optional, default uses slack_default_channel)", value="")
            test_message = st.text_area(
                "Test Message",
                value=f"GoldenStackers Slack test from `{settings.app_env}` at `{utcnow_naive().isoformat()}`.",
                height=100,
            )
            send_test_slack = st.form_submit_button("Send Test Slack Message")
        if send_test_slack:
            try:
                result = send_slack_message(
                    repo,
                    text=test_message.strip(),
                    channel=test_channel.strip(),
                )
                repo.log_integration_event(
                    actor=user.username,
                    integration="slack",
                    action="test_send",
                    status="success",
                    details={
                        "channel": result.get("channel", ""),
                        "ts": result.get("ts", ""),
                        "env": settings.app_env,
                    },
                )
                st.success(f"Slack test sent to `{result.get('channel', '')}`.")
            except Exception as exc:
                try:
                    repo.log_integration_event(
                        actor=user.username,
                        integration="slack",
                        action="test_send",
                        status="failed",
                        details={"error": str(exc), "env": settings.app_env},
                    )
                except Exception:
                    pass
                st.error(f"Slack test send failed: {exc}")

        st.markdown("#### Recent Slack Delivery Events")
        slack_audit_rows = repo.db.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "integration_event",
                AuditLog.created_at >= (utcnow_naive() - timedelta(days=14)),
            )
            .order_by(AuditLog.created_at.desc())
            .limit(500)
        ).all()
        slack_events: list[dict[str, str]] = []
        for row in slack_audit_rows:
            try:
                payload = json.loads(row.changes_json or "{}")
            except Exception:
                payload = {}
            after = payload.get("after") if isinstance(payload, dict) else {}
            if not isinstance(after, dict):
                after = {}
            integration_name = str(after.get("integration") or "").strip().lower()
            if not integration_name.startswith("slack"):
                continue
            details = after.get("details") if isinstance(after.get("details"), dict) else {}
            slack_events.append(
                {
                    "created_at": row.created_at.isoformat() if row.created_at else "",
                    "actor": row.actor,
                    "integration": integration_name,
                    "action": str(after.get("action") or row.action or ""),
                    "status": str(after.get("status") or ""),
                    "channel": str(details.get("channel") or ""),
                    "ts": str(details.get("ts") or ""),
                    "error": str(details.get("error") or "")[:220],
                }
            )
        if slack_events:
            st.dataframe(pd.DataFrame(slack_events), use_container_width=True)
        else:
            st.info("No Slack integration events in last 14 days.")

        st.markdown("#### Slack Retry Queue")
        try:
            slack_queue_rows = repo.list_integration_queue_jobs(
                environment=settings.app_env,
                integration="slack",
                statuses={"queued", "running", "failed", "success"},
                limit=500,
            )
        except Exception as exc:
            st.error(
                "Integration queue table is not available yet. "
                "Run database migrations first (`docker compose run --rm migrate`)."
            )
            st.caption(f"Details: {exc}")
            slack_queue_rows = []

        if slack_queue_rows:
            slack_queue_df = pd.DataFrame(
                [
                    {
                        "id": row.id,
                        "action": row.action,
                        "status": row.status,
                        "retry_count": int(row.retry_count or 0),
                        "max_retries": int(row.max_retries or 0),
                        "next_attempt_at": row.next_attempt_at.isoformat() if row.next_attempt_at else "",
                        "last_attempt_at": row.last_attempt_at.isoformat() if row.last_attempt_at else "",
                        "completed_at": row.completed_at.isoformat() if row.completed_at else "",
                        "requested_by": row.requested_by,
                        "last_error": str(row.last_error or "")[:250],
                        "created_at": row.created_at.isoformat() if row.created_at else "",
                    }
                    for row in slack_queue_rows
                ]
            )
            st.dataframe(slack_queue_df, use_container_width=True)
            sq1, sq2 = st.columns(2)
            with sq1:
                if st.button("Run Due Slack Queue Jobs Now", key="admin_slack_queue_run_due_btn"):
                    try:
                        summary = process_due_integration_queue_jobs(
                            repo,
                            integration="slack",
                            actor=user.username,
                            limit=20,
                        )
                        st.success(
                            f"Processed {summary['processed']} Slack queue job(s): "
                            f"{summary['success']} success, {summary['queued']} re-queued, {summary['failed']} failed."
                        )
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Unable to run due Slack queue jobs: {exc}")
            with sq2:
                slack_failed_only = [row for row in slack_queue_rows if str(row.status or "").lower() == "failed"]
                if slack_failed_only:
                    row_map = {
                        f"#{row.id} | {row.action} | retry {row.retry_count}/{row.max_retries}": row
                        for row in slack_failed_only
                    }
                    selected_key = st.selectbox(
                        "Retry Failed Slack Job",
                        options=list(row_map.keys()),
                        key="admin_slack_queue_retry_failed_select",
                    )
                    if st.button("Retry Selected Slack Job Now", key="admin_slack_queue_retry_failed_btn"):
                        selected_row = row_map[selected_key]
                        try:
                            repo.update_integration_queue_job(
                                int(selected_row.id),
                                {"status": "queued", "next_attempt_at": utcnow_naive()},
                                actor=user.username,
                            )
                            ok, msg = process_integration_queue_job(
                                repo,
                                job_id=int(selected_row.id),
                                actor=user.username,
                            )
                            if ok:
                                st.success(f"Retry succeeded for Slack queue job #{selected_row.id}. {msg}")
                            else:
                                st.warning(f"Retry did not complete successfully for Slack queue job #{selected_row.id}. {msg}")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Unable to retry selected Slack queue job: {exc}")
                else:
                    st.info("No failed Slack queue jobs available.")
        else:
            st.info("No Slack queue jobs found for this environment.")

        st.divider()
        st.markdown("#### Google Retry Queue")
        st.caption("Durable retry queue for failed Google Gmail/Calendar/Drive actions with backoff scheduling.")
        try:
            queue_rows = repo.list_integration_queue_jobs(
                environment=settings.app_env,
                integration="google",
                statuses={"queued", "running", "failed", "success"},
                limit=500,
            )
        except Exception as exc:
            st.error(
                "Integration queue table is not available yet. "
                "Run database migrations first (`docker compose run --rm migrate`)."
            )
            st.caption(f"Details: {exc}")
            queue_rows = []

        if queue_rows:
            queue_df = pd.DataFrame(
                [
                    {
                        "id": row.id,
                        "integration": row.integration,
                        "action": row.action,
                        "status": row.status,
                        "retry_count": int(row.retry_count or 0),
                        "max_retries": int(row.max_retries or 0),
                        "next_attempt_at": row.next_attempt_at.isoformat() if row.next_attempt_at else "",
                        "last_attempt_at": row.last_attempt_at.isoformat() if row.last_attempt_at else "",
                        "completed_at": row.completed_at.isoformat() if row.completed_at else "",
                        "requested_by": row.requested_by,
                        "last_error": str(row.last_error or "")[:250],
                        "created_at": row.created_at.isoformat() if row.created_at else "",
                    }
                    for row in queue_rows
                ]
            )
            st.dataframe(queue_df, use_container_width=True)
            q1, q2 = st.columns(2)
            with q1:
                if st.button("Run Due Queue Jobs Now", key="admin_google_queue_run_due_btn"):
                    try:
                        summary = process_due_google_queue_jobs(repo, actor=user.username, limit=20)
                        st.success(
                            f"Processed {summary['processed']} due job(s): "
                            f"{summary['success']} success, {summary['queued']} re-queued, {summary['failed']} failed."
                        )
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Unable to run due queue jobs: {exc}")
            with q2:
                failed_only = [row for row in queue_rows if str(row.status or "").lower() == "failed"]
                if failed_only:
                    row_map = {f"#{row.id} | {row.action} | retry {row.retry_count}/{row.max_retries}": row for row in failed_only}
                    selected_retry_key = st.selectbox(
                        "Retry Failed Job",
                        options=list(row_map.keys()),
                        key="admin_google_queue_retry_failed_select",
                    )
                    if st.button("Retry Selected Failed Job Now", key="admin_google_queue_retry_failed_btn"):
                        selected_row = row_map[selected_retry_key]
                        try:
                            repo.update_integration_queue_job(
                                int(selected_row.id),
                                {
                                    "status": "queued",
                                    "next_attempt_at": utcnow_naive(),
                                },
                                actor=user.username,
                            )
                            ok, msg = process_integration_queue_job(
                                repo,
                                job_id=int(selected_row.id),
                                actor=user.username,
                            )
                            if ok:
                                st.success(f"Retry succeeded for job #{selected_row.id}. {msg}")
                            else:
                                st.warning(f"Retry did not complete successfully for job #{selected_row.id}. {msg}")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Unable to retry selected job: {exc}")
                else:
                    st.info("No failed queue jobs available.")
        else:
            st.info("No Google queue jobs found for this environment.")

    with tab_comp_config:
        _render_listing_review_policy_editor(repo, user)
        st.divider()
        _render_comp_dealer_domains_editor(repo, user)
        st.divider()
        _render_comp_photo_retry_telemetry(repo, user)
        st.divider()
        _render_coin_paid_source_editor(repo, user)

    with tab_saved_filters:
        st.markdown("### Saved Filter Governance")
        st.caption(
            "Transfer ownership of team-shared filters and delete shared filters when needed."
        )
        blocker_scopes = {"listings_blocker_followups", "operations_home_blocker_followups"}
        try:
            all_filter_rows = repo.db.scalars(
                select(SavedFilterProfile)
                .where(SavedFilterProfile.environment == settings.app_env)
                .order_by(
                    SavedFilterProfile.scope.asc(),
                    SavedFilterProfile.is_shared.desc(),
                    SavedFilterProfile.name.asc(),
                )
            ).all()
        except Exception as exc:
            st.error(
                "Saved filter table is not available yet. Run database migrations first "
                "(`docker compose run --rm migrate`)."
            )
            st.caption(f"Details: {exc}")
            all_filter_rows = []
        scope_values = sorted(
            {
                str(row.scope or "").strip().lower()
                for row in all_filter_rows
                if str(row.scope or "").strip()
            }
        )
        selected_scopes = st.multiselect(
            "Scope Filter",
            options=scope_values,
            default=scope_values,
            key="admin_saved_filters_scope_filter",
        )
        only_blocker_scopes = st.checkbox(
            "Only blocker preset scopes",
            value=False,
            key="admin_saved_filters_only_blocker_scopes",
            help="Focus governance view on `listings_blocker_followups` and `operations_home_blocker_followups`.",
        )
        st.markdown("#### Scope Presets")
        sp1, sp2, sp3, sp4 = st.columns(4)
        with sp1:
            if st.button("All", key="admin_saved_filters_scope_preset_all", use_container_width=True):
                st.session_state["admin_saved_filters_scope_filter"] = list(scope_values)
                st.session_state["admin_saved_filters_only_blocker_scopes"] = False
                st.rerun()
        with sp2:
            if st.button("Blocker Presets", key="admin_saved_filters_scope_preset_blocker", use_container_width=True):
                st.session_state["admin_saved_filters_scope_filter"] = [
                    s for s in scope_values if s in blocker_scopes
                ]
                st.session_state["admin_saved_filters_only_blocker_scopes"] = True
                st.rerun()
        with sp3:
            if st.button("Listings", key="admin_saved_filters_scope_preset_listings", use_container_width=True):
                st.session_state["admin_saved_filters_scope_filter"] = [
                    s for s in scope_values if "listing" in s
                ]
                st.session_state["admin_saved_filters_only_blocker_scopes"] = False
                st.rerun()
        with sp4:
            if st.button("Operations Home", key="admin_saved_filters_scope_preset_operations_home", use_container_width=True):
                st.session_state["admin_saved_filters_scope_filter"] = [
                    s for s in scope_values if "operations_home" in s
                ]
                st.session_state["admin_saved_filters_only_blocker_scopes"] = False
                st.rerun()
        owner_filter_value = st.selectbox(
            "Ownership Filter",
            options=[
                "all",
                "my_owned",
                "shared_owned_by_me",
                "shared_not_owned_by_me",
            ],
            index=0,
            key="admin_saved_filters_owner_filter",
            help="Focus by ownership to speed transfer/delete governance actions.",
        )
        only_default_presets = st.checkbox(
            "Only default presets",
            value=False,
            key="admin_saved_filters_only_default",
            help="Show only saved filters currently marked as default.",
        )
        st.markdown("#### Ownership Presets")
        op1, op2, op3 = st.columns(3)
        with op1:
            if st.button("My Owned", key="admin_saved_filters_owner_preset_my_owned", use_container_width=True):
                st.session_state["admin_saved_filters_owner_filter"] = "my_owned"
                st.rerun()
        with op2:
            if st.button(
                "Shared Owned By Me",
                key="admin_saved_filters_owner_preset_shared_mine",
                use_container_width=True,
            ):
                st.session_state["admin_saved_filters_owner_filter"] = "shared_owned_by_me"
                st.rerun()
        with op3:
            if st.button(
                "Shared Not Owned By Me",
                key="admin_saved_filters_owner_preset_shared_not_mine",
                use_container_width=True,
            ):
                st.session_state["admin_saved_filters_owner_filter"] = "shared_not_owned_by_me"
                st.rerun()
        if st.button(
            "Reset Governance Filters",
            key="admin_saved_filters_reset_filters_btn",
            use_container_width=True,
        ):
            st.session_state["admin_saved_filters_scope_filter"] = list(scope_values)
            st.session_state["admin_saved_filters_only_blocker_scopes"] = False
            st.session_state["admin_saved_filters_owner_filter"] = "all"
            st.session_state["admin_saved_filters_only_default"] = False
            st.rerun()
        filtered_rows = [
            row
            for row in all_filter_rows
            if (
                (not selected_scopes or str(row.scope or "").strip().lower() in {str(s).strip().lower() for s in selected_scopes})
                and (not only_blocker_scopes or str(row.scope or "").strip().lower() in blocker_scopes)
                and (
                    owner_filter_value == "all"
                    or (
                        owner_filter_value == "my_owned"
                        and str(row.username or "").strip() == str(user.username or "").strip()
                    )
                    or (
                        owner_filter_value == "shared_owned_by_me"
                        and bool(row.is_shared)
                        and str(row.username or "").strip() == str(user.username or "").strip()
                    )
                    or (
                        owner_filter_value == "shared_not_owned_by_me"
                        and bool(row.is_shared)
                        and str(row.username or "").strip() != str(user.username or "").strip()
                    )
                )
                and (not only_default_presets or bool(row.is_default))
            )
        ]
        active_scope_count = int(len(selected_scopes or []))
        owner_mode_label = {
            "all": "All",
            "my_owned": "My Owned",
            "shared_owned_by_me": "Shared By Me",
            "shared_not_owned_by_me": "Shared Not Mine",
        }.get(str(owner_filter_value or "all"), str(owner_filter_value or "all"))
        st.markdown("#### Governance Filter State")
        g1, g2, g3, g4 = st.columns(4)
        g1.metric("Active Scope Count", active_scope_count)
        g2.metric("Owner Mode", owner_mode_label)
        g3.metric("Blocker Focus", "On" if bool(only_blocker_scopes) else "Off")
        g4.metric("Defaults Only", "On" if bool(only_default_presets) else "Off")
        g5, g6 = st.columns(2)
        g5.metric("Visible Rows", int(len(filtered_rows)))
        g6.metric("Visible Defaults", int(len([row for row in filtered_rows if bool(row.is_default)])))
        shared_rows = [row for row in filtered_rows if bool(row.is_shared)]

        if filtered_rows:
            blocker_rows = [
                row for row in filtered_rows if str(row.scope or "").strip().lower() in blocker_scopes
            ]
            sfm1, sfm2, sfm3 = st.columns(3)
            sfm1.metric("Visible Saved Filters", int(len(filtered_rows)))
            sfm2.metric("Visible Shared Filters", int(len(shared_rows)))
            sfm3.metric("Visible Blocker Presets", int(len(blocker_rows)))
            filtered_rows_export = [
                {
                    "id": row.id,
                    "environment": row.environment,
                    "scope": row.scope,
                    "name": row.name,
                    "owner": row.username,
                    "is_shared": bool(row.is_shared),
                    "is_default": bool(row.is_default),
                    "is_active": bool(row.is_active),
                    "updated_at": row.updated_at.isoformat() if row.updated_at else "",
                }
                for row in filtered_rows
            ]
            st.dataframe(
                pd.DataFrame(
                    filtered_rows_export
                ),
                use_container_width=True,
            )
            st.download_button(
                "Download Filtered Saved Filters CSV",
                data=pd.DataFrame(filtered_rows_export).to_csv(index=False).encode("utf-8"),
                file_name=f"admin_saved_filters_{settings.app_env}_{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv",
                key="admin_saved_filters_filtered_csv_btn",
            )
            st.markdown("#### Filtered Governance Breakdown")
            b1, b2 = st.columns(2)
            filtered_df = pd.DataFrame(filtered_rows_export)
            with b1:
                owner_summary = (
                    filtered_df.groupby(["owner"], dropna=False)
                    .size()
                    .reset_index(name="count")
                    .sort_values(["count", "owner"], ascending=[False, True])
                )
                st.caption("By Owner")
                st.dataframe(owner_summary, use_container_width=True, hide_index=True)
            with b2:
                scope_summary = (
                    filtered_df.groupby(["scope"], dropna=False)
                    .size()
                    .reset_index(name="count")
                    .sort_values(["count", "scope"], ascending=[False, True])
                )
                st.caption("By Scope")
                st.dataframe(scope_summary, use_container_width=True, hide_index=True)
        else:
            st.info("No saved filters found for the current scope filter.")

        if not shared_rows:
            st.info("No team-shared filters available for transfer/delete actions.")
        else:
            shared_map = {
                f"#{row.id} | {row.scope} | {row.name} | owner={row.username}": row
                for row in shared_rows
            }
            selected_key = st.selectbox(
                "Select Shared Filter",
                options=list(shared_map.keys()),
                key="admin_saved_filter_shared_select",
            )
            selected_row = shared_map[selected_key]

            st.markdown("### Transfer Shared Filter Ownership")
            user_options = sorted({u.username for u in users if u.is_active})
            if not user_options:
                st.warning("No active users available as transfer targets.")
            else:
                target_owner = st.selectbox(
                    "New Owner",
                    options=user_options,
                    index=user_options.index(selected_row.username)
                    if selected_row.username in user_options
                    else 0,
                    key="admin_saved_filter_new_owner",
                )
                if st.button("Transfer Ownership", key="admin_saved_filter_transfer_btn"):
                    try:
                        repo.transfer_shared_filter_ownership(
                            profile_id=selected_row.id,
                            new_username=target_owner,
                            actor=user.username,
                        )
                        st.success(f"Transferred filter #{selected_row.id} to `{target_owner}`.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Ownership transfer failed: {exc}")

            st.markdown("### Delete Shared Filter")
            with st.form("admin_delete_shared_filter_form"):
                confirm_delete = st.checkbox("I understand this permanently deletes the shared filter.")
                phrase = st.text_input("Type DELETE to confirm")
                delete_submit = st.form_submit_button("Delete Selected Shared Filter")
            if delete_submit:
                if not confirm_delete or phrase.strip() != "DELETE":
                    st.error("Confirm deletion and type `DELETE` exactly.")
                else:
                    try:
                        repo.delete_shared_filter_profile_by_id(
                            profile_id=selected_row.id,
                            actor=user.username,
                        )
                        st.success(f"Deleted shared filter #{selected_row.id}.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Delete failed: {exc}")

    with tab_sync_jobs:
        st.markdown("### Sync Job Controls")
        st.caption(
            "Sync jobs now resolve from Runtime Settings (DB) first, with env fallback. "
            "Runtime changes apply immediately; env changes apply on restart."
        )

        env_map = {
            "ebay_orders_pull_import": "SYNC_JOB_EBAY_ORDERS_PULL_IMPORT_ENABLED",
            "ebay_shipping_tracking_push": "SYNC_JOB_EBAY_SHIPPING_TRACKING_PUSH_ENABLED",
            "quickbooks_export": "SYNC_JOB_QUICKBOOKS_EXPORT_ENABLED",
            "shopify_orders_pull": "SYNC_JOB_SHOPIFY_ORDERS_PULL_ENABLED",
        }
        job_rows = []
        for row in sync_job_catalog(repo):
            job_name = str(row.get("job_name") or "")
            job_rows.append(
                {
                    "job_name": job_name,
                    "provider": row.get("provider"),
                    "direction": row.get("direction"),
                    "implemented": bool(row.get("implemented")),
                    "enabled": bool(is_sync_job_enabled(job_name, repo=repo)),
                    "env_var": env_map.get(job_name, ""),
                }
            )
        st.dataframe(pd.DataFrame(job_rows), use_container_width=True)

        st.markdown("### Desired Toggle Values")
        st.caption("Save Runtime Settings for live behavior and/or generate `.env` snippet for deployment fallback.")
        with st.form("admin_sync_jobs_env_snippet_form"):
            desired_orders_pull = st.checkbox(
                "Enable eBay orders pull/import",
                value=bool(is_sync_job_enabled("ebay_orders_pull_import", repo=repo)),
            )
            desired_tracking_push = st.checkbox(
                "Enable eBay shipping tracking push",
                value=bool(is_sync_job_enabled("ebay_shipping_tracking_push", repo=repo)),
            )
            desired_quickbooks_export = st.checkbox(
                "Enable QuickBooks export (future job)",
                value=bool(is_sync_job_enabled("quickbooks_export", repo=repo)),
            )
            desired_shopify_pull = st.checkbox(
                "Enable Shopify orders pull (future job)",
                value=bool(is_sync_job_enabled("shopify_orders_pull", repo=repo)),
            )
            st.markdown("#### Governance Snapshot Scheduler")
            desired_governance_snapshot_runner = st.checkbox(
                "Enable scheduled governance snapshots (sync worker)",
                value=get_runtime_bool(repo, "governance_snapshot_runner_enabled", False),
                help="When enabled, sync worker records governance snapshot audit events on an interval.",
            )
            desired_governance_snapshot_interval_hours = st.number_input(
                "Snapshot Interval Hours",
                min_value=1,
                max_value=24 * 30,
                value=max(1, min(24 * 30, get_runtime_int(repo, "governance_snapshot_interval_hours", 24))),
                step=1,
            )
            desired_governance_snapshot_lookback_days = st.number_input(
                "Snapshot Lookback Days",
                min_value=1,
                max_value=365,
                value=max(1, min(365, get_runtime_int(repo, "governance_snapshot_lookback_days", 30))),
                step=1,
            )
            desired_governance_snapshot_max_rows = st.number_input(
                "Snapshot Max Rows Per Scope",
                min_value=100,
                max_value=10000,
                value=max(100, min(10000, get_runtime_int(repo, "governance_snapshot_max_rows_per_scope", 2000))),
                step=100,
            )
            save_runtime_toggles = st.form_submit_button("Save Runtime Toggles")
            generate_snippet = st.form_submit_button("Generate Env Snippet")

        if save_runtime_toggles:
            try:
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="sync_job_ebay_orders_pull_import_enabled",
                    value="true" if desired_orders_pull else "false",
                    value_type="bool",
                    description="Enable/disable eBay orders pull/import job.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="sync_job_ebay_shipping_tracking_push_enabled",
                    value="true" if desired_tracking_push else "false",
                    value_type="bool",
                    description="Enable/disable eBay shipping tracking push job.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="sync_job_quickbooks_export_enabled",
                    value="true" if desired_quickbooks_export else "false",
                    value_type="bool",
                    description="Enable/disable QuickBooks export job scaffold.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="sync_job_shopify_orders_pull_enabled",
                    value="true" if desired_shopify_pull else "false",
                    value_type="bool",
                    description="Enable/disable Shopify pull job scaffold.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="governance_snapshot_runner_enabled",
                    value="true" if desired_governance_snapshot_runner else "false",
                    value_type="bool",
                    description="Enable/disable scheduled governance snapshot creation in sync runner.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="governance_snapshot_interval_hours",
                    value=str(int(desired_governance_snapshot_interval_hours)),
                    value_type="int",
                    description="Minimum hours between sync-runner governance snapshot events.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="governance_snapshot_lookback_days",
                    value=str(int(desired_governance_snapshot_lookback_days)),
                    value_type="int",
                    description="Lookback window for scheduled governance snapshot event counts.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="governance_snapshot_max_rows_per_scope",
                    value=str(int(desired_governance_snapshot_max_rows)),
                    value_type="int",
                    description="Max rows per governance scope sampled in scheduled snapshots.",
                    is_active=True,
                    actor=user.username,
                )
                st.success("Runtime sync toggles saved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to save runtime sync toggles: {exc}")

        if generate_snippet:
            snippet = (
                f"SYNC_JOB_EBAY_ORDERS_PULL_IMPORT_ENABLED={'true' if desired_orders_pull else 'false'}\n"
                f"SYNC_JOB_EBAY_SHIPPING_TRACKING_PUSH_ENABLED={'true' if desired_tracking_push else 'false'}\n"
                f"SYNC_JOB_QUICKBOOKS_EXPORT_ENABLED={'true' if desired_quickbooks_export else 'false'}\n"
                f"SYNC_JOB_SHOPIFY_ORDERS_PULL_ENABLED={'true' if desired_shopify_pull else 'false'}\n"
                "# Governance snapshot scheduler is runtime-only (DB-backed):\n"
                f"# governance_snapshot_runner_enabled={'true' if desired_governance_snapshot_runner else 'false'}\n"
                f"# governance_snapshot_interval_hours={int(desired_governance_snapshot_interval_hours)}\n"
                f"# governance_snapshot_lookback_days={int(desired_governance_snapshot_lookback_days)}\n"
                f"# governance_snapshot_max_rows_per_scope={int(desired_governance_snapshot_max_rows)}"
            )
            st.code(snippet, language="bash")

        st.markdown("### Governance Snapshot Scheduler Status")
        worker_snapshots = repo.db.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "governance_export",
                AuditLog.action == "snapshot",
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(200)
        ).all()
        latest_worker_snapshot = None
        latest_manual_snapshot = None
        for row in worker_snapshots:
            payload = _audit_changes(row)
            source = str(payload.get("source") or "").strip().lower()
            if source == "sync_runner":
                latest_worker_snapshot = row
            elif source in {"admin_sync_jobs", "admin_governance_exports"} and latest_manual_snapshot is None:
                latest_manual_snapshot = row
            if latest_worker_snapshot is not None and latest_manual_snapshot is not None:
                break
        now_ts = utcnow_naive()
        interval_hours = int(desired_governance_snapshot_interval_hours)
        next_due_at = None
        if latest_worker_snapshot is not None and latest_worker_snapshot.created_at is not None:
            next_due_at = latest_worker_snapshot.created_at + timedelta(hours=interval_hours)
        is_overdue = bool(next_due_at is not None and next_due_at <= now_ts)
        scheduler_enabled = bool(desired_governance_snapshot_runner)
        ss1, ss2, ss3, ss4, ss5 = st.columns(5)
        ss1.metric("Scheduler Enabled", "yes" if scheduler_enabled else "no")
        ss2.metric(
            "Last Worker Snapshot",
            latest_worker_snapshot.created_at.isoformat(timespec="seconds")
            if latest_worker_snapshot and latest_worker_snapshot.created_at
            else "none",
        )
        ss3.metric(
            "Last Manual Snapshot",
            latest_manual_snapshot.created_at.isoformat(timespec="seconds")
            if latest_manual_snapshot and latest_manual_snapshot.created_at
            else "none",
        )
        ss4.metric(
            "Next Due",
            next_due_at.isoformat(timespec="seconds") if next_due_at is not None else "on first run",
        )
        ss5.metric("Due Status", "overdue" if is_overdue else ("scheduled" if next_due_at is not None else "pending"))
        if latest_worker_snapshot is not None and latest_manual_snapshot is not None and latest_worker_snapshot.created_at and latest_manual_snapshot.created_at:
            lag_seconds = int((latest_worker_snapshot.created_at - latest_manual_snapshot.created_at).total_seconds())
            st.caption(
                "Worker vs manual snapshot recency delta (seconds): "
                f"{lag_seconds:+d} (positive means worker snapshot is newer)."
            )
        if scheduler_enabled and is_overdue:
            st.warning("Governance snapshot scheduler is overdue. Check sync worker health/logs.")
        elif scheduler_enabled:
            st.caption("Governance snapshot scheduler is active and waiting for next due interval.")
        else:
            st.caption("Governance snapshot scheduler is disabled.")
        cutoff_7d = now_ts - timedelta(days=7)
        cutoff_30d = now_ts - timedelta(days=30)
        source_counts_7d: Counter[str] = Counter()
        source_counts_30d: Counter[str] = Counter()
        source_logs = repo.db.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "governance_export",
                AuditLog.action == "snapshot",
                AuditLog.created_at >= cutoff_30d,
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(5000)
        ).all()
        for row in source_logs:
            payload = _audit_changes(row)
            source = str(payload.get("source") or "unknown").strip().lower() or "unknown"
            created_at = row.created_at
            if created_at is None:
                continue
            source_counts_30d[source] += 1
            if created_at >= cutoff_7d:
                source_counts_7d[source] += 1
        source_keys = sorted(set(source_counts_30d.keys()) | set(source_counts_7d.keys()))
        if source_keys:
            source_breakdown_df = pd.DataFrame(
                [
                    {
                        "source": source,
                        "last_7_days": int(source_counts_7d.get(source, 0)),
                        "last_30_days": int(source_counts_30d.get(source, 0)),
                    }
                    for source in source_keys
                ]
            ).sort_values(["last_30_days", "source"], ascending=[False, True])
            st.caption("Snapshot Source Breakdown")
            st.dataframe(source_breakdown_df, use_container_width=True, hide_index=True)
        else:
            st.caption("No governance snapshot source activity in last 30 days.")

        st.markdown("#### Cadence Health")
        worker_7d = int(source_counts_7d.get("sync_runner", 0))
        worker_30d = int(source_counts_30d.get("sync_runner", 0))
        expected_7d = max(1, int(round((7 * 24) / max(1, interval_hours))))
        expected_30d = max(1, int(round((30 * 24) / max(1, interval_hours))))
        completion_7d = float(worker_7d) / float(expected_7d)
        completion_30d = float(worker_30d) / float(expected_30d)
        cadence_ratio = min(completion_7d, completion_30d)
        if not scheduler_enabled:
            cadence_state = "disabled"
        elif cadence_ratio >= 0.9:
            cadence_state = "green"
        elif cadence_ratio >= 0.5:
            cadence_state = "yellow"
        else:
            cadence_state = "red"
        ch1, ch2, ch3, ch4 = st.columns(4)
        ch1.metric("Cadence State", cadence_state.upper())
        ch2.metric("Worker Snapshots 7d", f"{worker_7d}/{expected_7d}")
        ch3.metric("Worker Snapshots 30d", f"{worker_30d}/{expected_30d}")
        ch4.metric("Cadence Ratio", f"{cadence_ratio * 100:.1f}%")
        if cadence_state == "green":
            st.success("Cadence health is GREEN based on expected worker snapshot frequency.")
        elif cadence_state == "yellow":
            st.warning("Cadence health is YELLOW. Worker snapshots are below expected target.")
        elif cadence_state == "red":
            st.error("Cadence health is RED. Worker snapshots are significantly below expected target.")
        else:
            st.caption("Cadence health is DISABLED because scheduler toggle is off.")
        if cadence_state == "red":
            followup_logs = repo.db.scalars(
                select(AuditLog)
                .where(AuditLog.entity_type == "workspace_followup")
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                .limit(2000)
            ).all()
            cadence_created: dict[str, dict[str, Any]] = {}
            cadence_resolved: set[str] = set()
            for row in followup_logs:
                payload = _audit_changes(row)
                task_key = str(payload.get("task_key") or "").strip()
                workflow = str(payload.get("workflow") or "").strip().lower()
                action = str(row.action or "").strip().lower()
                if workflow != "governance_snapshot_cadence" or not task_key:
                    continue
                if action == "create" and task_key not in cadence_created:
                    cadence_created[task_key] = payload
                if action in {"resolve", "closed"}:
                    cadence_resolved.add(task_key)
            open_cadence_tasks = [
                payload for key, payload in cadence_created.items() if key not in cadence_resolved
            ]
            st.markdown("##### Cadence Blocker Follow-up")
            if open_cadence_tasks:
                st.warning(
                    f"Open cadence follow-up tasks detected: {len(open_cadence_tasks)}. "
                    "Resolve existing task(s) before creating another."
                )
            cf1, cf2, cf3 = st.columns(3)
            with cf1:
                cadence_followup_owner = st.text_input(
                    "Cadence Follow-up Owner",
                    value=user.username,
                    key="admin_sync_jobs_cadence_followup_owner",
                )
            with cf2:
                cadence_followup_priority = st.selectbox(
                    "Cadence Follow-up Priority",
                    options=["critical", "high", "medium", "low"],
                    index=1,
                    key="admin_sync_jobs_cadence_followup_priority",
                )
            with cf3:
                cadence_followup_due_days = st.number_input(
                    "Cadence Follow-up Due (days)",
                    min_value=0,
                    max_value=30,
                    value=1,
                    step=1,
                    key="admin_sync_jobs_cadence_followup_due_days",
                )
            if st.button(
                "Create Cadence Follow-up Task",
                key="admin_sync_jobs_create_cadence_followup_btn",
                disabled=bool(open_cadence_tasks),
            ):
                try:
                    task_key = f"gov-cadence-{utcnow_naive().strftime('%Y%m%d%H%M%S')}"
                    due_date = (utcnow_naive() + timedelta(days=int(cadence_followup_due_days))).date().isoformat()
                    repo.record_audit_event(
                        entity_type="workspace_followup",
                        entity_id=None,
                        action="create",
                        actor=user.username,
                        changes={
                            "task_key": task_key,
                            "workflow": "governance_snapshot_cadence",
                            "title": "Governance snapshot cadence below threshold",
                            "owner": str(cadence_followup_owner or user.username).strip() or user.username,
                            "priority": str(cadence_followup_priority).strip().lower(),
                            "due_date": due_date,
                            "status": "open",
                            "environment": settings.app_env,
                            "note": (
                                f"Cadence red. interval_hours={interval_hours}, "
                                f"worker_7d={worker_7d}/{expected_7d}, "
                                f"worker_30d={worker_30d}/{expected_30d}, "
                                f"ratio={cadence_ratio * 100:.1f}%"
                            ),
                        },
                    )
                    st.success(f"Created cadence follow-up task `{task_key}`.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to create cadence follow-up task: {exc}")

        st.markdown("### Governance Snapshot Actions")
        st.caption("Run governance snapshot now using current scheduler settings, without waiting for sync-worker interval.")
        if st.button("Run Governance Snapshot Now", key="admin_sync_jobs_run_governance_snapshot_now_btn"):
            try:
                counts = _record_governance_snapshot_event(
                    repo,
                    actor=user.username,
                    lookback_days=int(desired_governance_snapshot_lookback_days),
                    max_rows=int(desired_governance_snapshot_max_rows),
                    source="admin_sync_jobs",
                    download_intent=False,
                )
                st.success(
                    "Governance snapshot recorded from Sync Jobs. "
                    f"handoff={counts['handoff_events']} "
                    f"feedback={counts['workspace_feedback_events']} "
                    f"parity={counts['parity_followup_events']} "
                    f"comp={counts['photo_comp_events']}"
                )
            except Exception as exc:
                st.error(f"Unable to run governance snapshot: {exc}")

    with tab_governance_exports:
        _render_governance_exports_hub(repo, user)

    with tab_system_health:
        render_system_health(repo)
        st.markdown("### UX Navigation Controls")
        runtime_map_nav = {
            str(row.key): row for row in repo.list_runtime_settings(environment=settings.app_env, active_only=False)
        }

        def _rb_nav(key: str, default: bool) -> bool:
            row = runtime_map_nav.get(key)
            if row is None:
                return default
            return str(row.value or "").strip().lower() in {"1", "true", "yes", "on"}

        current_nav_mode = str((runtime_map_nav.get("ux_navigation_mode").value if runtime_map_nav.get("ux_navigation_mode") else "unified") or "unified").strip().lower()
        if current_nav_mode not in {"unified", "legacy"}:
            current_nav_mode = "unified"
        current_window_start_raw = str(
            (runtime_map_nav.get("ux_navigation_window_start_iso").value if runtime_map_nav.get("ux_navigation_window_start_iso") else "")
            or ""
        ).strip()

        with st.form("admin_nav_controls_form"):
            nc1, nc2, nc3 = st.columns(3)
            with nc1:
                nav_mode = st.selectbox(
                    "Navigation Mode",
                    options=["unified", "legacy"],
                    index=0 if current_nav_mode == "unified" else 1,
                    help="`unified`: pinned pages + role default landing. `legacy`: classic sidebar behavior.",
                )
                nav_telemetry_enabled = st.checkbox(
                    "Enable Navigation Telemetry",
                    value=_rb_nav("ux_navigation_telemetry_enabled", True),
                )
            with nc2:
                role_default_landing_enabled = st.checkbox(
                    "Enable Role Default Landing",
                    value=_rb_nav("ux_role_default_landing_enabled", True),
                )
                workspace_ebay_enabled = st.checkbox(
                    "Enable eBay Workspace Group",
                    value=_rb_nav("ux_workspace_ebay_enabled", True),
                )
                workspace_inventory_enabled = st.checkbox(
                    "Enable Inventory Workspace Group",
                    value=_rb_nav("ux_workspace_inventory_enabled", True),
                )
            with nc3:
                workspace_fulfillment_enabled = st.checkbox(
                    "Enable Fulfillment Workspace Group",
                    value=_rb_nav("ux_workspace_fulfillment_enabled", True),
                )
                workspace_sync_enabled = st.checkbox(
                    "Enable Sync Workspace Group",
                    value=_rb_nav("ux_workspace_sync_enabled", True),
                )
                workspace_revenue_enabled = st.checkbox(
                    "Enable Revenue Workspace Group",
                    value=_rb_nav("ux_workspace_revenue_enabled", True),
                )
                listings_auto_photo_comp_review_preset = st.checkbox(
                    "Auto-Apply Listings Photo-Comp Queue",
                    value=_rb_nav("ux_listings_auto_photo_comp_review_preset", False),
                    help="When enabled, Listings auto-loads the Photo-Comp Review Queue preset once per signed-in session.",
                )
                ebay_require_runbook_for_bulk_ops = st.checkbox(
                    "Require eBay Runbook For Bulk Ops",
                    value=_rb_nav("ebay_require_runbook_for_bulk_ops", False),
                    help="When enabled, eBay Ops bulk controls are disabled until the eBay Workspace runbook checklist is complete.",
                )
                st.text_input(
                    "Telemetry Window Start (ISO, optional)",
                    value=current_window_start_raw,
                    disabled=True,
                    help="Set by action buttons below to isolate baseline windows.",
                )
            save_nav_controls = st.form_submit_button("Save Navigation Controls")

        if save_nav_controls:
            try:
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="ux_navigation_mode",
                    value=nav_mode,
                    value_type="str",
                    description="Navigation rollout mode (`unified` or `legacy`).",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="ux_navigation_telemetry_enabled",
                    value="true" if nav_telemetry_enabled else "false",
                    value_type="bool",
                    description="Enable navigation telemetry audit events.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="ux_role_default_landing_enabled",
                    value="true" if role_default_landing_enabled else "false",
                    value_type="bool",
                    description="Enable role-based default landing redirect from Home page.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="ux_workspace_ebay_enabled",
                    value="true" if workspace_ebay_enabled else "false",
                    value_type="bool",
                    description="Enable eBay workspace grouping/navigation surfaces.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="ux_workspace_inventory_enabled",
                    value="true" if workspace_inventory_enabled else "false",
                    value_type="bool",
                    description="Enable Inventory workspace grouping/navigation surfaces.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="ux_workspace_fulfillment_enabled",
                    value="true" if workspace_fulfillment_enabled else "false",
                    value_type="bool",
                    description="Enable Fulfillment workspace grouping/navigation surfaces.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="ux_workspace_sync_enabled",
                    value="true" if workspace_sync_enabled else "false",
                    value_type="bool",
                    description="Enable Sync workspace grouping/navigation surfaces.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="ux_workspace_revenue_enabled",
                    value="true" if workspace_revenue_enabled else "false",
                    value_type="bool",
                    description="Enable Revenue workspace grouping/navigation surfaces.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="ux_listings_auto_photo_comp_review_preset",
                    value="true" if listings_auto_photo_comp_review_preset else "false",
                    value_type="bool",
                    description="Auto-apply Listings Photo-Comp Review Queue preset once per user session.",
                    is_active=True,
                    actor=user.username,
                )
                repo.upsert_runtime_setting(
                    environment=settings.app_env,
                    key="ebay_require_runbook_for_bulk_ops",
                    value="true" if ebay_require_runbook_for_bulk_ops else "false",
                    value_type="bool",
                    description="Require eBay Workspace runbook completion before bulk eBay Ops actions are enabled.",
                    is_active=True,
                    actor=user.username,
                )
                st.success("Navigation controls saved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to save navigation controls: {exc}")

        wc1, wc2 = st.columns(2)
        with wc1:
            if st.button("Start New Telemetry Window Now", key="admin_nav_window_start_now_btn"):
                try:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key="ux_navigation_window_start_iso",
                        value=utcnow_naive().isoformat(timespec="seconds"),
                        value_type="str",
                        description="Optional telemetry window lower-bound timestamp (ISO UTC) for nav analytics.",
                        is_active=True,
                        actor=user.username,
                    )
                    st.success("Telemetry window start set to current time.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to set telemetry window start: {exc}")
        with wc2:
            if st.button("Clear Telemetry Window Marker", key="admin_nav_window_clear_btn"):
                try:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key="ux_navigation_window_start_iso",
                        value="",
                        value_type="str",
                        description="Optional telemetry window lower-bound timestamp (ISO UTC) for nav analytics.",
                        is_active=True,
                        actor=user.username,
                    )
                    st.success("Telemetry window marker cleared.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to clear telemetry window marker: {exc}")

        st.markdown("### Navigation Telemetry")
        st.caption("Derived from audit events (`entity_type=navigation`) to tune IA and workflow grouping.")
        nav_query = select(AuditLog).where(AuditLog.entity_type == "navigation")
        window_start_dt = None
        if current_window_start_raw:
            try:
                window_start_dt = datetime.fromisoformat(current_window_start_raw)
            except Exception:
                window_start_dt = None
        if window_start_dt is not None:
            nav_query = nav_query.where(AuditLog.created_at >= window_start_dt)
            st.caption(f"Telemetry window start: `{window_start_dt.isoformat(timespec='seconds')}`")
        nav_events = repo.db.scalars(nav_query.order_by(AuditLog.created_at.desc()).limit(1500)).all()
        if not nav_events:
            st.info("No navigation telemetry events recorded yet.")
        else:
            page_view_counter: Counter[str] = Counter()
            switch_counter: Counter[str] = Counter()
            bounce_counter: Counter[str] = Counter()
            handoff_applied_counter: Counter[str] = Counter()
            handoff_cleared_counter: Counter[str] = Counter()
            event_rows: list[dict] = []
            for row in nav_events:
                payload = _audit_changes(row)
                action = str(row.action or "").strip().lower()
                if action == "page_view":
                    page_key = str(payload.get("page") or payload.get("page_title") or "unknown")
                    page_view_counter[page_key] += 1
                elif action == "page_switch":
                    from_page = str(payload.get("from_page") or "").strip()
                    to_page = str(payload.get("to_page") or "").strip()
                    if from_page or to_page:
                        edge = f"{from_page or '?'} -> {to_page or '?'}"
                        switch_counter[edge] += 1
                        try:
                            delta = float(payload.get("seconds_since_last_page") or 0.0)
                        except Exception:
                            delta = 0.0
                        if delta > 0.0 and delta < 10.0:
                            bounce_counter[edge] += 1
                elif action == "workspace_handoff_applied":
                    target = str(payload.get("to") or payload.get("target") or "unknown").strip().lower() or "unknown"
                    handoff_applied_counter[target] += 1
                elif action == "workspace_handoff_cleared":
                    target = str(payload.get("target") or payload.get("to") or "unknown").strip().lower() or "unknown"
                    handoff_cleared_counter[target] += 1
                event_rows.append(
                    {
                        "id": row.id,
                        "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
                        "actor": row.actor,
                        "action": row.action,
                        "changes": json.dumps(payload)[:400],
                    }
                )

            n1, n2, n3 = st.columns(3)
            n1.metric("Total Nav Events", len(nav_events))
            n2.metric("Unique Page Views", len(page_view_counter))
            n3.metric("Unique Switch Paths", len(switch_counter))

            top_pages_df = pd.DataFrame(
                [{"page": page, "views": count} for page, count in page_view_counter.most_common(20)]
            )
            top_switch_df = pd.DataFrame(
                [
                    {"switch_path": path, "count": count, "bounce_lt_10s": int(bounce_counter.get(path, 0))}
                    for path, count in switch_counter.most_common(20)
                ]
            )
            c1, c2 = st.columns(2)
            with c1:
                st.markdown("#### Most Visited Pages")
                if top_pages_df.empty:
                    st.caption("No page-view telemetry yet.")
                else:
                    st.dataframe(top_pages_df, use_container_width=True, hide_index=True)
            with c2:
                st.markdown("#### Most Common Switch Paths")
                if top_switch_df.empty:
                    st.caption("No page-switch telemetry yet.")
                else:
                    st.dataframe(top_switch_df, use_container_width=True, hide_index=True)

            st.markdown("#### Handoff Telemetry")
            total_handoff_applied = int(sum(handoff_applied_counter.values()))
            total_handoff_cleared = int(sum(handoff_cleared_counter.values()))
            clear_rate = (float(total_handoff_cleared) / float(total_handoff_applied)) if total_handoff_applied else 0.0
            handoff_df_for_export = pd.DataFrame()
            h1, h2, h3 = st.columns(3)
            h1.metric("Handoff Applied", total_handoff_applied)
            h2.metric("Handoff Cleared", total_handoff_cleared)
            h3.metric("Handoff Clear Rate", f"{clear_rate * 100:.1f}%")
            handoff_targets = sorted(set(handoff_applied_counter.keys()) | set(handoff_cleared_counter.keys()))
            if handoff_targets:
                handoff_df = pd.DataFrame(
                    [
                        {
                            "target": target,
                            "applied_count": int(handoff_applied_counter.get(target, 0)),
                            "cleared_count": int(handoff_cleared_counter.get(target, 0)),
                            "clear_rate": round(
                                (
                                    float(handoff_cleared_counter.get(target, 0))
                                    / float(handoff_applied_counter.get(target, 0))
                                )
                                if int(handoff_applied_counter.get(target, 0)) > 0
                                else 0.0,
                                4,
                            ),
                        }
                        for target in handoff_targets
                    ]
                ).sort_values(by=["applied_count", "target"], ascending=[False, True])
                handoff_df_for_export = handoff_df.copy()
                st.dataframe(handoff_df, use_container_width=True, hide_index=True)
            else:
                st.caption("No workspace handoff telemetry recorded yet.")
            handoff_event_rows = [
                {
                    "id": row.id,
                    "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
                    "actor": str(row.actor or ""),
                    "action": str(row.action or ""),
                    "target": str(
                        _audit_changes(row).get("target", "")
                        or _audit_changes(row).get("to", "")
                        or "unknown"
                    ).strip().lower(),
                    "summary": json.dumps(_audit_changes(row))[:220],
                }
                for row in nav_events
                if str(row.action or "").strip().lower() in {"workspace_handoff_applied", "workspace_handoff_cleared"}
            ]
            with st.expander("Recent Handoff Events", expanded=False):
                if not handoff_event_rows:
                    st.caption("No handoff events recorded in this telemetry window.")
                else:
                    handoff_events_df = pd.DataFrame(handoff_event_rows)
                    hf1, hf2, hf3 = st.columns(3)
                    with hf1:
                        action_filter = st.multiselect(
                            "Action",
                            options=sorted(handoff_events_df["action"].dropna().unique().tolist()),
                            default=[],
                            key="admin_handoff_events_action_filter",
                        )
                    with hf2:
                        target_filter = st.multiselect(
                            "Target",
                            options=sorted(handoff_events_df["target"].dropna().unique().tolist()),
                            default=[],
                            key="admin_handoff_events_target_filter",
                        )
                    with hf3:
                        actor_filter = st.multiselect(
                            "Actor",
                            options=sorted(handoff_events_df["actor"].dropna().unique().tolist()),
                            default=[],
                            key="admin_handoff_events_actor_filter",
                        )
                    filtered_handoff_df = handoff_events_df
                    if action_filter:
                        filtered_handoff_df = filtered_handoff_df[
                            filtered_handoff_df["action"].isin(action_filter)
                        ]
                    if target_filter:
                        filtered_handoff_df = filtered_handoff_df[
                            filtered_handoff_df["target"].isin(target_filter)
                        ]
                    if actor_filter:
                        filtered_handoff_df = filtered_handoff_df[
                            filtered_handoff_df["actor"].isin(actor_filter)
                        ]
                    top_actor = ""
                    top_target = ""
                    most_cleared_target = ""
                    if not filtered_handoff_df.empty:
                        actor_counts = (
                            filtered_handoff_df["actor"]
                            .fillna("")
                            .astype(str)
                            .str.strip()
                            .loc[lambda s: s != ""]
                            .value_counts()
                        )
                        if not actor_counts.empty:
                            top_actor = str(actor_counts.index[0])
                        target_counts = (
                            filtered_handoff_df["target"]
                            .fillna("")
                            .astype(str)
                            .str.strip()
                            .loc[lambda s: s != ""]
                            .value_counts()
                        )
                        if not target_counts.empty:
                            top_target = str(target_counts.index[0])
                        cleared_df = filtered_handoff_df[
                            filtered_handoff_df["action"].astype(str).str.lower() == "workspace_handoff_cleared"
                        ]
                        if not cleared_df.empty:
                            cleared_counts = (
                                cleared_df["target"]
                                .fillna("")
                                .astype(str)
                                .str.strip()
                                .loc[lambda s: s != ""]
                                .value_counts()
                            )
                            if not cleared_counts.empty:
                                most_cleared_target = str(cleared_counts.index[0])
                    k1, k2, k3 = st.columns(3)
                    k1.metric("Top Actor", top_actor or "n/a")
                    k2.metric("Top Target", top_target or "n/a")
                    k3.metric("Most Cleared Target", most_cleared_target or "n/a")
                    st.download_button(
                        "Download Handoff Events CSV",
                        data=filtered_handoff_df.to_csv(index=False).encode("utf-8"),
                        file_name=f"handoff_events_{settings.app_env}_{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv",
                        mime="text/csv",
                        key="admin_handoff_events_csv_btn",
                    )
                    bundle_buffer = BytesIO()
                    with zipfile.ZipFile(bundle_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as bundle_zip:
                        handoff_kpis_df = pd.DataFrame(
                            [
                                {
                                    "environment": settings.app_env,
                                    "total_handoff_applied": int(total_handoff_applied),
                                    "total_handoff_cleared": int(total_handoff_cleared),
                                    "handoff_clear_rate_pct": round(float(clear_rate) * 100.0, 2),
                                    "top_actor": top_actor or "",
                                    "top_target": top_target or "",
                                    "most_cleared_target": most_cleared_target or "",
                                    "generated_at_utc": utcnow_naive().isoformat(),
                                }
                            ]
                        )
                        bundle_zip.writestr("handoff_kpis.csv", handoff_kpis_df.to_csv(index=False))
                        if not handoff_df_for_export.empty:
                            agg_df = handoff_df_for_export.copy()
                            agg_df.insert(0, "environment", settings.app_env)
                            bundle_zip.writestr(
                                "handoff_target_aggregate.csv",
                                agg_df.to_csv(index=False),
                            )
                        bundle_zip.writestr(
                            "handoff_events_filtered.csv",
                            filtered_handoff_df.to_csv(index=False),
                        )
                        full_events_df = handoff_events_df.copy()
                        full_events_df.insert(0, "environment", settings.app_env)
                        bundle_zip.writestr(
                            "handoff_events_full_window.csv",
                            full_events_df.to_csv(index=False),
                        )
                    bundle_buffer.seek(0)
                    st.download_button(
                        "Export Handoff Governance Bundle (ZIP)",
                        data=bundle_buffer.getvalue(),
                        file_name=f"handoff_governance_bundle_{settings.app_env}_{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.zip",
                        mime="application/zip",
                        key="admin_handoff_events_bundle_zip_btn",
                    )
                    st.dataframe(
                        filtered_handoff_df.head(200),
                        use_container_width=True,
                        hide_index=True,
                    )

            with st.expander("Recent Navigation Events", expanded=False):
                st.dataframe(pd.DataFrame(event_rows[:200]), use_container_width=True, hide_index=True)

            st.markdown("#### Workflow Baseline Metrics")
            switch_events: list[dict] = []
            for row in nav_events:
                payload = _audit_changes(row)
                if str(row.action or "").strip().lower() != "page_switch":
                    continue
                try:
                    delta = float(payload.get("seconds_since_last_page") or 0.0)
                except Exception:
                    delta = 0.0
                switch_events.append(
                    {
                        "actor": str(row.actor or "unknown"),
                        "created_at": row.created_at,
                        "from_page": str(payload.get("from_page") or "").strip().lower(),
                        "to_page": str(payload.get("to_page") or "").strip().lower(),
                        "delta_s": delta,
                    }
                )

            if not switch_events:
                st.caption("No page-switch telemetry yet for baseline metrics.")
            else:
                def _median(values: list[float]) -> float:
                    vals = sorted(v for v in values if v >= 0)
                    if not vals:
                        return 0.0
                    n = len(vals)
                    mid = n // 2
                    if n % 2 == 1:
                        return float(vals[mid])
                    return float((vals[mid - 1] + vals[mid]) / 2.0)

                all_deltas = [float(r["delta_s"]) for r in switch_events if float(r["delta_s"]) > 0]
                bounce_count = len([v for v in all_deltas if v < 10.0])
                bounce_rate = (bounce_count / len(all_deltas)) if all_deltas else 0.0

                # Session click-depth: per actor, new session starts after 30m inactivity.
                sessions: list[int] = []
                by_actor: dict[str, list[dict]] = {}
                for row in switch_events:
                    by_actor.setdefault(str(row["actor"]), []).append(row)
                for actor_rows in by_actor.values():
                    actor_rows_sorted = sorted(actor_rows, key=lambda r: r["created_at"] or utcnow_naive())
                    session_count = 0
                    prev_ts = None
                    for ev in actor_rows_sorted:
                        ts = ev["created_at"]
                        if ts is None:
                            continue
                        if prev_ts is None or (ts - prev_ts).total_seconds() > 1800:
                            if session_count > 0:
                                sessions.append(session_count)
                            session_count = 1
                        else:
                            session_count += 1
                        prev_ts = ts
                    if session_count > 0:
                        sessions.append(session_count)

                def _workflow_median(pairs: set[tuple[str, str]]) -> float:
                    vals = [
                        float(r["delta_s"])
                        for r in switch_events
                        if (str(r["from_page"]), str(r["to_page"])) in pairs and float(r["delta_s"]) > 0
                    ]
                    return _median(vals)

                listing_pairs = {
                    ("operations_home", "listings"),
                    ("products", "listings"),
                    ("inventory_intake_wizard", "listings"),
                    ("listings", "ebay_workspace"),
                }
                fulfillment_pairs = {
                    ("operations_home", "shipping"),
                    ("orders", "shipping"),
                    ("sales", "shipping"),
                    ("shipping", "orders"),
                }
                reconcile_pairs = {
                    ("shipping", "sync"),
                    ("sales", "reports"),
                    ("sync", "reports"),
                    ("reports", "documents"),
                }

                b1, b2, b3, b4 = st.columns(4)
                b1.metric("Median Switch Latency (s)", f"{_median(all_deltas):.1f}")
                b2.metric("Bounce Rate (<10s)", f"{bounce_rate * 100:.1f}%")
                b3.metric("Median Click-Depth / Session", f"{_median([float(v) for v in sessions]):.1f}")
                b4.metric("Switch Events (window)", len(switch_events))

                wf_df = pd.DataFrame(
                    [
                        {
                            "workflow": "Listing handoff",
                            "median_transition_seconds": round(_workflow_median(listing_pairs), 2),
                        },
                        {
                            "workflow": "Fulfillment handoff",
                            "median_transition_seconds": round(_workflow_median(fulfillment_pairs), 2),
                        },
                        {
                            "workflow": "Reconcile handoff",
                            "median_transition_seconds": round(_workflow_median(reconcile_pairs), 2),
                        },
                    ]
                )
                st.dataframe(wf_df, use_container_width=True, hide_index=True)
                baseline_summary_df = pd.DataFrame(
                    [
                        {"metric": "median_switch_latency_seconds", "value": round(_median(all_deltas), 4)},
                        {"metric": "bounce_rate_lt_10s", "value": round(bounce_rate, 6)},
                        {"metric": "median_click_depth_per_session", "value": round(_median([float(v) for v in sessions]), 4)},
                        {"metric": "switch_event_count", "value": int(len(switch_events))},
                        {"metric": "window_start", "value": window_start_dt.isoformat(timespec="seconds") if window_start_dt else ""},
                        {"metric": "window_end", "value": utcnow_naive().isoformat(timespec="seconds")},
                    ]
                )
                combined_baseline_export = pd.concat(
                    [
                        baseline_summary_df.assign(section="summary"),
                        wf_df.rename(columns={"median_transition_seconds": "value"}).assign(section="workflow"),
                    ],
                    ignore_index=True,
                )
                ex1, ex2 = st.columns(2)
                with ex1:
                    st.download_button(
                        "Download Baseline Metrics CSV",
                        data=combined_baseline_export.to_csv(index=False).encode("utf-8"),
                        file_name=f"ux_baseline_metrics_{settings.app_env}_{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv",
                        mime="text/csv",
                        key="admin_nav_baseline_metrics_csv_btn",
                    )
                with ex2:
                    if st.button("Record Baseline Snapshot Event", key="admin_nav_baseline_snapshot_btn"):
                        try:
                            repo.record_audit_event(
                                entity_type="navigation_baseline",
                                entity_id=None,
                                action="snapshot",
                                actor=user.username,
                                changes={
                                    "environment": settings.app_env,
                                    "recorded_at": utcnow_naive().isoformat(timespec="seconds"),
                                    "summary": baseline_summary_df.to_dict(orient="records"),
                                    "workflow_handoffs": wf_df.to_dict(orient="records"),
                                },
                            )
                            st.success("Baseline metrics snapshot recorded.")
                        except Exception as exc:
                            st.error(f"Unable to record baseline snapshot: {exc}")

        st.markdown("### Workspace Feedback Insights")
        st.caption("Aggregated from audit events (`entity_type=workspace_feedback`) to prioritize UX fixes.")
        feedback_lookback_days = st.number_input(
            "Feedback Lookback Window (days)",
            min_value=1,
            max_value=365,
            value=30,
            step=1,
            key="admin_workspace_feedback_lookback_days",
        )
        feedback_since_dt = utcnow_naive() - timedelta(days=int(feedback_lookback_days))
        if window_start_dt is not None and window_start_dt > feedback_since_dt:
            feedback_since_dt = window_start_dt
        feedback_rows = repo.db.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "workspace_feedback",
                AuditLog.created_at >= feedback_since_dt,
            )
            .order_by(AuditLog.created_at.desc())
            .limit(2000)
        ).all()
        if not feedback_rows:
            st.caption("No workspace feedback events in the selected window.")
        else:
            feedback_counter: Counter[str] = Counter()
            sentiment_counter: Counter[str] = Counter()
            flattened_feedback_rows: list[dict] = []
            for row in feedback_rows:
                payload = _audit_changes(row)
                workspace = str(payload.get("workspace") or "unknown").strip().lower() or "unknown"
                sentiment = str(payload.get("sentiment") or "unknown").strip().lower() or "unknown"
                note = str(payload.get("note") or "").strip()
                feedback_counter[workspace] += 1
                sentiment_counter[sentiment] += 1
                flattened_feedback_rows.append(
                    {
                        "id": int(row.id),
                        "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
                        "actor": str(row.actor or ""),
                        "workspace": workspace,
                        "sentiment": sentiment,
                        "note": note,
                    }
                )

            total_feedback = len(flattened_feedback_rows)
            down_count = int(sentiment_counter.get("down", 0))
            up_count = int(sentiment_counter.get("up", 0))
            down_rate = (down_count / total_feedback) if total_feedback else 0.0
            f1, f2, f3, f4 = st.columns(4)
            f1.metric("Feedback Events", total_feedback)
            f2.metric("Needs Improvement", down_count)
            f3.metric("Helpful", up_count)
            f4.metric("Needs Improvement Rate", f"{down_rate * 100:.1f}%")

            by_workspace_df = pd.DataFrame(
                [
                    {
                        "workspace": workspace,
                        "feedback_count": count,
                        "needs_improvement": int(
                            len(
                                [
                                    r
                                    for r in flattened_feedback_rows
                                    if r["workspace"] == workspace and r["sentiment"] == "down"
                                ]
                            )
                        ),
                    }
                    for workspace, count in feedback_counter.most_common(50)
                ]
            )
            if not by_workspace_df.empty:
                by_workspace_df["needs_improvement_rate"] = by_workspace_df.apply(
                    lambda r: round(
                        float(r["needs_improvement"]) / float(r["feedback_count"])
                        if float(r["feedback_count"]) > 0
                        else 0.0,
                        4,
                    ),
                    axis=1,
                )
            by_sentiment_df = pd.DataFrame(
                [{"sentiment": k, "count": v} for k, v in sentiment_counter.items()]
            ).sort_values(by="count", ascending=False)
            fc1, fc2 = st.columns(2)
            with fc1:
                st.markdown("#### Feedback by Workspace")
                st.dataframe(by_workspace_df, use_container_width=True, hide_index=True)
            with fc2:
                st.markdown("#### Feedback by Sentiment")
                st.dataframe(by_sentiment_df, use_container_width=True, hide_index=True)

            notes_only_df = pd.DataFrame([r for r in flattened_feedback_rows if r.get("note")]).sort_values(
                by="created_at", ascending=False
            )
            with st.expander("Recent Feedback Notes", expanded=False):
                if notes_only_df.empty:
                    st.caption("No note text submitted in the selected window.")
                else:
                    st.dataframe(notes_only_df.head(300), use_container_width=True, hide_index=True)
                    note_options = {
                        (
                            f"#{int(r['id'])} | {r['workspace']} | {r['sentiment']} | "
                            f"{str(r['created_at'])[:19]} | {str(r['note'])[:70]}"
                        ): r
                        for _, r in notes_only_df.head(300).iterrows()
                    }
                    selected_feedback_label = st.selectbox(
                        "Create follow-up from feedback note",
                        options=["None"] + list(note_options.keys()),
                        key="admin_workspace_feedback_followup_select",
                    )
                    followup_owner = st.text_input(
                        "Follow-up Owner",
                        value=user.username,
                        key="admin_workspace_feedback_followup_owner",
                    )
                    followup_priority = st.selectbox(
                        "Follow-up Priority",
                        options=["high", "medium", "low"],
                        index=1,
                        key="admin_workspace_feedback_followup_priority",
                    )
                    followup_due_days = st.number_input(
                        "Due in Days",
                        min_value=0,
                        max_value=60,
                        value=7,
                        step=1,
                        key="admin_workspace_feedback_followup_due_days",
                    )
                    if st.button(
                        "Create Follow-up Task From Selected Feedback",
                        key="admin_workspace_feedback_create_followup_btn",
                        disabled=selected_feedback_label == "None",
                    ):
                        selected_row = note_options.get(selected_feedback_label)
                        if not selected_row:
                            st.error("Select a feedback note first.")
                        else:
                            try:
                                task_id = f"wf-feedback-{int(selected_row['id'])}-{utcnow_naive().strftime('%Y%m%d%H%M%S')}"
                                due_date = (utcnow_naive() + timedelta(days=int(followup_due_days))).date()
                                workspace = str(selected_row.get("workspace") or "unknown").strip().lower()
                                sentiment = str(selected_row.get("sentiment") or "unknown").strip().lower()
                                note = str(selected_row.get("note") or "").strip()
                                repo.record_audit_event(
                                    entity_type="workspace_followup",
                                    entity_id=None,
                                    action="create",
                                    actor=user.username,
                                    changes={
                                        "task_id": task_id,
                                        "workflow": f"feedback:{workspace}",
                                        "title": f"[feedback/{sentiment}] {workspace} UX follow-up",
                                        "owner": str(followup_owner or user.username).strip() or user.username,
                                        "priority": str(followup_priority).strip().lower(),
                                        "due_date": due_date.isoformat(),
                                        "note": note,
                                        "source_feedback_id": int(selected_row["id"]),
                                        "source_workspace": workspace,
                                        "source_sentiment": sentiment,
                                    },
                                )
                                st.success(f"Created follow-up task `{task_id}`.")
                                st.rerun()
                            except Exception as exc:
                                st.error(f"Unable to create follow-up task from feedback: {exc}")
            st.download_button(
                "Download Workspace Feedback CSV",
                data=pd.DataFrame(flattened_feedback_rows).to_csv(index=False).encode("utf-8"),
                file_name=f"workspace_feedback_{settings.app_env}_{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv",
                key="admin_workspace_feedback_csv_btn",
            )
            feedback_bundle_buffer = BytesIO()
            with zipfile.ZipFile(feedback_bundle_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as bundle_zip:
                feedback_events_df = pd.DataFrame(flattened_feedback_rows)
                feedback_events_df.insert(0, "environment", settings.app_env)
                feedback_events_df.insert(1, "lookback_days", int(feedback_lookback_days))
                bundle_zip.writestr("workspace_feedback_events.csv", feedback_events_df.to_csv(index=False))
                workspace_export_df = by_workspace_df.copy()
                workspace_export_df.insert(0, "environment", settings.app_env)
                bundle_zip.writestr("workspace_feedback_by_workspace.csv", workspace_export_df.to_csv(index=False))
                sentiment_export_df = by_sentiment_df.copy()
                sentiment_export_df.insert(0, "environment", settings.app_env)
                bundle_zip.writestr("workspace_feedback_by_sentiment.csv", sentiment_export_df.to_csv(index=False))
                notes_export_df = notes_only_df.copy()
                if not notes_export_df.empty:
                    notes_export_df.insert(0, "environment", settings.app_env)
                bundle_zip.writestr("workspace_feedback_notes.csv", notes_export_df.to_csv(index=False))
                summary_export_df = pd.DataFrame(
                    [
                        {
                            "environment": settings.app_env,
                            "lookback_days": int(feedback_lookback_days),
                            "feedback_events": int(total_feedback),
                            "needs_improvement_count": int(down_count),
                            "helpful_count": int(up_count),
                            "needs_improvement_rate_pct": round(float(down_rate) * 100.0, 2),
                            "generated_at_utc": utcnow_naive().isoformat(),
                        }
                    ]
                )
                bundle_zip.writestr("workspace_feedback_summary.csv", summary_export_df.to_csv(index=False))
            feedback_bundle_buffer.seek(0)
            st.download_button(
                "Export Workspace Feedback Governance Bundle (ZIP)",
                data=feedback_bundle_buffer.getvalue(),
                file_name=(
                    f"workspace_feedback_governance_bundle_{settings.app_env}_"
                    f"{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.zip"
                ),
                mime="application/zip",
                key="admin_workspace_feedback_bundle_zip_btn",
            )

        st.markdown("### Workspace Rollout Parity Checker")
        st.caption(
            "Validates permission coverage and recent audit evidence across legacy vs unified workflow contracts."
        )
        lookback_days = st.number_input(
            "Audit Lookback Window (days)",
            min_value=1,
            max_value=180,
            value=30,
            step=1,
            key="admin_parity_lookback_days",
        )
        current_min_task_events = max(0, int(get_runtime_int(repo, "ux_parity_min_task_completion_events", 1)))
        min_task_events_input = st.number_input(
            "Minimum Task-Completion Events Per Workflow",
            min_value=0,
            max_value=100,
            value=int(current_min_task_events),
            step=1,
            key="admin_parity_min_task_events",
            help="If >0, workflows with configured task telemetry must meet this threshold in the lookback window.",
        )
        if int(min_task_events_input) != int(current_min_task_events):
            if st.button("Save Task-Completion Threshold", key="admin_parity_save_min_task_events"):
                try:
                    repo.upsert_runtime_setting(
                        environment=settings.app_env,
                        key="ux_parity_min_task_completion_events",
                        value=str(int(min_task_events_input)),
                        value_type="int",
                        description="Minimum workspace task-completion events required in parity lookback window per workflow.",
                        is_active=True,
                        actor=user.username,
                    )
                    st.success("Task-completion threshold saved.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to save threshold: {exc}")
        since_dt = utcnow_naive() - timedelta(days=int(lookback_days))
        parity_specs = _workspace_parity_specs()
        role_permission_map = st.session_state.get("auth_role_permissions", DEFAULT_PERMISSIONS)
        audit_rows = repo.db.scalars(
            select(AuditLog).where(AuditLog.created_at >= since_dt).order_by(AuditLog.created_at.desc()).limit(5000)
        ).all()
        min_task_events = int(min_task_events_input)
        task_completion_counts: Counter[str] = Counter()
        for row in audit_rows:
            if str(row.entity_type or "").strip().lower() != "workspace_task_completion":
                continue
            if str(row.action or "").strip().lower() != "complete":
                continue
            payload = _audit_changes(row)
            workflow_key = str(payload.get("workflow") or "").strip().lower()
            if workflow_key:
                task_completion_counts[workflow_key] += 1

        parity_rows: list[dict] = []
        for spec in parity_specs:
            required_permission = str(spec.get("required_permission") or "").strip()
            viewer_ok = required_permission in set(role_permission_map.get("viewer", set()))
            ops_ok = required_permission in set(role_permission_map.get("ops", set()))
            admin_ok = True  # admin is super-role by policy

            entity_types = {str(v).strip().lower() for v in spec.get("audit_entity_types", []) if str(v).strip()}
            actions = {str(v).strip().lower() for v in spec.get("audit_actions", []) if str(v).strip()}
            observed = False
            observed_count = 0
            for row in audit_rows:
                row_entity = str(row.entity_type or "").strip().lower()
                row_action = str(row.action or "").strip().lower()
                if (not entity_types or row_entity in entity_types) and (not actions or row_action in actions):
                    observed = True
                    observed_count += 1
            task_workflow_keys = [
                str(v or "").strip().lower()
                for v in spec.get("task_completion_workflows", [])
                if str(v or "").strip()
            ]
            task_count = (
                sum(int(task_completion_counts.get(k, 0)) for k in task_workflow_keys)
                if task_workflow_keys
                else 0
            )
            task_observed = True if not task_workflow_keys else task_count >= int(min_task_events)

            parity_rows.append(
                {
                    "workflow": spec.get("workflow"),
                    "legacy_surface": spec.get("legacy_surface"),
                    "unified_surface": spec.get("unified_surface"),
                    "required_permission": required_permission,
                    "viewer_has_permission": bool(viewer_ok),
                    "ops_has_permission": bool(ops_ok),
                    "admin_has_permission": bool(admin_ok),
                    "audit_observed_in_window": bool(observed),
                    "audit_match_count": int(observed_count),
                    "task_completion_required": bool(task_workflow_keys),
                    "task_completion_events": int(task_count),
                    "task_completion_observed": bool(task_observed),
                }
            )

        parity_df = pd.DataFrame(parity_rows)
        st.dataframe(parity_df, use_container_width=True, hide_index=True)
        permission_gap_df = parity_df[
            (~parity_df["ops_has_permission"]) | (~parity_df["admin_has_permission"])
        ]
        audit_gap_df = parity_df[~parity_df["audit_observed_in_window"]]
        task_gap_df = parity_df[
            (parity_df["task_completion_required"] == True)
            & (parity_df["task_completion_observed"] == False)
        ]
        p1, p2, p3, p4 = st.columns(4)
        p1.metric("Parity Workflows", len(parity_df))
        p2.metric("Permission Gaps", len(permission_gap_df))
        p3.metric("No Audit Evidence", len(audit_gap_df))
        p4.metric("No Task Completion Evidence", len(task_gap_df))
        open_followups_count = 0
        overdue_followups_count = 0
        followup_snapshot_rows = repo.db.scalars(
            select(AuditLog)
            .where(AuditLog.entity_type == "workspace_followup")
            .order_by(AuditLog.created_at.desc())
            .limit(1000)
        ).all()
        if followup_snapshot_rows:
            created_by_key: dict[str, AuditLog] = {}
            resolved_keys: set[str] = set()
            for row in followup_snapshot_rows:
                payload = _audit_changes(row)
                task_key = str(payload.get("task_key") or "").strip()
                if not task_key:
                    continue
                action = str(row.action or "").strip().lower()
                if action == "create" and task_key not in created_by_key:
                    created_by_key[task_key] = row
                if action in {"resolve", "closed"}:
                    resolved_keys.add(task_key)
            today = utcnow_naive().date()
            for task_key, row in created_by_key.items():
                if task_key in resolved_keys:
                    continue
                open_followups_count += 1
                payload = _audit_changes(row)
                due_raw = str(payload.get("due_date") or "").strip()
                if due_raw:
                    try:
                        due_dt = datetime.fromisoformat(due_raw).date()
                        if due_dt < today:
                            overdue_followups_count += 1
                    except Exception:
                        pass

        latest_decision_row_for_score = repo.db.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "workspace_parity_decision",
                AuditLog.action == "decision",
            )
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        ).first()
        latest_decision_value = (
            str(((latest_decision_row_for_score.changes or {}).get("decision") or "")).strip().lower()
            if latest_decision_row_for_score
            else ""
        )
        weight_permission_gap = max(
            0, min(50, int(get_runtime_int(repo, "ux_readiness_weight_permission_gap", 12)))
        )
        weight_audit_gap = max(0, min(50, int(get_runtime_int(repo, "ux_readiness_weight_audit_gap", 8)))
        )
        weight_overdue = max(
            0, min(50, int(get_runtime_int(repo, "ux_readiness_weight_overdue_followup", 5)))
        )
        weight_task_gap = max(0, min(50, int(get_runtime_int(repo, "ux_readiness_weight_task_gap", 6))))
        penalty_rejected = max(
            0, min(100, int(get_runtime_int(repo, "ux_readiness_penalty_rejected_decision", 25)))
        )
        penalty_missing_decision = max(
            0, min(100, int(get_runtime_int(repo, "ux_readiness_penalty_missing_decision", 10)))
        )
        score = 100
        score -= min(40, int(len(permission_gap_df) * weight_permission_gap))
        score -= min(30, int(len(audit_gap_df) * weight_audit_gap))
        score -= min(20, int(overdue_followups_count * weight_overdue))
        score -= min(20, int(len(task_gap_df) * weight_task_gap))
        if latest_decision_value == "rejected":
            score -= penalty_rejected
        if latest_decision_value not in {"approved", "rejected"}:
            score -= penalty_missing_decision
        score = max(0, min(100, score))
        readiness = "green" if score >= 85 else "yellow" if score >= 65 else "red"
        r1, r2, r3 = st.columns(3)
        r1.metric("Rollout Readiness Score", f"{score}/100")
        r2.metric("Open Follow-ups", open_followups_count)
        r3.metric("Overdue Follow-ups", overdue_followups_count)
        if readiness == "green":
            st.success("Readiness status: GREEN")
        elif readiness == "yellow":
            st.warning("Readiness status: YELLOW")
        else:
            st.error("Readiness status: RED")
        with st.expander("Readiness Score Weights", expanded=False):
            st.caption("Tune score strictness per environment.")
            preset_map = {
                "conservative": {
                    "ux_readiness_weight_permission_gap": 16,
                    "ux_readiness_weight_audit_gap": 12,
                    "ux_readiness_weight_overdue_followup": 8,
                    "ux_readiness_weight_task_gap": 10,
                    "ux_readiness_penalty_rejected_decision": 35,
                    "ux_readiness_penalty_missing_decision": 15,
                },
                "balanced": {
                    "ux_readiness_weight_permission_gap": 12,
                    "ux_readiness_weight_audit_gap": 8,
                    "ux_readiness_weight_overdue_followup": 5,
                    "ux_readiness_weight_task_gap": 6,
                    "ux_readiness_penalty_rejected_decision": 25,
                    "ux_readiness_penalty_missing_decision": 10,
                },
                "aggressive": {
                    "ux_readiness_weight_permission_gap": 8,
                    "ux_readiness_weight_audit_gap": 5,
                    "ux_readiness_weight_overdue_followup": 3,
                    "ux_readiness_weight_task_gap": 3,
                    "ux_readiness_penalty_rejected_decision": 15,
                    "ux_readiness_penalty_missing_decision": 5,
                },
            }
            current_weights = {
                "ux_readiness_weight_permission_gap": int(weight_permission_gap),
                "ux_readiness_weight_audit_gap": int(weight_audit_gap),
                "ux_readiness_weight_overdue_followup": int(weight_overdue),
                "ux_readiness_weight_task_gap": int(weight_task_gap),
                "ux_readiness_penalty_rejected_decision": int(penalty_rejected),
                "ux_readiness_penalty_missing_decision": int(penalty_missing_decision),
            }
            current_preset_name = "custom"
            for preset_name, preset_values in preset_map.items():
                if all(int(current_weights.get(k, -1)) == int(v) for k, v in preset_values.items()):
                    current_preset_name = preset_name
                    break
            st.caption(f"Current preset match: `{current_preset_name}`")
            st.caption("Preset profiles")
            pcol1, pcol2, pcol3 = st.columns(3)
            with pcol1:
                apply_conservative = st.button(
                    "Apply Conservative",
                    key="admin_readiness_preset_conservative",
                    use_container_width=True,
                )
            with pcol2:
                apply_balanced = st.button(
                    "Apply Balanced",
                    key="admin_readiness_preset_balanced",
                    use_container_width=True,
                )
            with pcol3:
                apply_aggressive = st.button(
                    "Apply Aggressive",
                    key="admin_readiness_preset_aggressive",
                    use_container_width=True,
                )
            selected_preset = ""
            if apply_conservative:
                selected_preset = "conservative"
            elif apply_balanced:
                selected_preset = "balanced"
            elif apply_aggressive:
                selected_preset = "aggressive"
            if selected_preset:
                try:
                    for key, value in preset_map[selected_preset].items():
                        repo.upsert_runtime_setting(
                            environment=settings.app_env,
                            key=key,
                            value=str(int(value)),
                            value_type="int",
                            description=f"Readiness scoring preset `{selected_preset}` value.",
                            is_active=True,
                            actor=user.username,
                        )
                    st.success(f"Applied `{selected_preset}` readiness preset.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to apply readiness preset: {exc}")

            with st.form("admin_readiness_weights_form"):
                w1, w2, w3 = st.columns(3)
                with w1:
                    edit_weight_permission_gap = st.number_input(
                        "Penalty per Permission Gap",
                        min_value=0,
                        max_value=50,
                        value=int(weight_permission_gap),
                        step=1,
                    )
                    edit_weight_audit_gap = st.number_input(
                        "Penalty per Audit Gap",
                        min_value=0,
                        max_value=50,
                        value=int(weight_audit_gap),
                        step=1,
                    )
                with w2:
                    edit_weight_overdue = st.number_input(
                        "Penalty per Overdue Follow-up",
                        min_value=0,
                        max_value=50,
                        value=int(weight_overdue),
                        step=1,
                    )
                    edit_weight_task_gap = st.number_input(
                        "Penalty per Task-Completion Gap",
                        min_value=0,
                        max_value=50,
                        value=int(weight_task_gap),
                        step=1,
                    )
                    edit_penalty_rejected = st.number_input(
                        "Penalty: Rejected Decision",
                        min_value=0,
                        max_value=100,
                        value=int(penalty_rejected),
                        step=1,
                    )
                with w3:
                    edit_penalty_missing_decision = st.number_input(
                        "Penalty: Missing Decision",
                        min_value=0,
                        max_value=100,
                        value=int(penalty_missing_decision),
                        step=1,
                    )
                save_weights = st.form_submit_button("Save Readiness Weights")
            if save_weights:
                try:
                    weight_updates = [
                        (
                            "ux_readiness_weight_permission_gap",
                            str(int(edit_weight_permission_gap)),
                            "Penalty per permission gap workflow.",
                        ),
                        (
                            "ux_readiness_weight_audit_gap",
                            str(int(edit_weight_audit_gap)),
                            "Penalty per missing-audit-evidence workflow.",
                        ),
                        (
                            "ux_readiness_weight_overdue_followup",
                            str(int(edit_weight_overdue)),
                            "Penalty per overdue open follow-up task.",
                        ),
                        (
                            "ux_readiness_weight_task_gap",
                            str(int(edit_weight_task_gap)),
                            "Penalty per workflow missing task-completion evidence.",
                        ),
                        (
                            "ux_readiness_penalty_rejected_decision",
                            str(int(edit_penalty_rejected)),
                            "Penalty when latest release decision is rejected.",
                        ),
                        (
                            "ux_readiness_penalty_missing_decision",
                            str(int(edit_penalty_missing_decision)),
                            "Penalty when no latest approved/rejected decision exists.",
                        ),
                    ]
                    for key, value, desc in weight_updates:
                        repo.upsert_runtime_setting(
                            environment=settings.app_env,
                            key=key,
                            value=value,
                            value_type="int",
                            description=desc,
                            is_active=True,
                            actor=user.username,
                        )
                    st.success("Readiness score weights saved.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to save readiness weights: {exc}")
        if not permission_gap_df.empty:
            st.warning("Permission parity gaps detected for one or more workflows.")
            st.dataframe(
                permission_gap_df[
                    [
                        "workflow",
                        "required_permission",
                        "viewer_has_permission",
                        "ops_has_permission",
                        "admin_has_permission",
                    ]
                ],
                use_container_width=True,
                hide_index=True,
            )
        if not audit_gap_df.empty:
            st.info(
                "Some workflows have no recent audit evidence in this window. Run workflow smoke tests before cutover."
            )
            st.dataframe(
                audit_gap_df[["workflow", "legacy_surface", "unified_surface", "required_permission"]],
                use_container_width=True,
                hide_index=True,
            )
        if not task_gap_df.empty:
            st.info(
                f"Some workflows are missing task-completion evidence (threshold={int(min_task_events)} event(s))."
            )
            st.dataframe(
                task_gap_df[["workflow", "task_completion_events", "task_completion_observed"]],
                use_container_width=True,
                hide_index=True,
            )
        s1, s2 = st.columns(2)
        with s1:
            st.download_button(
                "Download Parity Snapshot CSV",
                data=parity_df.to_csv(index=False).encode("utf-8"),
                file_name=(
                    f"workspace_parity_snapshot_{settings.app_env}_"
                    f"{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv"
                ),
                mime="text/csv",
                key="admin_parity_snapshot_csv_btn",
            )
        with s2:
            if st.button("Record Parity Snapshot Event", key="admin_record_parity_snapshot_btn"):
                try:
                    snapshot_ts = utcnow_naive()
                    repo.record_audit_event(
                        entity_type="workspace_parity",
                        entity_id=None,
                        action="snapshot",
                        actor=user.username,
                        changes={
                            "environment": settings.app_env,
                            "recorded_at": snapshot_ts.isoformat(timespec="seconds"),
                            "lookback_days": int(lookback_days),
                            "since": since_dt.isoformat(timespec="seconds"),
                            "workflow_count": int(len(parity_df)),
                            "permission_gap_count": int(len(permission_gap_df)),
                            "audit_gap_count": int(len(audit_gap_df)),
                            "workflows": parity_df.to_dict(orient="records"),
                        },
                    )
                    st.success("Workspace parity snapshot recorded to audit log.")
                except Exception as exc:
                    st.error(f"Unable to record parity snapshot: {exc}")

        recent_df = pd.DataFrame()
        st.markdown("#### Recent Parity Snapshots")
        parity_snapshot_rows = repo.db.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "workspace_parity",
                AuditLog.action == "snapshot",
            )
            .order_by(AuditLog.created_at.desc())
            .limit(30)
        ).all()
        if not parity_snapshot_rows:
            st.caption("No parity snapshots recorded yet.")
        else:
            recent_rows: list[dict] = []
            snapshot_payload_by_id: dict[int, dict] = {}
            for row in parity_snapshot_rows:
                payload = _audit_changes(row)
                snapshot_payload_by_id[int(row.id)] = payload if isinstance(payload, dict) else {}
                workflows = payload.get("workflows") if isinstance(payload, dict) else []
                recent_rows.append(
                    {
                        "id": row.id,
                        "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
                        "actor": row.actor,
                        "environment": str(payload.get("environment") or ""),
                        "lookback_days": int(payload.get("lookback_days") or 0),
                        "workflow_count": int(payload.get("workflow_count") or 0),
                        "permission_gap_count": int(payload.get("permission_gap_count") or 0),
                        "audit_gap_count": int(payload.get("audit_gap_count") or 0),
                        "workflows_json_len": len(workflows) if isinstance(workflows, list) else 0,
                    }
                )
            recent_df = pd.DataFrame(recent_rows)
            st.dataframe(recent_df, use_container_width=True, hide_index=True)

            snapshot_option_map = {
                (
                    f"#{r['id']} | {r['created_at']} | env={r['environment']} | "
                    f"perm_gap={r['permission_gap_count']} | audit_gap={r['audit_gap_count']}"
                ): r["id"]
                for r in recent_rows
            }
            selected_snapshot_label = st.selectbox(
                "Inspect Snapshot",
                options=list(snapshot_option_map.keys()),
                key="admin_parity_snapshot_inspect_select",
            )
            selected_snapshot_id = int(snapshot_option_map[selected_snapshot_label])
            selected_snapshot = next((r for r in parity_snapshot_rows if int(r.id) == selected_snapshot_id), None)
            if selected_snapshot is not None:
                payload = selected_snapshot.changes if isinstance(selected_snapshot.changes, dict) else {}
                workflow_rows = payload.get("workflows") if isinstance(payload, dict) else []
                if isinstance(workflow_rows, list) and workflow_rows:
                    st.caption("Snapshot workflow details")
                    st.dataframe(pd.DataFrame(workflow_rows), use_container_width=True, hide_index=True)
                with st.expander("Snapshot Raw Payload", expanded=False):
                    st.json(payload)

                st.markdown("#### Release Decision")
                with st.form("admin_parity_release_decision_form"):
                    d1, d2 = st.columns([1, 2])
                    with d1:
                        decision = st.selectbox(
                            "Decision",
                            options=["approved", "rejected"],
                            key="admin_parity_release_decision_value",
                        )
                    with d2:
                        decision_note = st.text_input(
                            "Decision Note (optional)",
                            key="admin_parity_release_decision_note",
                            placeholder="Reason, blocker, or follow-up action.",
                        )
                    submit_decision = st.form_submit_button("Record Release Decision")
                if submit_decision:
                    try:
                        repo.record_audit_event(
                            entity_type="workspace_parity_decision",
                            entity_id=int(selected_snapshot.id),
                            action="decision",
                            actor=user.username,
                            changes={
                                "snapshot_id": int(selected_snapshot.id),
                                "decision": decision,
                                "note": (decision_note or "").strip(),
                                "environment": settings.app_env,
                                "snapshot_created_at": selected_snapshot.created_at.isoformat(timespec="seconds")
                                if selected_snapshot.created_at
                                else "",
                            },
                        )
                        if get_runtime_bool(repo, "slack_notify_parity_decisions", False):
                            text = build_slack_alert_text(
                                repo,
                                event_type="parity_decision",
                                default_template=(
                                    ":clipboard: *GoldenStackers* parity release decision `{decision}`\n"
                                    "- Env: `{env}`\n"
                                    "- Snapshot: `#{snapshot_id}`\n"
                                    "- Actor: `{actor}`\n"
                                    "- Note: `{note}`"
                                ),
                                context={
                                    "decision": decision,
                                    "snapshot_id": int(selected_snapshot.id),
                                    "actor": user.username,
                                    "note": (decision_note or "").strip() or "(none)",
                                },
                            )
                            dispatch_slack_alert(
                                repo,
                                actor=user.username,
                                text=text,
                                event_type="parity_decision",
                                severity="warning" if decision == "rejected" else "info",
                            )
                        st.success(f"Recorded release decision `{decision}` for snapshot #{selected_snapshot.id}.")
                    except Exception as exc:
                        st.error(f"Unable to record release decision: {exc}")

                st.markdown("#### Create Follow-up Task")
                with st.form("admin_parity_followup_create_form"):
                    f1, f2 = st.columns(2)
                    with f1:
                        followup_title = st.text_input(
                            "Task Title",
                            key="admin_parity_followup_title",
                            placeholder="Example: fix missing shipping parity evidence",
                        )
                        followup_owner = st.text_input(
                            "Owner",
                            key="admin_parity_followup_owner",
                            value=user.username,
                        )
                    with f2:
                        followup_due_date = st.date_input(
                            "Due Date (optional)",
                            key="admin_parity_followup_due_date",
                            value=utcnow_naive().date(),
                        )
                        followup_priority = st.selectbox(
                            "Priority",
                            options=["low", "medium", "high", "critical"],
                            index=1,
                            key="admin_parity_followup_priority",
                        )
                    followup_note = st.text_area(
                        "Task Notes (optional)",
                        key="admin_parity_followup_note",
                        placeholder="Context, acceptance criteria, or links.",
                    )
                    submit_followup = st.form_submit_button("Create Follow-up Task")
                if submit_followup:
                    if not str(followup_title or "").strip():
                        st.error("Task title is required.")
                    else:
                        try:
                            task_key = f"snapshot-{int(selected_snapshot.id)}-{utcnow_naive().strftime('%Y%m%d%H%M%S')}"
                            repo.record_audit_event(
                                entity_type="workspace_followup",
                                entity_id=int(selected_snapshot.id),
                                action="create",
                                actor=user.username,
                                changes={
                                    "task_key": task_key,
                                    "snapshot_id": int(selected_snapshot.id),
                                    "title": str(followup_title).strip(),
                                    "owner": str(followup_owner).strip() or user.username,
                                    "priority": str(followup_priority).strip().lower(),
                                    "due_date": followup_due_date.isoformat() if followup_due_date else "",
                                    "note": str(followup_note or "").strip(),
                                    "status": "open",
                                    "environment": settings.app_env,
                                },
                            )
                            st.success(f"Follow-up task created (`{task_key}`).")
                        except Exception as exc:
                            st.error(f"Unable to create follow-up task: {exc}")

            st.markdown("#### Compare Two Snapshots")
            if len(recent_rows) < 2:
                st.caption("Need at least 2 snapshots to compare.")
            else:
                snapshot_labels = [
                    (
                        f"#{r['id']} | {r['created_at']} | env={r['environment']} | "
                        f"perm_gap={r['permission_gap_count']} | audit_gap={r['audit_gap_count']}"
                    )
                    for r in recent_rows
                ]
                label_to_id = {label: int(recent_rows[idx]["id"]) for idx, label in enumerate(snapshot_labels)}
                dc1, dc2 = st.columns(2)
                with dc1:
                    baseline_label = st.selectbox(
                        "Baseline Snapshot",
                        options=snapshot_labels,
                        index=min(1, len(snapshot_labels) - 1),
                        key="admin_parity_compare_baseline",
                    )
                with dc2:
                    compare_label = st.selectbox(
                        "Compare Snapshot",
                        options=snapshot_labels,
                        index=0,
                        key="admin_parity_compare_target",
                    )
                baseline_id = label_to_id.get(baseline_label)
                compare_id = label_to_id.get(compare_label)
                if baseline_id == compare_id:
                    st.caption("Select two different snapshots to compute deltas.")
                else:
                    base_payload = snapshot_payload_by_id.get(int(baseline_id or 0), {})
                    cmp_payload = snapshot_payload_by_id.get(int(compare_id or 0), {})
                    base_perm_gap = int(base_payload.get("permission_gap_count") or 0)
                    cmp_perm_gap = int(cmp_payload.get("permission_gap_count") or 0)
                    base_audit_gap = int(base_payload.get("audit_gap_count") or 0)
                    cmp_audit_gap = int(cmp_payload.get("audit_gap_count") or 0)
                    dd1, dd2 = st.columns(2)
                    dd1.metric(
                        "Permission Gap Delta",
                        f"{cmp_perm_gap - base_perm_gap:+d}",
                        help=f"Baseline={base_perm_gap}, Compare={cmp_perm_gap}",
                    )
                    dd2.metric(
                        "Audit Gap Delta",
                        f"{cmp_audit_gap - base_audit_gap:+d}",
                        help=f"Baseline={base_audit_gap}, Compare={cmp_audit_gap}",
                    )

                    base_workflows = base_payload.get("workflows") if isinstance(base_payload, dict) else []
                    cmp_workflows = cmp_payload.get("workflows") if isinstance(cmp_payload, dict) else []
                    base_map = {
                        str(row.get("workflow") or ""): row
                        for row in base_workflows
                        if isinstance(row, dict) and str(row.get("workflow") or "").strip()
                    }
                    cmp_map = {
                        str(row.get("workflow") or ""): row
                        for row in cmp_workflows
                        if isinstance(row, dict) and str(row.get("workflow") or "").strip()
                    }
                    all_workflows = sorted(set(base_map.keys()) | set(cmp_map.keys()))
                    diff_rows: list[dict] = []
                    for wf in all_workflows:
                        b = base_map.get(wf, {})
                        c = cmp_map.get(wf, {})
                        b_perm_ok = bool(b.get("ops_has_permission", False)) and bool(
                            b.get("admin_has_permission", False)
                        )
                        c_perm_ok = bool(c.get("ops_has_permission", False)) and bool(
                            c.get("admin_has_permission", False)
                        )
                        b_audit_ok = bool(b.get("audit_observed_in_window", False))
                        c_audit_ok = bool(c.get("audit_observed_in_window", False))
                        diff_rows.append(
                            {
                                "workflow": wf,
                                "perm_ok_baseline": b_perm_ok,
                                "perm_ok_compare": c_perm_ok,
                                "perm_changed": b_perm_ok != c_perm_ok,
                                "audit_ok_baseline": b_audit_ok,
                                "audit_ok_compare": c_audit_ok,
                                "audit_changed": b_audit_ok != c_audit_ok,
                                "audit_match_count_delta": int(c.get("audit_match_count") or 0)
                                - int(b.get("audit_match_count") or 0),
                            }
                        )
                    if diff_rows:
                        diff_df = pd.DataFrame(diff_rows)
                        st.dataframe(diff_df, use_container_width=True, hide_index=True)

            decisions_df = pd.DataFrame()
            st.markdown("#### Recent Release Decisions")
            decision_rows = repo.db.scalars(
                select(AuditLog)
                .where(
                    AuditLog.entity_type == "workspace_parity_decision",
                    AuditLog.action == "decision",
                )
                .order_by(AuditLog.created_at.desc())
                .limit(50)
            ).all()
            latest_snapshot_row = parity_snapshot_rows[0] if parity_snapshot_rows else None
            latest_decision_row = decision_rows[0] if decision_rows else None
            latest_approved_row = next(
                (
                    row
                    for row in decision_rows
                    if str((_audit_changes(row).get("decision") or "")).strip().lower() == "approved"
                ),
                None,
            )
            status_cols = st.columns(3)
            with status_cols[0]:
                latest_decision = (
                    str((_audit_changes(latest_decision_row).get("decision") or "none")).strip().lower()
                    if latest_decision_row
                    else "none"
                )
                st.metric("Latest Decision", latest_decision)
            with status_cols[1]:
                st.metric(
                    "Latest Approved Snapshot",
                    f"#{latest_approved_row.entity_id}" if latest_approved_row and latest_approved_row.entity_id else "none",
                )
            with status_cols[2]:
                stale_days = 999
                if latest_approved_row and latest_approved_row.created_at:
                    stale_days = int((utcnow_naive() - latest_approved_row.created_at).days)
                st.metric("Approved Snapshot Age (days)", "n/a" if stale_days == 999 else str(stale_days))

            if latest_decision_row is None:
                st.warning("No release decision recorded yet. Capture a parity decision before cutover.")
            else:
                latest_decision = str((_audit_changes(latest_decision_row).get("decision") or "")).strip().lower()
                decision_note = str((_audit_changes(latest_decision_row).get("note") or "")).strip()
                if latest_decision == "rejected":
                    st.error(
                        "Latest parity release decision is `rejected`. Resolve parity gaps and record a new decision before cutover."
                    )
                elif latest_decision == "approved":
                    st.success("Latest parity release decision is `approved`.")
                else:
                    st.info(f"Latest parity release decision: `{latest_decision}`")
                if decision_note:
                    st.caption(f"Latest decision note: {decision_note}")

            if latest_snapshot_row and latest_decision_row:
                latest_snapshot_id = int(latest_snapshot_row.id)
                latest_decision_snapshot_id = int(latest_decision_row.entity_id or 0)
                if latest_decision_snapshot_id != latest_snapshot_id:
                    st.warning(
                        "Latest snapshot does not have a matching release decision yet. "
                        "Record decision on current snapshot to keep go/no-go state current."
                    )

            if latest_approved_row and latest_approved_row.created_at:
                approved_age_days = int((utcnow_naive() - latest_approved_row.created_at).days)
                if approved_age_days >= 14:
                    st.warning(
                        f"Latest approved snapshot is {approved_age_days} day(s) old. "
                        "Re-run parity checks before release."
                    )

            if not decision_rows:
                st.caption("No release decisions recorded yet.")
            else:
                decisions_df = pd.DataFrame(
                    [
                        {
                            "id": row.id,
                            "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
                            "actor": row.actor,
                            "snapshot_id": row.entity_id,
                            "decision": _audit_changes(row).get("decision", ""),
                            "note": _audit_changes(row).get("note", ""),
                            "environment": _audit_changes(row).get("environment", ""),
                        }
                        for row in decision_rows
                    ]
                )
                st.dataframe(decisions_df, use_container_width=True, hide_index=True)

            display_df = pd.DataFrame()
            st.markdown("#### Follow-up Tasks")
            followup_rows = repo.db.scalars(
                select(AuditLog)
                .where(AuditLog.entity_type == "workspace_followup")
                .order_by(AuditLog.created_at.desc())
                .limit(500)
            ).all()
            if not followup_rows:
                st.caption("No follow-up tasks recorded yet.")
            else:
                created_by_key: dict[str, AuditLog] = {}
                resolved_keys: set[str] = set()
                overdue_alerted_keys: set[str] = set()
                for row in followup_rows:
                    payload = _audit_changes(row)
                    task_key = str(payload.get("task_key") or "").strip()
                    if not task_key:
                        continue
                    action = str(row.action or "").strip().lower()
                    if action == "create" and task_key not in created_by_key:
                        created_by_key[task_key] = row
                    if action in {"resolve", "closed"}:
                        resolved_keys.add(task_key)
                    if action == "overdue_alert":
                        overdue_alerted_keys.add(task_key)

                open_task_rows: list[dict] = []
                today = utcnow_naive().date()
                priority_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "": 4}
                for task_key, row in created_by_key.items():
                    payload = _audit_changes(row)
                    is_open = task_key not in resolved_keys
                    due_raw = str(payload.get("due_date") or "").strip()
                    due_dt = None
                    if due_raw:
                        try:
                            due_dt = datetime.fromisoformat(due_raw).date()
                        except Exception:
                            due_dt = None
                    due_in_days = (due_dt - today).days if due_dt is not None else None
                    sla_status = "none"
                    if is_open:
                        if due_in_days is None:
                            sla_status = "no_due_date"
                        elif due_in_days < 0:
                            sla_status = "overdue"
                        elif due_in_days <= 2:
                            sla_status = "due_soon"
                        else:
                            sla_status = "on_track"
                    open_task_rows.append(
                        {
                            "task_key": task_key,
                            "status": "open" if is_open else "resolved",
                            "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
                            "created_by": row.actor,
                            "snapshot_id": payload.get("snapshot_id", row.entity_id),
                            "title": payload.get("title", ""),
                            "owner": payload.get("owner", ""),
                            "priority": payload.get("priority", ""),
                            "due_date": payload.get("due_date", ""),
                            "due_in_days": due_in_days if due_in_days is not None else "",
                            "sla_status": sla_status,
                            "note": payload.get("note", ""),
                            "_priority_rank": priority_rank.get(str(payload.get("priority") or "").strip().lower(), 4),
                        }
                    )
                open_task_rows.sort(
                    key=lambda r: (
                        0 if str(r.get("status")) == "open" else 1,
                        0
                        if str(r.get("sla_status") or "") == "overdue"
                        else 1 if str(r.get("sla_status") or "") == "due_soon" else 2,
                        int(r.get("_priority_rank") or 4),
                        str(r.get("due_date") or ""),
                    )
                )
                status_opts = sorted({str(r.get("status") or "") for r in open_task_rows if str(r.get("status") or "")})
                owner_opts = sorted({str(r.get("owner") or "") for r in open_task_rows if str(r.get("owner") or "")})
                priority_opts = sorted(
                    {str(r.get("priority") or "") for r in open_task_rows if str(r.get("priority") or "")}
                )
                f1, f2, f3 = st.columns(3)
                with f1:
                    status_filter = st.multiselect(
                        "Status Filter",
                        options=status_opts,
                        default=["open"] if "open" in status_opts else status_opts,
                        key="admin_parity_followup_status_filter",
                    )
                with f2:
                    owner_filter = st.multiselect(
                        "Owner Filter",
                        options=owner_opts,
                        default=owner_opts,
                        key="admin_parity_followup_owner_filter",
                    )
                with f3:
                    priority_filter = st.multiselect(
                        "Priority Filter",
                        options=priority_opts,
                        default=priority_opts,
                        key="admin_parity_followup_priority_filter",
                    )
                filtered_followups = [
                    row
                    for row in open_task_rows
                    if (not status_filter or str(row.get("status") or "") in set(status_filter))
                    and (not owner_filter or str(row.get("owner") or "") in set(owner_filter))
                    and (not priority_filter or str(row.get("priority") or "") in set(priority_filter))
                ]
                followups_df = pd.DataFrame(filtered_followups)
                if not followups_df.empty:
                    m1, m2, m3 = st.columns(3)
                    m1.metric(
                        "Open Tasks",
                        int((followups_df["status"] == "open").sum()),
                    )
                    m2.metric(
                        "Overdue",
                        int(((followups_df["status"] == "open") & (followups_df["sla_status"] == "overdue")).sum()),
                    )
                    m3.metric(
                        "Due Soon (<=2d)",
                        int(((followups_df["status"] == "open") & (followups_df["sla_status"] == "due_soon")).sum()),
                    )
                display_df = followups_df.drop(columns=["_priority_rank"], errors="ignore")
                st.dataframe(display_df, use_container_width=True, hide_index=True)
                st.download_button(
                    "Download Follow-up Tasks CSV",
                    data=display_df.to_csv(index=False).encode("utf-8"),
                    file_name=(
                        f"workspace_followup_tasks_{settings.app_env}_"
                        f"{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.csv"
                    ),
                    mime="text/csv",
                    key="admin_parity_followup_csv_btn",
                    disabled=display_df.empty,
                )

                open_rows = [r for r in open_task_rows if str(r.get("status")) == "open"]
                if open_rows:
                    overdue_open_rows = [
                        r for r in open_rows if str(r.get("sla_status") or "") == "overdue"
                    ]
                    if overdue_open_rows:
                        send_overdue_btn = st.button(
                            "Send Overdue Alerts Now",
                            key="admin_parity_followup_send_overdue_btn",
                            help="Sends alerts for overdue tasks that have not yet received an overdue alert event.",
                        )
                        if send_overdue_btn:
                            if not get_runtime_bool(repo, "slack_notify_followup_overdue", False):
                                st.warning(
                                    "Overdue Slack alerts are disabled (`slack_notify_followup_overdue=false`)."
                                )
                            else:
                                sent_count = 0
                                queued_count = 0
                                skipped_count = 0
                                for task in overdue_open_rows:
                                    task_key = str(task.get("task_key") or "").strip()
                                    if not task_key or task_key in overdue_alerted_keys:
                                        skipped_count += 1
                                        continue
                                    try:
                                        text = build_slack_alert_text(
                                            repo,
                                            event_type="followup_overdue",
                                            default_template=(
                                                ":rotating_light: *GoldenStackers* rollout follow-up overdue\n"
                                                "- Env: `{env}`\n"
                                                "- Task: `{task_key}`\n"
                                                "- Title: `{title}`\n"
                                                "- Owner: `{owner}`\n"
                                                "- Due: `{due_date}`\n"
                                                "- Priority: `{priority}`"
                                            ),
                                            context={
                                                "task_key": task_key,
                                                "title": str(task.get("title") or ""),
                                                "owner": str(task.get("owner") or ""),
                                                "due_date": str(task.get("due_date") or ""),
                                                "priority": str(task.get("priority") or ""),
                                            },
                                        )
                                        dispatch_result = dispatch_slack_alert(
                                            repo,
                                            actor=user.username,
                                            text=text,
                                            event_type="followup_overdue",
                                            severity="warning",
                                        )
                                        repo.record_audit_event(
                                            entity_type="workspace_followup",
                                            entity_id=int(task.get("snapshot_id") or 0) or None,
                                            action="overdue_alert",
                                            actor=user.username,
                                            changes={
                                                "task_key": task_key,
                                                "status": str(dispatch_result.get("status") or ""),
                                                "queue_job_id": dispatch_result.get("queue_job_id"),
                                                "channel": dispatch_result.get("channel", ""),
                                                "environment": settings.app_env,
                                            },
                                        )
                                        if str(dispatch_result.get("status") or "") == "queued":
                                            queued_count += 1
                                        else:
                                            sent_count += 1
                                    except Exception:
                                        skipped_count += 1
                                st.success(
                                    f"Overdue alerts processed. sent={sent_count}, queued={queued_count}, skipped={skipped_count}."
                                )
                                st.rerun()
                    open_map = {
                        f"{r['task_key']} | owner={r['owner']} | priority={r['priority']} | due={r['due_date']}": r
                        for r in open_rows
                    }
                    selected_followup_label = st.selectbox(
                        "Resolve Follow-up Task",
                        options=list(open_map.keys()),
                        key="admin_parity_followup_resolve_select",
                    )
                    resolve_note = st.text_input(
                        "Resolution Note (optional)",
                        key="admin_parity_followup_resolve_note",
                        placeholder="What changed to close this blocker?",
                    )
                    if st.button("Mark Follow-up Resolved", key="admin_parity_followup_resolve_btn"):
                        try:
                            selected_task = open_map[selected_followup_label]
                            repo.record_audit_event(
                                entity_type="workspace_followup",
                                entity_id=int(selected_task.get("snapshot_id") or 0) or None,
                                action="resolve",
                                actor=user.username,
                                changes={
                                    "task_key": selected_task.get("task_key"),
                                    "resolution_note": str(resolve_note or "").strip(),
                                    "resolved_at": utcnow_naive().isoformat(timespec="seconds"),
                                    "status": "resolved",
                                    "environment": settings.app_env,
                                },
                            )
                            st.success(f"Marked follow-up `{selected_task.get('task_key')}` as resolved.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Unable to resolve follow-up: {exc}")

            st.markdown("#### Parity Governance Bundle")
            parity_bundle_buffer = BytesIO()
            with zipfile.ZipFile(parity_bundle_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as bundle_zip:
                readiness_summary_df = pd.DataFrame(
                    [
                        {
                            "environment": settings.app_env,
                            "lookback_days": int(lookback_days),
                            "readiness_score": int(score),
                            "readiness_status": str(readiness),
                            "permission_gap_count": int(len(permission_gap_df)),
                            "audit_gap_count": int(len(audit_gap_df)),
                            "open_followups_count": int(open_followups_count),
                            "overdue_followups_count": int(overdue_followups_count),
                            "generated_at_utc": utcnow_naive().isoformat(),
                        }
                    ]
                )
                bundle_zip.writestr("parity_readiness_summary.csv", readiness_summary_df.to_csv(index=False))
                parity_export_df = parity_df.copy()
                parity_export_df.insert(0, "environment", settings.app_env)
                bundle_zip.writestr("parity_workflows.csv", parity_export_df.to_csv(index=False))
                perm_gap_export_df = permission_gap_df.copy()
                if not perm_gap_export_df.empty:
                    perm_gap_export_df.insert(0, "environment", settings.app_env)
                bundle_zip.writestr("parity_permission_gaps.csv", perm_gap_export_df.to_csv(index=False))
                audit_gap_export_df = audit_gap_df.copy()
                if not audit_gap_export_df.empty:
                    audit_gap_export_df.insert(0, "environment", settings.app_env)
                bundle_zip.writestr("parity_audit_gaps.csv", audit_gap_export_df.to_csv(index=False))
                snapshot_export_df = recent_df.copy()
                if not snapshot_export_df.empty:
                    snapshot_export_df.insert(0, "environment", settings.app_env)
                bundle_zip.writestr("parity_recent_snapshots.csv", snapshot_export_df.to_csv(index=False))
                decision_export_df = decisions_df.copy()
                if not decision_export_df.empty:
                    decision_export_df.insert(0, "environment", settings.app_env)
                bundle_zip.writestr("parity_release_decisions.csv", decision_export_df.to_csv(index=False))
                followup_export_df = display_df.copy()
                if not followup_export_df.empty:
                    followup_export_df.insert(0, "environment", settings.app_env)
                bundle_zip.writestr("parity_followup_tasks_filtered.csv", followup_export_df.to_csv(index=False))
            parity_bundle_buffer.seek(0)
            st.download_button(
                "Export Parity Governance Bundle (ZIP)",
                data=parity_bundle_buffer.getvalue(),
                file_name=(
                    f"parity_governance_bundle_{settings.app_env}_"
                    f"{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.zip"
                ),
                mime="application/zip",
                key="admin_parity_governance_bundle_zip_btn",
            )
