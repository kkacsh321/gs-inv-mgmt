from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.db.models import AuditLog, Product, Sale
from app.services.ai_accountant_identity import (
    AI_ACCOUNTANT_LABEL,
    AI_ACCOUNTANT_NAME,
    DEFAULT_AI_ACCOUNTANT_MONITOR_INSTRUCTION,
    DEFAULT_AI_ACCOUNTANT_SYSTEM_MESSAGE,
)
from app.services.llm_runtime import describe_llm_runtime_chain
from app.services.runtime_settings import get_runtime_bool, get_runtime_int, get_runtime_str
from app.utils.time import utcnow_naive


ACCOUNTING_SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}

ACCOUNTING_TASK_PRIORITY = {
    "missing_cost_basis": 0,
    "missing_product_link": 1,
    "dashboard_profit_basis_review": 2,
    "nonpositive_margin": 3,
    "listing_lot_inventory_movement_mismatch": 4,
    "lot_overallocated": 4,
    "lot_underallocated": 6,
    "lot_equal_fallback_review_needed": 7,
    "lot_allocation_pending_check_in": 8,
    "blank_lot_assignment_without_lot_total": 9,
    "missing_fee_evidence": 10,
    "fee_source_fallback": 11,
    "missing_shipping_label_spend": 12,
    "unmatched_shipping_label_finance_entry": 13,
    "dashboard_return_profit_impact_review": 14,
    "active_bundle_listing_stock_shortage": 15,
    "active_bundle_component_overcommitted": 16,
    "ai_accountant_review_followup": 17,
    "ai_accountant_answer_followup": 18,
}

AI_ACCOUNTANT_ANSWER_RE = re.compile(
    r"^\s*(?:(?:ai[-_\s]+)?accountant|goldie)\s+answer\s+"
    r"(?P<task_type>[a-z0-9_:-]+)\s+"
    r"(?P<reference>[a-z0-9_:-]+(?:#[0-9]+)?)\s*:\s*"
    r"(?P<answer>.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def _accounting_task_priority(row: dict[str, Any]) -> int:
    task_type = str(row.get("task_type") or row.get("exception_type") or "").strip().lower()
    return ACCOUNTING_TASK_PRIORITY.get(task_type, 99)


def _accounting_monitor_sort_key(row: dict[str, Any]) -> tuple[int, int, float, str, int]:
    return (
        ACCOUNTING_SEVERITY_ORDER.get(str(row.get("severity") or "").strip().upper(), 99),
        _accounting_task_priority(row),
        -abs(_safe_float(row.get("amount"))),
        str(row.get("occurred_at") or ""),
        _safe_int(row.get("entity_id")),
    )


def _recommended_accounting_action(exception_type: str) -> str:
    key = str(exception_type or "").strip().lower()
    if key in {"missing_cost_basis", "blank_lot_assignment_without_lot_total"}:
        return "Add product landed cost, lot landed total, or product-lot assignment cost evidence."
    if key in {"lot_equal_fallback_review_needed", "lot_allocation_pending_check_in"}:
        return "Set expected lot quantity, allocation weights, or explicit assignment costs for the lot."
    if key in {"missing_shipping_label_spend", "unmatched_shipping_label_finance_entry"}:
        return "Import/link shipping-label finance entries to the matching order or sale."
    if key in {"missing_fee_evidence", "fee_source_fallback"}:
        return "Import/link marketplace fee evidence from normalized order finance entries."
    if key == "nonpositive_margin":
        return "Review sale price, fees, label spend, returns, and FIFO COGS basis before close sign-off."
    if key == "dashboard_return_profit_impact_review":
        return "Review refund totals, returned listing/inventory units, restock status, and COGS reversal evidence."
    if key == "missing_product_link":
        return "Link the sale to the correct product so FIFO COGS can be proven."
    if key == "listing_lot_inventory_movement_mismatch":
        return "Reconcile the sale inventory movement to the inferred lot quantity, then verify FIFO COGS and stock."
    if key == "active_bundle_listing_stock_shortage":
        return "Reduce/end the active bundle listing quantity or restock the short bundle component inventory."
    if key == "active_bundle_component_overcommitted":
        return "Reduce/end overlapping active bundle listings or restock the overcommitted component inventory."
    if key in {"lot_overallocated", "lot_underallocated"}:
        return "Reconcile lot landed total against explicit assignment landed totals."
    return "Review source evidence and resolve before accounting close sign-off."


def _monitor_key(row: dict[str, Any]) -> str:
    return "|".join(
        [
            str(row.get("severity") or ""),
            str(row.get("exception_type") or row.get("task_type") or ""),
            str(row.get("entity_type") or ""),
            str(row.get("entity_id") or ""),
            str(row.get("reference") or ""),
        ]
    )


def _audit_changes(row: Any) -> dict[str, Any]:
    changes = getattr(row, "changes", None)
    if isinstance(changes, dict):
        return changes
    raw = str(getattr(row, "changes_json", "") or "{}")
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def parse_ai_accountant_answer_prompt(prompt: str) -> dict[str, Any] | None:
    """Parse operator replies generated from Goldie question prompts."""
    raw = str(prompt or "").strip()
    if not raw:
        return None
    match = AI_ACCOUNTANT_ANSWER_RE.match(raw)
    if not match:
        return None
    task_type = str(match.group("task_type") or "").strip().lower()
    reference = str(match.group("reference") or "").strip().lower()
    answer_text = str(match.group("answer") or "").strip()
    if not task_type or not reference or not answer_text:
        return None
    entity_type = ""
    entity_id: int | None = None
    if "#" in reference:
        raw_entity_type, raw_entity_id = reference.rsplit("#", 1)
        entity_type = raw_entity_type.strip().lower()
        try:
            entity_id = int(raw_entity_id)
        except Exception:
            entity_id = None
    return {
        "task_type": task_type,
        "reference": reference,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "answer_text": answer_text,
        "answer_hash_sha256": stable_ai_accountant_hash(answer_text),
        "raw_prompt": raw[:2000],
    }


def ai_accountant_answer_row(row: Any) -> dict[str, Any] | None:
    payload = _audit_changes(row)
    if not isinstance(payload, dict):
        return None
    answer_text = str(payload.get("answer_text") or "").strip()
    task_type = str(payload.get("task_type") or "").strip().lower()
    reference = str(payload.get("reference") or "").strip().lower()
    if not answer_text or not task_type:
        return None
    return {
        "recorded_at": getattr(row, "created_at", None),
        "actor": str(getattr(row, "actor", "") or ""),
        "source": str(payload.get("source") or "").strip(),
        "task_type": task_type,
        "reference": reference,
        "entity_type": str(payload.get("entity_type") or "").strip().lower(),
        "entity_id": payload.get("entity_id"),
        "answer_preview": answer_text[:260],
        "answer_hash_sha256": str(payload.get("answer_hash_sha256") or "").strip(),
    }


def ai_accountant_answer_followup_row(row: Any) -> dict[str, Any] | None:
    payload = _audit_changes(row)
    if not isinstance(payload, dict):
        return None
    answer_hash = str(payload.get("answer_hash_sha256") or "").strip()
    outcome = str(payload.get("outcome") or "").strip().lower()
    if not answer_hash or not outcome:
        return None
    return {
        "recorded_at": getattr(row, "created_at", None),
        "actor": str(getattr(row, "actor", "") or ""),
        "answer_hash_sha256": answer_hash,
        "outcome": outcome,
        "notes": str(payload.get("notes") or "").strip()[:500],
    }


def list_ai_accountant_answer_followups(repo: Any, *, limit: int = 100) -> list[dict[str, Any]]:
    db = getattr(repo, "db", None)
    if db is None:
        return []
    try:
        rows = db.scalars(
            select(AuditLog)
            .where(AuditLog.entity_type == "ai_accountant_answer_followup")
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(max(1, int(limit or 100)))
        ).all()
    except Exception:
        if hasattr(db, "rollback"):
            db.rollback()
        return []
    parsed = [ai_accountant_answer_followup_row(row) for row in rows]
    return [row for row in parsed if row is not None]


def list_ai_accountant_answers(repo: Any, *, limit: int = 25) -> list[dict[str, Any]]:
    db = getattr(repo, "db", None)
    if db is None:
        return []
    try:
        rows = db.scalars(
            select(AuditLog)
            .where(AuditLog.entity_type == "ai_accountant_answer")
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(max(1, int(limit or 25)))
        ).all()
    except Exception:
        if hasattr(db, "rollback"):
            db.rollback()
        return []
    parsed = [row for row in (ai_accountant_answer_row(row) for row in rows) if row is not None]
    followups = list_ai_accountant_answer_followups(repo, limit=max(100, int(limit or 25) * 4))
    latest_by_hash: dict[str, dict[str, Any]] = {}
    for followup in followups:
        answer_hash = str(followup.get("answer_hash_sha256") or "").strip()
        if answer_hash:
            latest_by_hash.setdefault(answer_hash, followup)
    out: list[dict[str, Any]] = []
    for row in parsed:
        followup = latest_by_hash.get(str(row.get("answer_hash_sha256") or ""))
        enriched = dict(row)
        enriched["followup_status"] = str(followup.get("outcome") or "unreviewed") if followup else "unreviewed"
        enriched["followup_at"] = followup.get("recorded_at") if followup else ""
        enriched["followup_actor"] = followup.get("actor") if followup else ""
        enriched["followup_notes"] = followup.get("notes") if followup else ""
        out.append(enriched)
    return out


def record_ai_accountant_answer_followup(
    repo: Any,
    *,
    actor: str,
    answer_hash_sha256: str,
    outcome: str,
    notes: str = "",
) -> dict[str, Any] | None:
    answer_hash = str(answer_hash_sha256 or "").strip()
    clean_outcome = str(outcome or "").strip().lower()
    if clean_outcome not in {"applied", "needs_more_info", "obsolete"}:
        clean_outcome = "needs_more_info"
    if not answer_hash:
        return None
    payload = {
        "event_type": "ai_accountant_answer_followup",
        "answer_hash_sha256": answer_hash,
        "outcome": clean_outcome,
        "notes": str(notes or "").strip()[:1000],
        "read_only": True,
        "correction_applied_through_normal_workflow": clean_outcome == "applied",
    }
    if hasattr(repo, "record_audit_event"):
        try:
            repo.record_audit_event(
                entity_type="ai_accountant_answer_followup",
                entity_id=None,
                action="record",
                actor=str(actor or "operator").strip() or "operator",
                changes=payload,
            )
        except Exception:
            db = getattr(repo, "db", None)
            if db is not None and hasattr(db, "rollback"):
                db.rollback()
    return payload


def ai_accountant_review_outcome_row(row: Any) -> dict[str, Any] | None:
    payload = _audit_changes(row)
    after = payload.get("after", {}) if isinstance(payload, dict) else {}
    if not isinstance(after, dict):
        return None
    metadata = after.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    event_type = str(metadata.get("event_type") or "").strip().lower()
    review_type = str(metadata.get("review_type") or "").strip().lower()
    intent = str(after.get("intent") or "").strip().lower()
    if event_type != "ai_accountant_review_outcome" and review_type != "ai_accountant_review":
        if intent != "ai_accountant_review_outcome":
            return None
    data_scope = metadata.get("data_scope") if isinstance(metadata.get("data_scope"), dict) else {}
    row_counts = data_scope.get("row_counts") if isinstance(data_scope.get("row_counts"), dict) else {}
    return {
        "recorded_at": getattr(row, "created_at", None),
        "actor": str(getattr(row, "actor", "") or ""),
        "outcome": str(metadata.get("outcome") or "").strip().lower(),
        "surface": str(metadata.get("surface") or "").strip(),
        "monitor_rows": _safe_int(row_counts.get("monitor_rows")),
        "exception_rows": _safe_int(row_counts.get("accounting_exception_rows")),
        "fifo_evidence_rows": _safe_int(row_counts.get("sale_fifo_cogs_evidence_rows")),
        "evidence_packet_hash_sha256": str(metadata.get("evidence_packet_hash_sha256") or "").strip(),
        "evidence_packet_monitor_rows": _safe_int(
            (metadata.get("evidence_packet_row_counts") or {}).get("monitor_rows")
            if isinstance(metadata.get("evidence_packet_row_counts"), dict)
            else 0
        ),
        "evidence_packet_exception_rows": _safe_int(
            (metadata.get("evidence_packet_row_counts") or {}).get("accounting_exception_rows")
            if isinstance(metadata.get("evidence_packet_row_counts"), dict)
            else 0
        ),
        "evidence_packet_fifo_rows": _safe_int(
            (metadata.get("evidence_packet_row_counts") or {}).get("sale_fifo_cogs_evidence")
            if isinstance(metadata.get("evidence_packet_row_counts"), dict)
            else 0
        ),
        "evidence_packet_integrity_status": str(
            metadata.get("evidence_packet_integrity_status") or ""
        ).strip(),
        "evidence_packet_integrity_errors": "; ".join(
            str(item) for item in (metadata.get("evidence_packet_integrity_errors") or [])
        )[:500],
        "evidence_packet_integrity_error_count": _safe_int(
            metadata.get("evidence_packet_integrity_error_count")
        ),
        "evidence_packet_manifest_status": str(
            metadata.get("evidence_packet_manifest_status") or ""
        ).strip(),
        "evidence_packet_manifest_rows": _safe_int(metadata.get("evidence_packet_manifest_row_count")),
        "evidence_packet_manifest_expected_rows": _safe_int(
            metadata.get("evidence_packet_manifest_expected_row_count")
        ),
        "evidence_packet_action_summary_task_counts": json.dumps(
            metadata.get("evidence_packet_action_summary_task_counts") or {},
            sort_keys=True,
            default=str,
        )[:500],
        "prompt_hash_sha256": str(metadata.get("prompt_hash_sha256") or "").strip(),
        "data_scope_hash_sha256": str(metadata.get("data_scope_hash_sha256") or "").strip(),
        "answer_hash_sha256": str(metadata.get("answer_hash_sha256") or "").strip(),
        "answer_preview": str(after.get("answer_preview") or "")[:220],
    }


def list_ai_accountant_review_outcomes(repo: Any, *, limit: int = 25) -> list[dict[str, Any]]:
    db = getattr(repo, "db", None)
    if db is None:
        return []
    try:
        rows = db.scalars(
            select(AuditLog)
            .where(AuditLog.entity_type == "ai_chat")
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(max(1, int(limit or 25)))
        ).all()
    except Exception:
        if hasattr(db, "rollback"):
            db.rollback()
        return []
    outcomes = [ai_accountant_review_outcome_row(row) for row in rows]
    return [row for row in outcomes if row is not None]


def summarize_ai_accountant_review_outcomes(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    sorted_outcomes = sorted(
        [row for row in outcomes or [] if str(row.get("outcome") or "").strip()],
        key=lambda row: str(row.get("recorded_at") or ""),
        reverse=True,
    )
    latest = sorted_outcomes[0] if sorted_outcomes else None
    if not latest:
        return {
            "latest_outcome": "none",
            "status": "No review outcome recorded",
            "needs_followup": False,
            "recorded_at": "",
            "actor": "",
        }
    outcome = str(latest.get("outcome") or "").strip().lower()
    needs_followup = outcome in {"edited", "rejected"}
    if outcome == "accepted":
        status = f"Latest {AI_ACCOUNTANT_NAME} review was accepted."
    elif outcome == "edited":
        status = f"Latest {AI_ACCOUNTANT_NAME} review needs edits before close sign-off."
    elif outcome == "rejected":
        status = f"Latest {AI_ACCOUNTANT_NAME} review was rejected and needs follow-up."
    else:
        status = f"Latest {AI_ACCOUNTANT_NAME} review outcome is `{outcome or 'unknown'}`."
    return {
        "latest_outcome": outcome or "unknown",
        "status": status,
        "needs_followup": needs_followup,
        "recorded_at": latest.get("recorded_at") or "",
        "actor": str(latest.get("actor") or ""),
        "answer_hash_sha256": str(latest.get("answer_hash_sha256") or ""),
    }


def build_ai_accountant_review_followup_rows(outcomes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary = summarize_ai_accountant_review_outcomes(outcomes)
    if not summary.get("needs_followup"):
        return []
    outcome = str(summary.get("latest_outcome") or "").strip().lower()
    actor = str(summary.get("actor") or "").strip()
    answer_hash = str(summary.get("answer_hash_sha256") or "").strip()
    return [
        {
            "severity": "P0" if outcome == "rejected" else "P1",
            "task_type": "ai_accountant_review_followup",
            "entity_type": "ai_chat",
            "entity_id": 0,
            "sku": "",
            "reference": answer_hash[:12] or outcome,
            "amount": None,
            "details": (
                f"Latest {AI_ACCOUNTANT_NAME} review outcome is `{outcome}`"
                f"{f' by {actor}' if actor else ''}; follow-up is required before close sign-off."
            ),
            "occurred_at": str(summary.get("recorded_at") or ""),
            "recommended_action": (
                f"Resolve {AI_ACCOUNTANT_NAME} review feedback, rerun or update the review, then record an accepted outcome."
            ),
            "source": "ai_accountant_review_outcomes",
        }
    ]


def build_ai_accountant_answer_followup_monitor_rows(answer_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    replacement_references: set[str] = set()
    for row in answer_rows or []:
        task_type = str(row.get("task_type") or "").strip().lower()
        status = str(row.get("followup_status") or "unreviewed").strip().lower()
        reference = str(row.get("reference") or "").strip().lower()
        if task_type == "ai_accountant_answer_followup" and reference and status not in {"needs_more_info", "obsolete"}:
            replacement_references.add(reference)
    for row in answer_rows or []:
        status = str(row.get("followup_status") or "unreviewed").strip().lower()
        if status not in {"needs_more_info", "obsolete"}:
            continue
        task_type = str(row.get("task_type") or "accounting_review").strip()
        reference = str(row.get("reference") or "").strip()
        if reference.strip().lower() in replacement_references:
            continue
        answer_hash = str(row.get("answer_hash_sha256") or "").strip()
        actor = str(row.get("actor") or "").strip()
        rows.append(
            {
                "severity": "P1" if status == "needs_more_info" else "P2",
                "task_type": "ai_accountant_answer_followup",
                "entity_type": str(row.get("entity_type") or "ai_accountant_answer").strip()
                or "ai_accountant_answer",
                "entity_id": row.get("entity_id") or 0,
                "sku": "",
                "reference": reference or answer_hash[:12] or status,
                "amount": None,
                "details": (
                    f"Answer for `{task_type}` `{reference or answer_hash[:12]}` is marked `{status}`"
                    f"{f' by {actor}' if actor else ''}; the accounting issue still needs follow-up."
                ),
                "occurred_at": str(row.get("followup_at") or row.get("recorded_at") or ""),
                "recommended_action": (
                    "Collect additional evidence and record a replacement answer."
                    if status == "needs_more_info"
                    else "Record a replacement answer or mark the underlying issue resolved through normal workflow."
                ),
                "source": "ai_accountant_answers",
            }
        )
    return rows


def build_ai_accountant_monitor_rows(
    exception_rows: list[dict[str, Any]],
    *,
    dashboard_metrics: dict[str, Any] | None = None,
    review_outcome_rows: list[dict[str, Any]] | None = None,
    answer_rows: list[dict[str, Any]] | None = None,
    max_rows: int = 200,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in exception_rows or []:
        exception_type = str(row.get("exception_type") or "accounting_review").strip()
        rows.append(
            {
                "severity": str(row.get("severity") or "P2").strip().upper() or "P2",
                "task_type": exception_type,
                "entity_type": str(row.get("entity_type") or "").strip(),
                "entity_id": _safe_int(row.get("entity_id")),
                "sku": str(row.get("sku") or ""),
                "reference": str(row.get("reference") or ""),
                "amount": row.get("amount"),
                "details": str(row.get("details") or "").strip(),
                "occurred_at": str(row.get("occurred_at") or ""),
                "recommended_action": _recommended_accounting_action(exception_type),
                "source": "accounting_exception_queue",
            }
        )

    metrics = dashboard_metrics or {}
    profit_basis = str(metrics.get("sales_30d_profit_basis_status") or "").strip().lower()
    review_count = _safe_int(metrics.get("sales_30d_cogs_review_count"))
    review_amount = _safe_float(metrics.get("sales_30d_cogs_review_amount"))
    estimate_amount = _safe_float(metrics.get("sales_30d_cogs_estimate_amount"))
    verified_amount = _safe_float(metrics.get("sales_30d_cogs_verified_amount"))
    review_sale_ids = [
        _safe_int(value)
        for value in list(metrics.get("sales_30d_cogs_review_sale_ids") or [])
        if _safe_int(value) > 0
    ]
    estimate_sale_ids = [
        _safe_int(value)
        for value in list(metrics.get("sales_30d_cogs_estimate_sale_ids") or [])
        if _safe_int(value) > 0
    ]
    bundle_sale_count = _safe_int(metrics.get("sales_30d_bundle_sale_count"))
    bundle_inventory_units_sold = _safe_int(metrics.get("sales_30d_bundle_inventory_units_sold"))
    returns_count = _safe_int(metrics.get("returns_30d_count"))
    returns_refund_total = _safe_float(metrics.get("returns_30d_refund_total"))
    returns_cogs_reversal = _safe_float(metrics.get("returns_30d_cogs_reversal"))
    returns_profit_impact = _safe_float(metrics.get("returns_30d_profit_impact"))
    profit_before_returns = _safe_float(metrics.get("sales_30d_profit_before_returns"))
    estimated_profit_after_returns = _safe_float(metrics.get("sales_30d_est_profit"))
    if profit_basis in {"review_needed", "partial_lot_estimate"} or review_count > 0:
        bundle_note = (
            f" Bundle accounting detected {bundle_sale_count} sale(s) consuming "
            f"{bundle_inventory_units_sold} inventory unit(s)."
            if bundle_sale_count > 0
            else ""
        )
        review_ids_note = (
            f" Review-needed sale IDs: {', '.join(str(value) for value in review_sale_ids[:12])}."
            if review_sale_ids
            else ""
        )
        estimate_ids_note = (
            f" Estimate-basis sale IDs: {', '.join(str(value) for value in estimate_sale_ids[:12])}."
            if estimate_sale_ids
            else ""
        )
        rows.append(
            {
                "severity": "P1" if profit_basis == "review_needed" else "P2",
                "task_type": "dashboard_profit_basis_review",
                "entity_type": "dashboard",
                "entity_id": 0,
                "sku": "",
                "reference": "30d_profit_after_returns",
                "amount": estimated_profit_after_returns,
                "details": (
                    f"Dashboard 30-day profit basis status is `{profit_basis or 'unknown'}`; "
                    f"{review_count} sale(s) need COGS basis review. "
                    f"COGS evidence split: verified ${verified_amount:,.2f}, "
                    f"estimated ${estimate_amount:,.2f}, needs review ${review_amount:,.2f}. "
                    f"Profit before returns ${profit_before_returns:,.2f}; "
                    f"estimated profit after returns ${estimated_profit_after_returns:,.2f}."
                    f"{review_ids_note}{estimate_ids_note}{bundle_note}"
                ),
                "occurred_at": "",
                "recommended_action": (
                    "Review sold COGS source mix. For partial or mixed lots, set expected lot quantity, "
                    "allocation weights, or assignment-level costs before trusting profit. "
                    "For bundle listings, confirm component quantities and FIFO component COGS evidence."
                ),
                "source": "dashboard_live_metrics",
            }
        )
    if returns_count > 0 and (
        abs(returns_refund_total) > 0.005
        or abs(returns_cogs_reversal) > 0.005
        or abs(returns_profit_impact) > 0.005
    ):
        rows.append(
            {
                "severity": "P2",
                "task_type": "dashboard_return_profit_impact_review",
                "entity_type": "dashboard",
                "entity_id": 0,
                "sku": "",
                "reference": "30d_return_profit_impact",
                "amount": returns_profit_impact,
                "details": (
                    f"Dashboard return impact includes {returns_count} return(s), "
                    f"refund total ${returns_refund_total:,.2f}, "
                    f"COGS reversal ${returns_cogs_reversal:,.2f}, "
                    f"profit impact ${returns_profit_impact:,.2f}."
                ),
                "occurred_at": "",
                "recommended_action": _recommended_accounting_action("dashboard_return_profit_impact_review"),
                "source": "dashboard_live_metrics",
            }
        )

    rows.extend(build_ai_accountant_review_followup_rows(review_outcome_rows or []))
    rows.extend(build_ai_accountant_answer_followup_monitor_rows(answer_rows or []))

    deduped: dict[str, dict[str, Any]] = {}
    for row in rows:
        deduped.setdefault(_monitor_key(row), row)
    return sorted(deduped.values(), key=_accounting_monitor_sort_key)[: max(1, int(max_rows or 200))]


def build_ai_accountant_message(
    rows: list[dict[str, Any]],
    *,
    period_label: str,
    answer_rows: list[dict[str, Any]] | None = None,
    max_items: int = 6,
) -> str:
    p0 = sum(1 for row in rows if str(row.get("severity") or "").upper() == "P0")
    p1 = sum(1 for row in rows if str(row.get("severity") or "").upper() == "P1")
    p2 = sum(1 for row in rows if str(row.get("severity") or "").upper() == "P2")
    lines = [
        f"{AI_ACCOUNTANT_LABEL} monitor for {period_label}: {len(rows)} item(s) need review.",
        f"Severity mix: P0={p0}, P1={p1}, P2={p2}.",
    ]
    fee_evidence_exposure = sum(
        _safe_float(row.get("amount"))
        for row in rows
        if str(row.get("task_type") or row.get("exception_type") or "").strip()
        in {"missing_fee_evidence", "fee_source_fallback"}
    )
    shipping_evidence_exposure = sum(
        _safe_float(row.get("amount"))
        for row in rows
        if str(row.get("task_type") or row.get("exception_type") or "").strip()
        in {"missing_shipping_label_spend", "unmatched_shipping_label_finance_entry"}
    )
    if fee_evidence_exposure or shipping_evidence_exposure:
        lines.append(
            "Fee/shipping evidence exposure: "
            f"fee rows ${fee_evidence_exposure:,.2f}; "
            f"shipping-label rows ${shipping_evidence_exposure:,.2f}."
        )
    for row in rows[: max(1, int(max_items or 6))]:
        task_type = str(row.get("task_type") or "accounting_review")
        action = str(row.get("recommended_action") or "").strip()
        details = str(row.get("details") or "").strip()
        if task_type in {
            "dashboard_profit_basis_review",
            "missing_cost_basis",
            "nonpositive_margin",
            "lot_overallocated",
            "lot_underallocated",
        } and details:
            detail_preview = details[:220] + ("..." if len(details) > 220 else "")
            summary = f"{action} Evidence: {detail_preview}" if action else detail_preview
        else:
            summary = action or details
        lines.append(
            "- "
            f"[{str(row.get('severity') or 'P2')}] {task_type} "
            f"{str(row.get('entity_type') or '')}#{str(row.get('entity_id') or '')}: "
            f"{summary.strip()}"
        )
    if len(rows) > max_items:
        lines.append(f"- Plus {len(rows) - max_items} more item(s) in the {AI_ACCOUNTANT_NAME} workspace.")
    question_rows = annotate_ai_accountant_question_rows(
        build_ai_accountant_question_rows(rows, max_rows=6),
        answer_rows or [],
    )
    question_status_counts = build_ai_accountant_question_status_counts_from_rows(question_rows)
    if question_rows:
        status_order = ["unanswered", "needs_more_info", "obsolete", "answered", "applied"]
        status_parts = [
            f"{status}={question_status_counts[status]}"
            for status in status_order
            if question_status_counts.get(status)
        ]
        extra_statuses = sorted(set(question_status_counts) - set(status_order))
        status_parts.extend(f"{status}={question_status_counts[status]}" for status in extra_statuses)
        lines.append(f"Question status: {', '.join(status_parts)}.")
    resolved_statuses = {"answered", "applied"}
    unanswered_question_rows = [
        row for row in question_rows if str(row.get("answer_status") or "") not in resolved_statuses
    ][:3]
    answered_question_rows = [
        row for row in question_rows if str(row.get("answer_status") or "") in resolved_statuses
    ][:3]
    if unanswered_question_rows:
        lines.append("")
        lines.append("Questions to answer in Ask or Slack:")
        for row in unanswered_question_rows:
            evidence = str(row.get("evidence_preview") or "").strip()
            amount = row.get("amount")
            evidence_parts = []
            if amount is not None:
                evidence_parts.append(f"amount={_safe_float(amount):,.2f}")
            if evidence:
                evidence_parts.append(evidence[:180] + ("..." if len(evidence) > 180 else ""))
            evidence_text = f" Evidence: {' | '.join(evidence_parts)}." if evidence_parts else ""
            lines.append(f"- {row['question']}{evidence_text} Reply with: `{row['reply_prompt']}`")
    if answered_question_rows:
        lines.append("")
        lines.append(f"Recently answered {AI_ACCOUNTANT_NAME} questions:")
        for row in answered_question_rows:
            lines.append(
                f"- `{row.get('task_type')}` `{row.get('reference') or row.get('entity_type')}` "
                f"answered by `{row.get('latest_answer_actor') or 'unknown'}`: "
                f"{str(row.get('latest_answer_preview') or '')[:160]}"
            )
    return "\n".join(lines)


def build_ai_accountant_question_status_counts_from_rows(
    question_rows: list[dict[str, Any]],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in question_rows or []:
        status = str(row.get("answer_status") or "unanswered").strip().lower() or "unanswered"
        counts[status] = counts.get(status, 0) + 1
    return counts


def build_ai_accountant_question_status_counts(
    rows: list[dict[str, Any]],
    *,
    answer_rows: list[dict[str, Any]] | None = None,
    max_rows: int = 6,
) -> dict[str, int]:
    question_rows = annotate_ai_accountant_question_rows(
        build_ai_accountant_question_rows(rows, max_rows=max_rows),
        answer_rows or [],
    )
    return build_ai_accountant_question_status_counts_from_rows(question_rows)


def build_ai_accountant_question_rows(
    rows: list[dict[str, Any]],
    *,
    max_rows: int = 12,
) -> list[dict[str, Any]]:
    """Convert monitor findings into concrete operator questions.

    These are intentionally deterministic. The LLM can rephrase them, but the
    source question is tied to monitor evidence and remains read-only.
    """
    questions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in sorted(list(rows or []), key=_accounting_monitor_sort_key):
        severity = str(row.get("severity") or "P2").strip().upper() or "P2"
        task_type = str(row.get("task_type") or row.get("exception_type") or "accounting_review").strip()
        entity_type = str(row.get("entity_type") or "").strip() or "item"
        entity_id = str(row.get("entity_id") or "").strip()
        reference = str(row.get("reference") or row.get("sku") or "").strip()
        target = f"{entity_type}#{entity_id}" if entity_id and entity_id != "0" else (reference or entity_type)
        base_reply = f"accountant answer {task_type} {target}: "
        question = ""
        answer_format = ""
        why_needed = ""
        action = str(row.get("recommended_action") or "").strip()
        if task_type in {"missing_cost_basis", "blank_lot_assignment_without_lot_total"}:
            question = f"What cost-basis evidence should we use for {target}?"
            answer_format = (
                "sale/product/listing ID, lot total, assignment unit/allocated cost, product landed cost, "
                "or product_cost source"
            )
            why_needed = "FIFO COGS cannot be proven until cost basis evidence exists."
        elif task_type in {"missing_product_link"}:
            question = f"Which product should {target} be linked to?"
            answer_format = "SKU/product ID and why it matches the sale/order"
            why_needed = "The sale needs a product link before FIFO COGS can be traced."
        elif task_type in {"lot_equal_fallback_review_needed", "lot_allocation_pending_check_in"}:
            question = f"How should the lot cost be allocated for {target}?"
            answer_format = "expected lot quantity, allocation weights, or assignment-level costs"
            why_needed = "Equal or partial-lot fallback can overstate or understate sold COGS."
        elif task_type in {"lot_overallocated", "lot_underallocated"}:
            question = f"Which lot total or assignment amounts are correct for {target}?"
            answer_format = "correct landed lot total and per-product assignment totals"
            why_needed = "Lot total and assignment totals do not reconcile."
        elif task_type in {"missing_shipping_label_spend", "unmatched_shipping_label_finance_entry"}:
            question = f"Which shipping label/finance entry belongs to {target}?"
            answer_format = "carrier, tracking/label ID, amount, order/sale ID, purchase date"
            why_needed = "Profit needs actual label spend rather than fallback or missing shipping evidence."
        elif task_type in {"missing_fee_evidence", "fee_source_fallback"}:
            question = f"Which marketplace fee evidence should be linked to {target}?"
            answer_format = "order ID, fee total, fee source/export, and whether sale fallback is acceptable"
            why_needed = "Net sales and profit need normalized fee evidence."
        elif task_type == "nonpositive_margin":
            question = f"Is the nonpositive margin on {target} expected?"
            answer_format = "confirm sale price, fees, label spend, returns, and COGS basis"
            why_needed = "Negative or zero margin should be explained before close sign-off."
        elif task_type == "dashboard_profit_basis_review":
            question = "Which COGS source rows should we correct first for the dashboard profit drop?"
            answer_format = "sale IDs or lot IDs, expected quantity/weights/cost evidence, and priority"
            why_needed = "Dashboard profit is using review-needed COGS sources."
        elif task_type == "dashboard_return_profit_impact_review":
            question = "Are the return refund, restock status, and COGS reversal correct?"
            answer_format = "return IDs/sale IDs, refund amount, restocked yes/no, returned quantity"
            why_needed = "Return profit impact changes estimated profit after returns."
        elif task_type == "listing_lot_inventory_movement_mismatch":
            question = f"Should {target} consume the inferred lot quantity or the recorded movement quantity?"
            answer_format = "sale/listing ID, correct inventory units consumed, and whether to adjust movements or listing metadata"
            why_needed = "Legacy lot-listing sales can leave stock and FIFO COGS out of sync if only one unit was consumed."
        elif task_type in {"active_bundle_listing_stock_shortage", "active_bundle_component_overcommitted"}:
            question = f"Should we reduce listing quantity or restock inventory for {target}?"
            answer_format = "listing IDs/SKUs, desired active quantity, or restock plan"
            why_needed = "Bundle listings may oversell component inventory and distort COGS planning."
        elif task_type == "ai_accountant_review_followup":
            question = f"What follow-up is needed for the latest {AI_ACCOUNTANT_NAME} review outcome?"
            answer_format = "accept, edit with corrected note, or reject with reason"
            why_needed = "Close readiness remains blocked until the review outcome is resolved."
        elif task_type == "ai_accountant_answer_followup":
            question = f"What replacement answer or evidence resolves the open {AI_ACCOUNTANT_NAME} answer follow-up for {target}?"
            answer_format = "replacement answer, evidence source, normal workflow correction made, or why obsolete"
            why_needed = (
                f"A prior {AI_ACCOUNTANT_NAME} answer was marked needs-more-info or obsolete, "
                "so the accounting issue should remain open until replacement evidence exists."
            )
        else:
            if not action:
                continue
            question = f"What human-reviewed correction should be made for {target}?"
            answer_format = "correction, evidence source, owner, and whether close can proceed"
            why_needed = action
        key = f"{task_type}|{target}|{question}"
        if key in seen:
            continue
        seen.add(key)
        questions.append(
            {
                "severity": severity,
                "task_type": task_type,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "reference": reference,
                "question": question,
                "why_needed": why_needed,
                "suggested_answer_format": answer_format,
                "reply_prompt": base_reply,
                "source": str(row.get("source") or "ai_accountant_monitor_rows"),
                "evidence_preview": str(row.get("details") or "").strip()[:260],
                "amount": row.get("amount"),
            }
        )
        if len(questions) >= max(1, int(max_rows or 12)):
            break
    return questions


def annotate_ai_accountant_question_rows(
    question_rows: list[dict[str, Any]],
    answer_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    latest_by_key: dict[str, dict[str, Any]] = {}
    for row in answer_rows or []:
        task_type = str(row.get("task_type") or "").strip().lower()
        reference = str(row.get("reference") or "").strip().lower()
        if not task_type or not reference:
            continue
        key = f"{task_type}|{reference}"
        latest_by_key.setdefault(key, row)

    annotated: list[dict[str, Any]] = []
    for row in question_rows or []:
        out = dict(row)
        task_type = str(out.get("task_type") or "").strip().lower()
        target = ""
        parsed = parse_ai_accountant_answer_prompt(str(out.get("reply_prompt") or "") + "placeholder")
        if parsed:
            target = str(parsed.get("reference") or "").strip().lower()
        if not target:
            entity_type = str(out.get("entity_type") or "").strip().lower()
            entity_id = str(out.get("entity_id") or "").strip()
            reference = str(out.get("reference") or "").strip().lower()
            target = f"{entity_type}#{entity_id}" if entity_type and entity_id and entity_id != "0" else reference
        answer = latest_by_key.get(f"{task_type}|{target}")
        followup_status = str(answer.get("followup_status") or "unreviewed").strip().lower() if answer else ""
        if not answer:
            answer_status = "unanswered"
        elif followup_status in {"needs_more_info", "obsolete"}:
            answer_status = followup_status
        elif followup_status == "applied":
            answer_status = "applied"
        else:
            answer_status = "answered"
        out["answer_status"] = answer_status
        out["latest_answer_followup_status"] = followup_status if answer else ""
        out["latest_answer_at"] = answer.get("recorded_at") if answer else ""
        out["latest_answer_actor"] = answer.get("actor") if answer else ""
        out["latest_answer_preview"] = answer.get("answer_preview") if answer else ""
        out["latest_answer_hash_sha256"] = answer.get("answer_hash_sha256") if answer else ""
        annotated.append(out)
    return annotated


def _runtime_chain_brief(runtime_chain: list[dict[str, Any]]) -> str:
    rows = list(runtime_chain or [])
    if not rows:
        return ""
    parts = []
    for row in rows[:3]:
        provider = str(row.get("provider") or "").strip() or "unknown"
        model = str(row.get("model") or "").strip() or "unknown"
        endpoint = str(row.get("endpoint_type") or "").strip() or "endpoint"
        source = str(row.get("source") or "").strip() or "source"
        status = str(row.get("status") or "").strip() or "unknown"
        parts.append(f"{provider}/{model} ({endpoint}, {source}, {status})")
    if len(rows) > 3:
        parts.append(f"+{len(rows) - 3} more")
    return "; ".join(parts)


def _compact_monitor_review(text: str, *, max_chars: int = 1200) -> str:
    compact = " ".join(str(text or "").strip().split())
    limit = max(200, int(max_chars or 1200))
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def stable_ai_accountant_hash(payload: Any) -> str:
    try:
        encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    except TypeError:
        encoded = str(payload).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_ai_accountant_review_context(
    *,
    period_label: str,
    start_date: str,
    end_date: str,
    monitor_rows: list[dict[str, Any]],
    exception_rows: list[dict[str, Any]],
    dashboard_metrics: dict[str, Any],
    sale_fifo_cogs_evidence_rows: list[dict[str, Any]] | None = None,
    max_monitor_rows: int = 100,
    max_exception_rows: int = 100,
    max_sale_fifo_cogs_evidence_rows: int = 50,
) -> dict[str, Any]:
    monitor_limit = max(1, min(100, int(max_monitor_rows or 100)))
    exception_limit = max(1, min(100, int(max_exception_rows or 100)))
    fifo_evidence_rows = list(sale_fifo_cogs_evidence_rows or [])
    fifo_evidence_limit = max(1, min(100, int(max_sale_fifo_cogs_evidence_rows or 50)))
    severity_counts = {
        "P0": sum(1 for row in monitor_rows if str(row.get("severity") or "").upper() == "P0"),
        "P1": sum(1 for row in monitor_rows if str(row.get("severity") or "").upper() == "P1"),
        "P2": sum(1 for row in monitor_rows if str(row.get("severity") or "").upper() == "P2"),
    }
    fifo_total_cost = sum(_safe_float(row.get("total_cost")) for row in fifo_evidence_rows)
    fifo_sale_ids = {str(row.get("sale_id")) for row in fifo_evidence_rows if row.get("sale_id") is not None}
    return {
        "period_label": str(period_label or "").strip(),
        "date_range": {"from": str(start_date or "").strip(), "to": str(end_date or "").strip()},
        "monitor_summary": {
            "open_item_count": int(len(monitor_rows or [])),
            "severity_counts": severity_counts,
            "p0_or_p1_count": int(severity_counts["P0"] + severity_counts["P1"]),
        },
        "monitor_rows": list(monitor_rows or [])[:monitor_limit],
        "monitor_rows_omitted": max(0, len(monitor_rows or []) - monitor_limit),
        "accounting_exception_rows": list(exception_rows or [])[:exception_limit],
        "accounting_exception_rows_omitted": max(0, len(exception_rows or []) - exception_limit),
        "sale_fifo_cogs_evidence_summary": {
            "row_count": int(len(fifo_evidence_rows)),
            "distinct_sale_count": int(len(fifo_sale_ids)),
            "total_cost": round(float(fifo_total_cost), 6),
        },
        "sale_fifo_cogs_evidence_rows": fifo_evidence_rows[:fifo_evidence_limit],
        "sale_fifo_cogs_evidence_rows_omitted": max(0, len(fifo_evidence_rows) - fifo_evidence_limit),
        "dashboard_profit_basis": {
            "sales_30d_est_profit": dashboard_metrics.get("sales_30d_est_profit"),
            "sales_30d_profit_before_returns": dashboard_metrics.get("sales_30d_profit_before_returns"),
            "sales_30d_est_cogs": dashboard_metrics.get("sales_30d_est_cogs"),
            "sales_30d_profit_basis_status": dashboard_metrics.get("sales_30d_profit_basis_status"),
            "sales_30d_cogs_review_count": dashboard_metrics.get("sales_30d_cogs_review_count"),
            "sales_30d_cogs_review_amount": dashboard_metrics.get("sales_30d_cogs_review_amount"),
            "sales_30d_cogs_estimate_amount": dashboard_metrics.get("sales_30d_cogs_estimate_amount"),
            "sales_30d_cogs_verified_amount": dashboard_metrics.get("sales_30d_cogs_verified_amount"),
            "sales_30d_cogs_review_sale_ids": dashboard_metrics.get("sales_30d_cogs_review_sale_ids"),
            "sales_30d_cogs_estimate_sale_ids": dashboard_metrics.get("sales_30d_cogs_estimate_sale_ids"),
            "sales_30d_cogs_source_counts": dashboard_metrics.get("sales_30d_cogs_source_counts"),
            "sales_30d_bundle_sale_count": dashboard_metrics.get("sales_30d_bundle_sale_count"),
            "sales_30d_bundle_inventory_units_sold": dashboard_metrics.get("sales_30d_bundle_inventory_units_sold"),
            "returns_30d_count": dashboard_metrics.get("returns_30d_count"),
            "returns_30d_refund_total": dashboard_metrics.get("returns_30d_refund_total"),
            "returns_30d_cogs_reversal": dashboard_metrics.get("returns_30d_cogs_reversal"),
            "returns_30d_profit_impact": dashboard_metrics.get("returns_30d_profit_impact"),
            "sales_30d_net_after_returns": dashboard_metrics.get("sales_30d_net_after_returns"),
        },
        "guardrails": {
            "read_only": True,
            "requires_human_approval_for_writes": True,
            "tax_legal_guardrail": "unsupported tax/legal conclusions must be routed to human review",
        },
    }


def build_ai_accountant_review_metadata(
    *,
    surface: str,
    prompt: str,
    system_message: str,
    instruction: str,
    context: dict[str, Any],
    citation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data_scope = {
        "context_keys": sorted(context.keys()),
        "context_hash_sha256": stable_ai_accountant_hash(context),
        "row_counts": {
            "monitor_rows": int(len(context.get("monitor_rows") or [])),
            "accounting_exception_rows": int(len(context.get("accounting_exception_rows") or [])),
            "sale_fifo_cogs_evidence_rows": int(len(context.get("sale_fifo_cogs_evidence_rows") or [])),
            "monitor_rows_omitted": int(context.get("monitor_rows_omitted") or 0),
            "accounting_exception_rows_omitted": int(context.get("accounting_exception_rows_omitted") or 0),
            "sale_fifo_cogs_evidence_rows_omitted": int(
                context.get("sale_fifo_cogs_evidence_rows_omitted") or 0
            ),
        },
        "date_range": dict(context.get("date_range") or {}),
    }
    return {
        "event_type": "ai_accountant_review",
        "surface": str(surface or "ai_accountant").strip() or "ai_accountant",
        "read_only": True,
        "requires_human_approval_for_writes": True,
        "tax_legal_guardrail": "unsupported conclusions routed to human review",
        "prompt_hash_sha256": stable_ai_accountant_hash(
            {"query": prompt, "system_message": system_message, "instruction": instruction}
        ),
        "data_scope_hash_sha256": data_scope["context_hash_sha256"],
        "data_scope": data_scope,
        "context_keys": data_scope["context_keys"],
        "ai_citation": dict(citation or {}),
    }


def build_sale_fifo_cogs_evidence_rows(
    repo: Any,
    *,
    start_dt: datetime,
    end_dt: datetime,
    max_rows: int = 200,
) -> list[dict[str, Any]]:
    """Build compact sale FIFO COGS evidence for automated accounting review."""
    db = getattr(repo, "db", None)
    if db is None or not hasattr(repo, "report_sale_unit_cost_maps"):
        return []
    limit = max(1, min(500, int(max_rows or 200)))
    try:
        product_cost_rows = db.execute(
            select(
                Product.id,
                Product.acquisition_cost,
                Product.acquisition_tax_paid,
                Product.acquisition_shipping_paid,
                Product.acquisition_handling_paid,
                Product.product_cost,
            )
        ).all()
        default_unit_cost_by_product = {
            int(row.id): repo._product_default_landed_unit_cost(row)
            for row in product_cost_rows
            if getattr(row, "id", None) is not None and hasattr(repo, "_product_default_landed_unit_cost")
        }
        cost_maps = repo.report_sale_unit_cost_maps(
            end_dt=end_dt,
            default_unit_cost_by_product=default_unit_cost_by_product,
        )
        evidence_by_sale = dict(cost_maps.get("fifo_cogs_evidence_by_sale") or {})
        if not evidence_by_sale:
            return []
        sale_rows = db.execute(
            select(
                Sale.id.label("sale_id"),
                Sale.sold_at.label("sold_at"),
                Sale.marketplace.label("marketplace"),
                Sale.external_order_id.label("external_order_id"),
                Sale.quantity_sold.label("quantity_sold"),
                Product.sku.label("sku"),
                Product.title.label("product_title"),
            )
            .select_from(Sale)
            .outerjoin(Product, Product.id == Sale.product_id)
            .where(
                Sale.sold_at.is_not(None),
                Sale.sold_at >= start_dt,
                Sale.sold_at <= end_dt,
            )
            .order_by(Sale.sold_at.desc(), Sale.id.desc())
        ).all()
        rows: list[dict[str, Any]] = []
        for sale in sale_rows:
            sale_id = int(getattr(sale, "sale_id", 0) or 0)
            sale_evidence = list(evidence_by_sale.get(sale_id) or [])
            if not sale_evidence:
                continue
            for idx, evidence in enumerate(sale_evidence, start=1):
                rows.append(
                    {
                        "sale_id": sale_id,
                        "sold_at": getattr(sale, "sold_at", None).isoformat()
                        if getattr(sale, "sold_at", None)
                        else None,
                        "marketplace": str(getattr(sale, "marketplace", "") or ""),
                        "external_order_id": str(getattr(sale, "external_order_id", "") or ""),
                        "sku": str(getattr(sale, "sku", "") or ""),
                        "product_title": str(getattr(sale, "product_title", "") or ""),
                        "sale_quantity": int(getattr(sale, "quantity_sold", 0) or 0),
                        "allocation_index": idx,
                        "evidence_product_id": evidence.get("product_id"),
                        "lot_id": evidence.get("lot_id"),
                        "assignment_id": evidence.get("assignment_id"),
                        "quantity": int(evidence.get("quantity") or 0),
                        "unit_cost": _safe_float(evidence.get("unit_cost")),
                        "total_cost": _safe_float(evidence.get("total_cost")),
                        "cost_source": str(evidence.get("cost_source") or "unknown").strip() or "unknown",
                    }
                )
                if len(rows) >= limit:
                    return rows
        return rows
    except Exception:
        if hasattr(db, "rollback"):
            try:
                db.rollback()
            except Exception:
                pass
        return []


def _review_metadata_row_counts(metadata: dict[str, Any]) -> dict[str, int]:
    data_scope = metadata.get("data_scope") if isinstance(metadata.get("data_scope"), dict) else {}
    row_counts = data_scope.get("row_counts") if isinstance(data_scope.get("row_counts"), dict) else {}
    return {
        "monitor_rows": _safe_int(row_counts.get("monitor_rows")),
        "accounting_exception_rows": _safe_int(row_counts.get("accounting_exception_rows")),
        "sale_fifo_cogs_evidence_rows": _safe_int(row_counts.get("sale_fifo_cogs_evidence_rows")),
        "monitor_rows_omitted": _safe_int(row_counts.get("monitor_rows_omitted")),
        "accounting_exception_rows_omitted": _safe_int(row_counts.get("accounting_exception_rows_omitted")),
        "sale_fifo_cogs_evidence_rows_omitted": _safe_int(
            row_counts.get("sale_fifo_cogs_evidence_rows_omitted")
        ),
    }


def record_ai_accountant_review_outcome(
    repo: Any,
    *,
    actor: str,
    outcome: str,
    answer_text: str,
    review_metadata: dict[str, Any] | None,
) -> None:
    if not hasattr(repo, "log_ai_chat_interaction"):
        return
    clean_outcome = str(outcome or "").strip().lower()
    if clean_outcome not in {"accepted", "edited", "rejected"}:
        clean_outcome = "reviewed"
    metadata = dict(review_metadata or {})
    metadata.update(
        {
            "event_type": "ai_accountant_review_outcome",
            "review_type": "ai_accountant_review",
            "outcome": clean_outcome,
            "answer_hash_sha256": stable_ai_accountant_hash(str(answer_text or "")),
            "requires_human_approval_for_writes": True,
        }
    )
    repo.log_ai_chat_interaction(
        actor=actor,
        prompt=f"ai_accountant_review outcome: {clean_outcome}",
        intent="ai_accountant_review_outcome",
        allowed_domains=["accounting", "reports", "sales", "orders", "inventory", "tax"],
        citations=[],
        answer_preview=str(answer_text or "").strip(),
        denied=False,
        elapsed_ms=0,
        metadata=metadata,
    )


def record_ai_accountant_answer(
    repo: Any,
    *,
    actor: str,
    prompt: str,
    source: str = "ask",
) -> dict[str, Any] | None:
    parsed = parse_ai_accountant_answer_prompt(prompt)
    if not parsed:
        return None
    if not hasattr(repo, "record_audit_event"):
        return parsed
    payload = {
        "event_type": "ai_accountant_answer",
        "source": str(source or "").strip() or "ask",
        "task_type": parsed["task_type"],
        "reference": parsed["reference"],
        "entity_type": parsed["entity_type"],
        "entity_id": parsed["entity_id"],
        "answer_text": parsed["answer_text"],
        "answer_hash_sha256": parsed["answer_hash_sha256"],
        "raw_prompt": parsed["raw_prompt"],
        "read_only": True,
        "requires_normal_workflow_correction": True,
    }
    try:
        repo.record_audit_event(
            entity_type="ai_accountant_answer",
            entity_id=parsed["entity_id"],
            action="record",
            actor=str(actor or "operator").strip() or "operator",
            changes=payload,
        )
    except Exception:
        db = getattr(repo, "db", None)
        if db is not None and hasattr(db, "rollback"):
            db.rollback()
    return parsed


def record_ai_accountant_message(
    repo: Any,
    *,
    actor: str,
    message: str,
    period_label: str,
    rows: list[dict[str, Any]],
    slack_outbox_id: int | None = None,
    review_result: dict[str, Any] | None = None,
    min_severity: str = "",
    requested_min_severity: str = "",
    question_status_counts: dict[str, int] | None = None,
) -> Any:
    review_payload = dict(review_result or {})
    review_metadata = review_payload.get("metadata") if isinstance(review_payload.get("metadata"), dict) else {}
    review_row_counts = _review_metadata_row_counts(review_metadata)
    effective_min_severity = str(min_severity or "").strip().upper()
    raw_requested_min_severity = str(requested_min_severity or effective_min_severity).strip().upper()
    return repo.record_audit_event(
        entity_type="ai_accountant_message",
        entity_id=None,
        action="create",
        actor=actor,
        changes={
            "message": str(message or "").strip(),
            "period": str(period_label or "").strip(),
            "item_count": len(rows),
            "severity_counts": {
                "P0": sum(1 for row in rows if str(row.get("severity") or "").upper() == "P0"),
                "P1": sum(1 for row in rows if str(row.get("severity") or "").upper() == "P1"),
                "P2": sum(1 for row in rows if str(row.get("severity") or "").upper() == "P2"),
            },
            "question_status_counts": {
                str(key): _safe_int(value)
                for key, value in sorted((question_status_counts or {}).items())
                if str(key).strip()
            },
            "sample_items": rows[:10],
            "slack_outbox_id": slack_outbox_id,
            "min_severity": effective_min_severity,
            "requested_min_severity": raw_requested_min_severity,
            "min_severity_fallback_applied": bool(
                raw_requested_min_severity and raw_requested_min_severity != effective_min_severity
            ),
            "automated_review": {
                "enabled": bool(review_payload.get("enabled")),
                "answer_hash_sha256": str(review_payload.get("answer_hash_sha256") or ""),
                "prompt_hash_sha256": str(review_metadata.get("prompt_hash_sha256") or ""),
                "data_scope_hash_sha256": str(review_metadata.get("data_scope_hash_sha256") or ""),
                "error": str(review_payload.get("error") or "")[:500],
                "preview": str(review_payload.get("text") or "")[:500],
                "compact_retry": bool(review_payload.get("compact_retry")),
                "runtime_chain": list(review_payload.get("runtime_chain") or []),
                "runtime_chain_brief": str(review_payload.get("runtime_chain_brief") or "")[:500],
                "monitor_rows": review_row_counts["monitor_rows"],
                "exception_rows": review_row_counts["accounting_exception_rows"],
                "sale_fifo_cogs_evidence_rows": review_row_counts["sale_fifo_cogs_evidence_rows"],
                "monitor_rows_omitted": review_row_counts["monitor_rows_omitted"],
                "exception_rows_omitted": review_row_counts["accounting_exception_rows_omitted"],
                "sale_fifo_cogs_evidence_rows_omitted": review_row_counts[
                    "sale_fifo_cogs_evidence_rows_omitted"
                ],
            },
        },
    )


def enqueue_ai_accountant_slack_message(
    repo: Any,
    *,
    actor: str,
    message: str,
    period_label: str,
    dedupe_key: str = "",
    channel: str = "",
) -> Any:
    payload = {
        "text": str(message or "").strip(),
        "channel": str(channel or "").strip(),
        "event_type": "ai_accountant_monitor",
        "severity": "warning",
    }
    digest = hashlib.sha256(str(message or "").encode("utf-8")).hexdigest()[:16]
    resolved_dedupe = dedupe_key or f"ai_accountant_monitor:{settings.app_env}:{period_label}:{digest}"
    return repo.enqueue_notification_outbox(
        environment=settings.app_env,
        channel="slack",
        event_type="ai_accountant_monitor",
        entity_type="ai_accountant_message",
        entity_id="",
        dedupe_key=resolved_dedupe,
        payload_json=json.dumps(payload, sort_keys=True),
        requested_by=actor,
        actor=actor,
    )


def run_ai_accountant_automated_review(
    repo: Any,
    *,
    actor: str,
    period_label: str,
    start_dt: datetime,
    end_dt: datetime,
    monitor_rows: list[dict[str, Any]],
    exception_rows: list[dict[str, Any]],
    dashboard_metrics: dict[str, Any],
) -> dict[str, Any]:
    if not get_runtime_bool(repo, "ai_accountant_monitor_llm_review_enabled", True):
        return {"enabled": False, "text": "", "error": "", "answer_hash_sha256": ""}

    prompt = f"{AI_ACCOUNTANT_NAME} scheduled monitor review"
    system_message = get_runtime_str(
        repo,
        "accountant_llm_system_message",
        DEFAULT_AI_ACCOUNTANT_SYSTEM_MESSAGE,
    ).strip()
    instruction = get_runtime_str(
        repo,
        "ai_accountant_monitor_review_instruction",
        DEFAULT_AI_ACCOUNTANT_MONITOR_INSTRUCTION,
    ).strip()
    max_monitor_rows = max(5, min(50, int(get_runtime_int(repo, "ai_accountant_monitor_review_max_rows", 25))))
    max_exception_rows = max(5, min(50, int(get_runtime_int(repo, "ai_accountant_monitor_review_max_exception_rows", 25))))
    max_fifo_evidence_rows = max(
        5,
        min(100, int(get_runtime_int(repo, "ai_accountant_monitor_review_max_fifo_evidence_rows", 25))),
    )
    compact_retry = False
    review_errors: list[str] = []
    runtime_chain = describe_llm_runtime_chain(repo, workflow="accounting")
    sale_fifo_cogs_evidence_rows = build_sale_fifo_cogs_evidence_rows(
        repo,
        start_dt=start_dt,
        end_dt=end_dt,
        max_rows=max_fifo_evidence_rows,
    )
    context_kwargs = {
        "period_label": period_label,
        "start_date": start_dt.date().isoformat(),
        "end_date": end_dt.date().isoformat(),
        "monitor_rows": monitor_rows,
        "exception_rows": exception_rows,
        "dashboard_metrics": dashboard_metrics,
        "sale_fifo_cogs_evidence_rows": sale_fifo_cogs_evidence_rows,
    }
    context = build_ai_accountant_review_context(
        **context_kwargs,
        max_monitor_rows=max_monitor_rows,
        max_exception_rows=max_exception_rows,
        max_sale_fifo_cogs_evidence_rows=max_fifo_evidence_rows,
    )
    try:
        from app.services.ai_orchestration import execute_comp_summary

        try:
            result = execute_comp_summary(
                repo,
                query=prompt,
                ebay_rows=[],
                web_rows=[],
                spot_context=context,
                system_message=system_message,
                instruction=instruction,
                workflow="accounting",
            )
        except Exception as exc:
            review_errors.append(f"default_context: {str(exc)[:350]}")
            compact_retry = True
            context = build_ai_accountant_review_context(
                **context_kwargs,
                max_monitor_rows=5,
                max_exception_rows=5,
                max_sale_fifo_cogs_evidence_rows=5,
            )
            result = execute_comp_summary(
                repo,
                query=f"{prompt} (compact retry)",
                ebay_rows=[],
                web_rows=[],
                spot_context=context,
                system_message=system_message,
                instruction=instruction,
                workflow="accounting",
            )
        review_text = str(getattr(result, "text", "") or "").strip()
        metadata = build_ai_accountant_review_metadata(
            surface="ai_accountant_scheduled_monitor",
            prompt=prompt,
            system_message=system_message,
            instruction=instruction,
            context=context,
            citation=dict(getattr(result, "citation", {}) or {}),
        )
        metadata.update(
            {
                "event_type": "ai_accountant_automated_review",
                "review_type": "ai_accountant_scheduled_monitor",
                "automated": True,
                "compact_retry": bool(compact_retry),
                "answer_hash_sha256": stable_ai_accountant_hash(review_text),
                "runtime_chain": runtime_chain,
                "runtime_chain_brief": _runtime_chain_brief(runtime_chain),
            }
        )
        if hasattr(repo, "log_ai_chat_interaction"):
            repo.log_ai_chat_interaction(
                actor=actor,
                prompt=prompt,
                intent="ai_accountant_scheduled_monitor_review",
                allowed_domains=["accounting", "reports", "sales", "orders", "inventory", "tax"],
                citations=[
                    {
                        "table": "ai_accountant_monitor_rows",
                        "filters": f"period={period_label}",
                        "rows_considered": int(len(monitor_rows or [])),
                        "as_of_utc": utcnow_naive().isoformat(),
                    },
                    {
                        "table": "accounting_exception_queue",
                        "filters": f"period={period_label}",
                        "rows_considered": int(len(exception_rows or [])),
                        "as_of_utc": utcnow_naive().isoformat(),
                    },
                    {
                        "table": "sale_fifo_cogs_evidence",
                        "filters": f"period={period_label}",
                        "rows_considered": int(len(sale_fifo_cogs_evidence_rows or [])),
                        "as_of_utc": utcnow_naive().isoformat(),
                    },
                ],
                answer_preview=review_text,
                denied=False,
                elapsed_ms=0,
                metadata=metadata,
            )
        return {
            "enabled": True,
            "text": review_text,
            "error": "",
            "metadata": metadata,
            "compact_retry": bool(compact_retry),
            "answer_hash_sha256": str(metadata.get("answer_hash_sha256") or ""),
            "runtime_chain": runtime_chain,
            "runtime_chain_brief": _runtime_chain_brief(runtime_chain),
        }
    except Exception as exc:
        db = getattr(repo, "db", None)
        if db is not None and hasattr(db, "rollback"):
            try:
                db.rollback()
            except Exception:
                pass
        metadata = build_ai_accountant_review_metadata(
            surface="ai_accountant_scheduled_monitor",
            prompt=prompt,
            system_message=system_message,
            instruction=instruction,
            context=context,
            citation={
                "tool_name": "ai_accountant_scheduled_monitor_review",
                "source": "runtime_failure",
                "fallback_errors": [*review_errors, f"compact_context: {str(exc)[:350]}"],
            },
        )
        metadata.update(
            {
                "event_type": "ai_accountant_automated_review",
                "review_type": "ai_accountant_scheduled_monitor",
                "automated": True,
                "compact_retry": bool(compact_retry),
                "runtime_failure": True,
                "runtime_chain": runtime_chain,
                "runtime_chain_brief": _runtime_chain_brief(runtime_chain),
            }
        )
        return {
            "enabled": True,
            "text": "",
            "error": " | ".join([*review_errors, f"compact_context: {str(exc)[:350]}"])[:700],
            "metadata": metadata,
            "compact_retry": bool(compact_retry),
            "answer_hash_sha256": "",
            "runtime_chain": runtime_chain,
            "runtime_chain_brief": _runtime_chain_brief(runtime_chain),
        }


def run_ai_accountant_monitor(
    repo: Any,
    *,
    actor: str,
    now: datetime | None = None,
    lookback_days: int = 30,
    min_severity: str = "P1",
    slack_enabled: bool = False,
    slack_channel: str = "",
    record_when_empty: bool = False,
) -> dict[str, Any]:
    end_dt = now or utcnow_naive()
    start_dt = end_dt - timedelta(days=max(1, int(lookback_days or 30)))
    period_label = f"{start_dt.date().isoformat()} to {end_dt.date().isoformat()}"
    exception_rows = list(repo.report_accounting_exception_rows(start_dt=start_dt, end_dt=end_dt) or [])
    dashboard_metrics = {}
    if hasattr(repo, "dashboard_live_metrics"):
        dashboard_metrics = dict(repo.dashboard_live_metrics(now=end_dt, include_fee_type_breakdown=False) or {})
    review_outcomes = list_ai_accountant_review_outcomes(repo)
    answer_rows = list_ai_accountant_answers(repo)
    rows = build_ai_accountant_monitor_rows(
        exception_rows,
        dashboard_metrics=dashboard_metrics,
        review_outcome_rows=review_outcomes,
        answer_rows=answer_rows,
    )
    requested_min_severity = str(min_severity or "P1").strip().upper() or "P1"
    effective_min_severity = requested_min_severity if requested_min_severity in ACCOUNTING_SEVERITY_ORDER else "P1"
    min_rank = ACCOUNTING_SEVERITY_ORDER[effective_min_severity]
    actionable_rows = [
        row for row in rows if ACCOUNTING_SEVERITY_ORDER.get(str(row.get("severity") or ""), 99) <= min_rank
    ]
    should_record = bool(actionable_rows) or bool(record_when_empty)
    message_rows = actionable_rows if actionable_rows else rows
    review_result: dict[str, Any] = {"enabled": False, "text": "", "error": "", "answer_hash_sha256": ""}
    if should_record:
        review_result = run_ai_accountant_automated_review(
            repo,
            actor=actor,
            period_label=period_label,
            start_dt=start_dt,
            end_dt=end_dt,
            monitor_rows=message_rows,
            exception_rows=exception_rows,
            dashboard_metrics=dashboard_metrics,
        )
    question_status_counts = build_ai_accountant_question_status_counts(message_rows, answer_rows=answer_rows)
    message = build_ai_accountant_message(message_rows, period_label=period_label, answer_rows=answer_rows)
    if review_result.get("text"):
        message = f"{message}\n\n{AI_ACCOUNTANT_NAME} automated review:\n{_compact_monitor_review(str(review_result.get('text') or ''))}"
    elif review_result.get("error"):
        runtime_brief = str(review_result.get("runtime_chain_brief") or "").strip()
        message = (
            f"{message}\n\n{AI_ACCOUNTANT_NAME} automated review unavailable: "
            f"{str(review_result.get('error') or '').strip()[:300]}"
        )
        if runtime_brief:
            message = f"{message}\nRuntime route: {runtime_brief}"
    outbox_id = None
    if should_record and slack_enabled and actionable_rows:
        outbox = enqueue_ai_accountant_slack_message(
            repo,
            actor=actor,
            message=message,
            period_label=period_label,
            channel=slack_channel,
        )
        outbox_id = int(getattr(outbox, "id", 0) or 0) or None
    audit_id = None
    if should_record:
        audit = record_ai_accountant_message(
            repo,
            actor=actor,
            message=message,
            period_label=period_label,
            rows=actionable_rows if actionable_rows else rows,
            slack_outbox_id=outbox_id,
            review_result=review_result,
            min_severity=effective_min_severity,
            requested_min_severity=requested_min_severity,
            question_status_counts=question_status_counts,
        )
        audit_id = int(getattr(audit, "id", 0) or 0) or None
    return {
        "item_count": len(rows),
        "actionable_count": len(actionable_rows),
        "audit_id": audit_id,
        "slack_outbox_id": outbox_id,
        "period_label": period_label,
        "message": message,
        "question_status_counts": question_status_counts,
        "min_severity": effective_min_severity,
        "requested_min_severity": requested_min_severity,
        "review_enabled": bool(review_result.get("enabled")),
        "review_error": str(review_result.get("error") or ""),
        "review_hash": str(review_result.get("answer_hash_sha256") or ""),
        "review_compact_retry": bool(review_result.get("compact_retry")),
        "review_runtime_route": str(review_result.get("runtime_chain_brief") or ""),
    }
