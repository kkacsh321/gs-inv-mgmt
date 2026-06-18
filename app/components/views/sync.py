from datetime import timedelta
import json

import pandas as pd
import streamlit as st

from app.auth import current_user, ensure_permission
from app.config import settings
from app.components.views.shared import render_help_panel
from app.components.views.workspace_shell import (
    render_workspace_empty_state,
    render_workspace_error_state,
    render_workspace_feedback,
    render_workspace_loading_state,
    render_workspace_task_completion,
)
from app.repository import InventoryRepository
from app.services.ai_orchestration import execute_comp_summary
from app.services.sync_jobs import (
    execute_sync_job,
    is_sync_job_enabled,
    sync_job_dispatch_meta,
    sync_job_catalog,
    sync_job_retry_policy,
)
from app.utils.time import utcnow_naive


def _store_category_sync_event_summary_rows(events) -> list[dict]:
    rows: list[dict] = []
    for event in list(events or []):
        if str(getattr(event, "entity_type", "") or "").strip() != "ebay_store_categories":
            continue
        raw_payload = str(getattr(event, "payload_json", "") or "").strip()
        payload = {}
        if raw_payload:
            try:
                parsed = json.loads(raw_payload)
                if isinstance(parsed, dict):
                    payload = parsed
            except Exception:
                payload = {}
        rows.append(
            {
                "event_id": int(getattr(event, "id", 0) or 0),
                "status": str(getattr(event, "status", "") or ""),
                "marketplace_id": str(payload.get("marketplace_id") or getattr(event, "entity_id", "") or ""),
                "site_id": str(payload.get("site_id") or ""),
                "ack": str(payload.get("ack") or ""),
                "imported_count": int(payload.get("imported_count") or 0),
                "missing_count": int(payload.get("missing_count") or 0),
                "deactivated_count": int(payload.get("deactivated_count") or 0),
                "deactivate_missing": bool(payload.get("deactivate_missing", False)),
                "message": str(getattr(event, "message", "") or ""),
                "created_at": getattr(event, "created_at", None),
            }
        )
    return rows


def _retry_allowed_for_run(run, repo: InventoryRepository) -> tuple[bool, str]:
    policy = sync_job_retry_policy(run.job_name, repo=repo)
    terminal_statuses = {str(v).strip().lower() for v in (policy.get("terminal_statuses") or [])}
    retryable_statuses = {str(v).strip().lower() for v in (policy.get("retryable_statuses") or [])}
    status = (run.status or "").strip().lower()
    if status not in terminal_statuses:
        return (
            False,
            f"Retry blocked: run is not in terminal statuses ({', '.join(sorted(terminal_statuses))}).",
        )
    if status not in retryable_statuses:
        return (
            False,
            f"Retry is only enabled for statuses: {', '.join(sorted(retryable_statuses)) or 'none configured'}.",
        )
    if not is_sync_job_enabled(run.job_name, repo=repo):
        return False, f"Retry is disabled because `{run.job_name}` is disabled by configuration."
    retry_max = int(policy.get("max_retries") or 0)
    if int(run.retry_count or 0) >= retry_max:
        runtime_keys = policy.get("runtime_keys") or {}
        return (
            False,
            f"Retry blocked: `{run.job_name}` reached max retries ({retry_max}). "
            f"Override `{runtime_keys.get('max_retries', '')}` in Runtime Settings if needed.",
        )
    backoff_seconds = int(policy.get("retry_backoff_seconds") or 0)
    if backoff_seconds > 0 and run.completed_at is not None:
        earliest_retry_at = run.completed_at + timedelta(seconds=backoff_seconds)
        if utcnow_naive() < earliest_retry_at:
            return (
                False,
                f"Retry backoff active. Earliest retry time: {earliest_retry_at.isoformat(timespec='seconds')}.",
            )
    return True, "Retry is available for failed/partial runs."


def _run_root_id(run, run_index: dict[int, object]) -> int:
    current = run
    seen: set[int] = set()
    while current is not None and getattr(current, "retry_of_run_id", None):
        current_id = int(getattr(current, "id"))
        if current_id in seen:
            break
        seen.add(current_id)
        parent_id = int(getattr(current, "retry_of_run_id"))
        parent = run_index.get(parent_id)
        if parent is None:
            return parent_id
        current = parent
    return int(getattr(current, "id"))


def _run_chain_depth(run, run_index: dict[int, object]) -> int:
    depth = 0
    current = run
    seen: set[int] = set()
    while current is not None and getattr(current, "retry_of_run_id", None):
        current_id = int(getattr(current, "id"))
        if current_id in seen:
            break
        seen.add(current_id)
        depth += 1
        parent_id = int(getattr(current, "retry_of_run_id"))
        current = run_index.get(parent_id)
    return depth


def _lineage_terminal_status(chain_rows: list[object]) -> str:
    if not chain_rows:
        return "unknown"
    ordered = sorted(
        chain_rows,
        key=lambda r: (
            0 if getattr(r, "completed_at", None) is not None else 1,
            getattr(r, "completed_at", None) or getattr(r, "started_at", None),
            int(getattr(r, "id")),
        ),
    )
    return str(getattr(ordered[-1], "status", "unknown") or "unknown")


def _render_sync_copilot(repo: InventoryRepository, user, runs, queue_pairs) -> None:
    st.markdown("### Sync Copilot")
    st.caption("AI failure clustering + retry recommendations + lineage root-cause hints.")
    if st.button("Analyze Sync Failures", key="sync_copilot_analyze_btn"):
        if not ensure_permission(user, "ai_comp_use", "Use Sync Copilot"):
            return
        try:
            failed_runs = [r for r in runs if str(r.status or "").strip().lower() in {"failed", "partial"}]
            by_job: dict[str, int] = {}
            by_status: dict[str, int] = {}
            for run in failed_runs:
                by_job[str(run.job_name or "unknown")] = by_job.get(str(run.job_name or "unknown"), 0) + 1
                by_status[str(run.status or "unknown")] = by_status.get(str(run.status or "unknown"), 0) + 1
            by_error_code: dict[str, int] = {}
            for err, run in queue_pairs:
                code = str(err.code or "no_code")
                key = f"{run.job_name}:{code}"
                by_error_code[key] = by_error_code.get(key, 0) + 1
            context = {
                "run_totals": {
                    "runs_considered": len(runs),
                    "failed_or_partial_runs": len(failed_runs),
                    "queued_runs": len([r for r in runs if str(r.status or "").strip().lower() == "queued"]),
                    "running_runs": len([r for r in runs if str(r.status or "").strip().lower() == "running"]),
                },
                "failure_clusters_by_job": by_job,
                "failure_clusters_by_status": by_status,
                "error_clusters_job_code": by_error_code,
                "sample_failed_runs": [
                    {
                        "run_id": int(r.id),
                        "job_name": str(r.job_name or ""),
                        "provider": str(r.provider or ""),
                        "status": str(r.status or ""),
                        "retry_count": int(r.retry_count or 0),
                        "retry_of_run_id": int(r.retry_of_run_id) if r.retry_of_run_id is not None else None,
                        "failed_count": int(r.records_failed or 0),
                        "started_at": str(r.started_at or ""),
                        "completed_at": str(r.completed_at or ""),
                    }
                    for r in failed_runs[:30]
                ],
                "sample_errors": [
                    {
                        "error_id": int(err.id),
                        "run_id": int(run.id),
                        "job_name": str(run.job_name or ""),
                        "severity": str(err.severity or ""),
                        "code": str(err.code or ""),
                        "message": str(err.message or "")[:240],
                        "resolved_at": str(err.resolved_at or ""),
                    }
                    for err, run in queue_pairs[:50]
                ],
            }
            result = execute_comp_summary(
                repo,
                query="Sync failure triage and retry recommendations",
                ebay_rows=[],
                web_rows=[],
                spot_context=context,
                system_message=(
                    "You are an integration reliability copilot for marketplace sync operations."
                ),
                instruction=(
                    "Return ONLY JSON with keys: `failure_clusters`, `retry_recommendations`, "
                    "`root_cause_hypotheses`, `next_actions`. Each key must be an array of concise bullet strings."
                ),
            )
            st.session_state["sync_copilot_raw"] = str(result.text or "").strip()
            st.success("Sync copilot analysis complete.")
            st.rerun()
        except Exception as exc:
            st.error(f"Sync copilot analysis failed: {exc}")

    raw_val = str(st.session_state.get("sync_copilot_raw") or "").strip()
    if raw_val:
        with st.expander("Sync Copilot Result", expanded=False):
            st.code(raw_val, language="json")


def render_sync(repo: InventoryRepository) -> None:
    user = current_user()
    ebay_pull_enabled = is_sync_job_enabled("ebay_orders_pull_import", repo=repo)
    ebay_tracking_push_enabled = is_sync_job_enabled("ebay_shipping_tracking_push", repo=repo)
    st.subheader("Sync")
    st.caption("Track marketplace/accounting sync runs, events, and errors.")
    render_help_panel(
        section_title="Sync",
        goal="Provide an operational log for pull/push sync jobs with retry visibility.",
        steps=[
            "Create a run record for each scheduled/manual sync execution.",
            "Record per-entity events and errors as the run executes.",
            "Update run status and processed/failed counters on completion.",
        ],
        roadmap_phase="v0.3 Channel Sync + Accounting Readiness",
    )
    st.markdown("### Sync Job Controls")
    j1, j2 = st.columns(2)
    with j1:
        st.metric("eBay Orders Pull/Import", "Enabled" if ebay_pull_enabled else "Disabled")
    with j2:
        st.metric("eBay Shipping Tracking Push", "Enabled" if ebay_tracking_push_enabled else "Disabled")
    st.caption(
        "Use Runtime Settings (DB) to control execution live, with env fallback keys: "
        "`sync_job_ebay_orders_pull_import_enabled`, "
        "`sync_job_ebay_shipping_tracking_push_enabled`, "
        "`sync_job_shopify_orders_pull_enabled`, "
        "`sync_job_shopify_orders_pull_shop_domain`, "
        "`sync_job_shopify_orders_pull_limit`, "
        "`sync_job_shopify_orders_pull_offset` "
        "(fallback env: `SYNC_JOB_EBAY_ORDERS_PULL_IMPORT_ENABLED`, "
        "`SYNC_JOB_EBAY_SHIPPING_TRACKING_PUSH_ENABLED`, "
        "`SYNC_JOB_EBAY_STORE_CATEGORIES_SYNC_ENABLED`, "
        "`SYNC_JOB_SHOPIFY_ORDERS_PULL_ENABLED`, "
        "`SYNC_JOB_SHOPIFY_ORDERS_PULL_SHOP_DOMAIN`, "
        "`SYNC_JOB_SHOPIFY_ORDERS_PULL_LIMIT`, "
        "`SYNC_JOB_SHOPIFY_ORDERS_PULL_OFFSET`)."
    )
    catalog_rows = sync_job_catalog(repo)
    st.dataframe(pd.DataFrame(catalog_rows), use_container_width=True)
    st.caption("Retry policy defaults (Runtime Settings):")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "job_name": row.get("job_name"),
                    "enabled": row.get("enabled"),
                    "max_retries": int((row.get("retry_policy") or {}).get("max_retries") or 0),
                    "retry_backoff_seconds": int((row.get("retry_policy") or {}).get("retry_backoff_seconds") or 0),
                    "retryable_statuses": ", ".join((row.get("retry_policy") or {}).get("retryable_statuses") or []),
                    "terminal_statuses": ", ".join((row.get("retry_policy") or {}).get("terminal_statuses") or []),
                    "max_retries_key": ((row.get("retry_policy") or {}).get("runtime_keys") or {}).get(
                        "max_retries", ""
                    ),
                    "backoff_key": ((row.get("retry_policy") or {}).get("runtime_keys") or {}).get(
                        "retry_backoff_seconds", ""
                    ),
                }
                for row in catalog_rows
            ]
        ),
        use_container_width=True,
    )
    executable_jobs = [
        row
        for row in catalog_rows
        if bool((row.get("dispatch_meta") or {}).get("supports_execute_now"))
    ]
    if executable_jobs:
        st.markdown("#### Execute Job Now")
        exec_options = {
            f"{row.get('job_name')} ({row.get('provider')})": row for row in executable_jobs
        }
        selected_exec_label = st.selectbox(
            "Job",
            options=list(exec_options.keys()),
            key="sync_execute_now_job",
        )
        selected_exec = exec_options[selected_exec_label]
        selected_job_name = str(selected_exec.get("job_name") or "").strip()
        selected_enabled = bool(selected_exec.get("enabled"))
        ex1, ex2, ex3, ex4 = st.columns(4)
        with ex1:
            execute_limit = st.number_input(
                "Limit",
                min_value=1,
                max_value=250,
                value=int(settings.sync_job_shopify_orders_pull_limit or 50),
                step=1,
                key="sync_execute_now_limit",
            )
        with ex2:
            execute_offset = st.number_input(
                "Offset",
                min_value=0,
                value=int(settings.sync_job_shopify_orders_pull_offset or 0),
                step=1,
                key="sync_execute_now_offset",
            )
        with ex3:
            execute_shop_domain = st.text_input(
                "Shop Domain (Shopify)",
                value=str(settings.sync_job_shopify_orders_pull_shop_domain or "").strip(),
                key="sync_execute_now_shop_domain",
            )
        with ex4:
            execute_token = st.text_input(
                "Access Token (optional)",
                value="",
                type="password",
                key="sync_execute_now_access_token",
            )
        if selected_job_name == "ebay_store_categories_sync":
            sc1, sc2 = st.columns(2)
            with sc1:
                execute_marketplace_id = st.text_input(
                    "eBay Marketplace ID",
                    value=str(settings.ebay_marketplace_id or "EBAY_US").strip() or "EBAY_US",
                    key="sync_execute_now_ebay_store_marketplace_id",
                )
            with sc2:
                execute_deactivate_missing = st.checkbox(
                    "Deactivate stale eBay-synced categories",
                    value=False,
                    key="sync_execute_now_ebay_store_deactivate_missing",
                    help=(
                        "Only previously eBay-imported store categories missing from the latest GetStore response "
                        "are deactivated. Manual categories are left active."
                    ),
                )
        else:
            execute_marketplace_id = str(settings.ebay_marketplace_id or "EBAY_US").strip() or "EBAY_US"
            execute_deactivate_missing = False
        if st.button(
            "Execute Selected Job Now",
            key="sync_execute_now_submit",
            disabled=not selected_enabled,
            help="Disabled while selected job is turned off in runtime settings.",
        ):
            if not ensure_permission(user, "create", "Execute Sync Job"):
                return
            if not selected_enabled:
                st.error(f"Sync job `{selected_job_name}` is disabled by configuration.")
                return
            try:
                result = execute_sync_job(
                    repo,
                    job_name=selected_job_name,
                    actor=user.username,
                    access_token=execute_token.strip(),
                    shop_domain=execute_shop_domain.strip(),
                    limit=int(execute_limit),
                    offset=int(execute_offset),
                    marketplace_id=execute_marketplace_id.strip(),
                    deactivate_missing=bool(execute_deactivate_missing),
                )
                st.success(
                    f"Run #{result.get('run_id')} completed with status `{result.get('status')}` "
                    f"(processed={result.get('processed', 0)}, created={result.get('created', 0)}, "
                    f"updated={result.get('updated', 0)}, failed={result.get('failed', 0)})."
                )
                st.session_state["sync_focus_run_id"] = int(result.get("run_id") or 0)
                st.rerun()
            except Exception as exc:
                st.error(f"Execute-now failed: {exc}")

    c1, c2 = st.columns([1, 2])
    if "sync_provider_filter" not in st.session_state:
        st.session_state["sync_provider_filter"] = "all"
    handoff_active = (
        str(st.session_state.get("workspace_handoff_from") or "").strip().lower() == "ebay_workspace"
        and str(st.session_state.get("workspace_handoff_target") or "").strip().lower() == "sync"
    )
    if handoff_active:
        h1, h2 = st.columns([4, 1])
        with h1:
            st.info("Opened from eBay Workspace context. Provider filter was preloaded for eBay sync operations.")
        with h2:
            if st.button("Clear Handoff", key="sync_clear_handoff_btn", use_container_width=True):
                try:
                    repo.record_audit_event(
                        entity_type="navigation",
                        entity_id=None,
                        action="workspace_handoff_cleared",
                        actor=user.username,
                        changes={
                            "from": "ebay_workspace",
                            "target": "sync",
                            "cleared_provider": st.session_state.get("sync_provider_filter") or "all",
                        },
                    )
                except Exception:
                    pass
                st.session_state["sync_provider_filter"] = "all"
                st.session_state["workspace_handoff_from"] = ""
                st.session_state["workspace_handoff_target"] = ""
                st.rerun()
    with c1:
        provider_filter = st.selectbox(
            "Provider",
            ["all", "ebay", "quickbooks", "shopify"],
            key="sync_provider_filter",
        )
        refresh = st.button("Refresh")
    with c2:
        st.caption("Use this page as run telemetry now; job orchestration hooks come next.")

    if refresh:
        st.rerun()

    provider_arg = None if provider_filter == "all" else provider_filter
    runs = repo.list_sync_runs(provider=provider_arg, limit=250)
    st.markdown("### Disabled-Job Queue Guard")
    disabled_queued_runs = [
        r
        for r in runs
        if (r.status or "").strip().lower() == "queued" and not is_sync_job_enabled(r.job_name, repo=repo)
    ]
    if disabled_queued_runs:
        st.warning(
            f"Found {len(disabled_queued_runs)} queued run(s) for disabled jobs. "
            "You can close them to keep queue state clean."
        )
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "run_id": r.id,
                        "provider": r.provider,
                        "job_name": r.job_name,
                        "status": r.status,
                        "retry_of_run_id": r.retry_of_run_id,
                        "retry_count": r.retry_count,
                        "started_at": r.started_at,
                    }
                    for r in disabled_queued_runs
                ]
            ),
            use_container_width=True,
        )
        with st.form("sync_finalize_disabled_queued_runs_form"):
            confirm_finalize = st.checkbox(
                "Mark these queued runs as failed/closed with a guard note.",
                value=False,
            )
            finalize_submit = st.form_submit_button("Finalize Disabled Queued Runs")
        if finalize_submit:
            if not ensure_permission(user, "update", "Finalize Disabled Queued Runs"):
                return
            if not confirm_finalize:
                render_workspace_error_state(
                    title="Disabled-Job Queue Guard",
                    detail="Confirm finalize action first.",
                )
            else:
                closed = 0
                now = utcnow_naive()
                for row in disabled_queued_runs:
                    guard_note = (
                        f"[auto-closed {now.isoformat(timespec='seconds')}] "
                        f"Queued run closed because job `{row.job_name}` is disabled by configuration."
                    )
                    existing_notes = (row.notes or "").strip()
                    merged_notes = f"{existing_notes}\n{guard_note}" if existing_notes else guard_note
                    repo.update_sync_run(
                        row.id,
                        {
                            "status": "failed",
                            "completed_at": now,
                            "notes": merged_notes,
                        },
                        actor=user.username,
                    )
                    closed += 1
                st.success(f"Finalized {closed} disabled queued run(s).")
                st.rerun()
    else:
        render_workspace_empty_state(
            title="Disabled-Job Queue Guard",
            detail="No queued runs are blocked by disabled job configuration.",
        )

    st.dataframe(
        pd.DataFrame(
            [
                {
                    "id": r.id,
                    "retry_of_run_id": r.retry_of_run_id,
                    "retry_count": r.retry_count,
                    "provider": r.provider,
                    "job_name": r.job_name,
                    "direction": r.direction,
                    "status": r.status,
                    "started_at": r.started_at,
                    "completed_at": r.completed_at,
                    "processed": r.records_processed,
                    "created": r.records_created,
                    "updated": r.records_updated,
                    "failed": r.records_failed,
                    "line_items_with_listing_link": r.line_items_with_listing_link,
                    "line_items_unmapped_sku": r.line_items_unmapped_sku,
                    "auto_listings_created": r.auto_listings_created,
                }
                for r in runs
            ]
        ),
        use_container_width=True,
    )

    st.markdown("### Run Lineage")
    run_index = {int(r.id): r for r in runs}
    lineage_groups: dict[int, list[object]] = {}
    for row in runs:
        root_id = _run_root_id(row, run_index)
        lineage_groups.setdefault(root_id, []).append(row)

    lineage_rows: list[dict] = []
    for root_id, group_rows in lineage_groups.items():
        for row in sorted(
            group_rows,
            key=lambda r: (getattr(r, "started_at", None) is None, getattr(r, "started_at", None), int(r.id)),
        ):
            lineage_rows.append(
                {
                    "root_run_id": root_id,
                    "lineage_terminal_status": _lineage_terminal_status(group_rows),
                    "run_id": row.id,
                    "parent_run_id": row.retry_of_run_id,
                    "chain_depth": _run_chain_depth(row, run_index),
                    "retry_count": row.retry_count,
                    "provider": row.provider,
                    "job_name": row.job_name,
                    "status": row.status,
                    "started_at": row.started_at,
                    "completed_at": row.completed_at,
                    "failed": row.records_failed,
                }
            )
    if lineage_rows:
        lineage_df = pd.DataFrame(lineage_rows)
        st.dataframe(lineage_df, use_container_width=True)
    else:
        render_workspace_empty_state(
            title="Run Lineage",
            detail="No lineage rows available yet.",
        )

    st.markdown("### Exception Queue")
    q1, q2, q3 = st.columns(3)
    with q1:
        unresolved_only = st.checkbox("Unresolved Only", value=True, key="sync_exception_unresolved_only")
    with q2:
        queue_limit = st.number_input("Queue Limit", min_value=50, max_value=1000, value=300, step=50)
    with q3:
        severity_filter = st.multiselect(
            "Severity",
            options=["error", "warning", "info", "critical"],
            default=["error", "warning", "critical"],
            key="sync_exception_severity_filter",
        )

    queue_pairs = repo.list_sync_error_queue(
        provider=provider_arg,
        unresolved_only=bool(unresolved_only),
        limit=int(queue_limit),
    )
    queue_pairs = [
        (err, run)
        for err, run in queue_pairs
        if not severity_filter or (err.severity or "").strip().lower() in {s.strip().lower() for s in severity_filter}
    ]
    queue_rows = [
        {
            "error_id": err.id,
            "run_id": run.id,
            "provider": run.provider,
            "job_name": run.job_name,
            "run_status": run.status,
            "severity": err.severity,
            "code": err.code,
            "message": err.message,
            "occurred_at": err.occurred_at,
            "resolved_at": err.resolved_at,
            "retry_count": run.retry_count,
            "retry_of_run_id": run.retry_of_run_id,
        }
        for err, run in queue_pairs
    ]
    ebay_network_rows = [
        row
        for row in queue_rows
        if str(row.get("provider") or "").strip().lower() == "ebay"
        and str(row.get("code") or "").strip().upper() == "EBAY_NETWORK_UNAVAILABLE"
        and row.get("resolved_at") is None
    ]
    if not queue_rows:
        render_workspace_empty_state(
            title="Exception Queue",
            detail="No queue rows match the current filters.",
        )
    else:
        render_workspace_loading_state(
            title="Exception Queue",
            detail=f"Showing {len(queue_rows)} rows.",
        )
    if ebay_network_rows:
        st.warning(
            f"{len(ebay_network_rows)} unresolved eBay network/DNS hold(s) are in the queue. "
            "Confirm DNS and outbound HTTPS connectivity before rotating eBay OAuth credentials."
        )
    st.dataframe(pd.DataFrame(queue_rows), use_container_width=True)
    _render_sync_copilot(repo, user, runs, queue_pairs)

    if queue_pairs:
        exception_map = {
            (
                f"Error #{err.id} | Run #{run.id} | {run.provider} | {err.code or 'no-code'} | "
                f"{(err.message or '')[:80]}"
            ): (err, run)
            for err, run in queue_pairs
        }
        selected_exception = st.selectbox(
            "Select Exception",
            options=list(exception_map.keys()),
            key="sync_exception_selected",
        )
        selected_error, selected_run = exception_map[selected_exception]

        cex1, cex2 = st.columns(2)
        with cex1:
            if st.button("Mark Exception Resolved", key=f"sync_exception_resolve_{selected_error.id}"):
                if not ensure_permission(user, "update", "Resolve Sync Exception"):
                    return
                repo.resolve_sync_error(selected_error.id, actor=user.username)
                st.success(f"Resolved sync error #{selected_error.id}.")
                st.rerun()
        with cex2:
            can_retry_exception, can_retry_exception_help = _retry_allowed_for_run(selected_run, repo)
            if st.button(
                "Retry Source Run",
                disabled=not can_retry_exception,
                key=f"sync_exception_retry_{selected_error.id}",
                help=can_retry_exception_help,
            ):
                if not ensure_permission(user, "create", "Retry Sync Run"):
                    return
                try:
                    retry_row = repo.retry_sync_run(selected_run.id, actor=user.username)
                    st.success(f"Created retry run #{retry_row.id} for run #{selected_run.id}.")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

        exception_dispatch = sync_job_dispatch_meta(selected_run.job_name)
        supports_exception_inline_retry = bool(exception_dispatch.get("supports_retry_execute_now")) and (
            selected_run.status in {"failed", "partial"}
        )
        if supports_exception_inline_retry:
            st.markdown("#### Exception Retry + Execute Now")
            if not ebay_pull_enabled:
                st.warning("`ebay_orders_pull_import` is disabled by configuration. Enable it to execute retries.")
            with st.form(f"sync_exception_retry_execute_{selected_error.id}"):
                ex1, ex2 = st.columns(2)
                with ex1:
                    retry_limit = st.number_input(
                        "Retry Fetch Limit",
                        min_value=1,
                        max_value=200,
                        value=max(1, int(selected_run.records_processed or 25)),
                        key=f"sync_exception_limit_{selected_error.id}",
                    )
                with ex2:
                    retry_offset = st.number_input(
                        "Retry Fetch Offset",
                        min_value=0,
                        value=0,
                        key=f"sync_exception_offset_{selected_error.id}",
                    )
                resolve_on_success = st.checkbox(
                    "Mark selected exception resolved on successful retry",
                    value=True,
                    key=f"sync_exception_resolve_on_success_{selected_error.id}",
                )
                retry_token = st.text_area(
                    "Access Token",
                    height=100,
                    key=f"sync_exception_token_{selected_error.id}",
                )
                retry_execute_submit = st.form_submit_button(
                    "Retry And Execute Now",
                    disabled=not ebay_pull_enabled,
                    help="This action is disabled while `SYNC_JOB_EBAY_ORDERS_PULL_IMPORT_ENABLED=false`.",
                )

            if retry_execute_submit:
                if not ensure_permission(user, "create", "Retry Sync Run"):
                    return
                if not ebay_pull_enabled:
                    st.error("`ebay_orders_pull_import` is disabled by configuration.")
                    return
                try:
                    retry_row = repo.retry_sync_run(selected_run.id, actor=user.username)
                    result = execute_sync_job(
                        repo,
                        job_name="ebay_orders_pull_import",
                        access_token=retry_token.strip(),
                        actor=user.username,
                        limit=int(retry_limit),
                        offset=int(retry_offset),
                        run_id=retry_row.id,
                    )
                    if bool(resolve_on_success):
                        repo.resolve_sync_error(selected_error.id, actor=user.username)
                    st.success(
                        f"Retry run #{result['run_id']} completed with status `{result['status']}`. "
                        f"processed={result['processed']}, created={result['created']}, "
                        f"updated={result['updated']}, failed={result['failed']}."
                    )
                    st.rerun()
                except Exception as exc:
                    st.error(f"Retry execution failed: {exc}")

    st.markdown("### Manual Run Record")
    with st.form("sync_manual_create_form"):
        f1, f2, f3, f4 = st.columns(4)
        with f1:
            provider = st.selectbox("Provider", ["ebay", "quickbooks", "shopify", "whatnot"])
        with f2:
            direction = st.selectbox("Direction", ["pull", "push", "bidirectional"])
        with f3:
            status = st.selectbox("Status", ["queued", "running", "success", "failed", "partial"])
        with f4:
            job_name = st.text_input("Job Name", value=f"{provider}_manual")
        notes = st.text_area("Notes", value="")
        submit = st.form_submit_button("Create Sync Run")

    if submit:
        if not ensure_permission(user, "create", "Create Sync Run"):
            return
        row = repo.create_sync_run(
            provider=provider,
            job_name=job_name.strip() or f"{provider}_manual",
            direction=direction,
            status=status,
            notes=notes,
            actor=user.username,
        )
        if status in {"success", "failed", "partial"}:
            repo.update_sync_run(
                row.id,
                {"completed_at": utcnow_naive()},
                actor=user.username,
            )
        st.success(f"Created sync run #{row.id}.")
        st.rerun()

    if runs:
        st.markdown("### Run Detail")
        run_map = {f"#{r.id} | {r.provider} | {r.job_name}": r for r in runs}
        run_keys = list(run_map.keys())
        focus_run_id = st.session_state.pop("sync_focus_run_id", None)
        selected_index = 0
        if focus_run_id is not None:
            for idx, key in enumerate(run_keys):
                row = run_map[key]
                if int(row.id) == int(focus_run_id):
                    selected_index = idx
                    break
        selected_key = st.selectbox("Select Run", run_keys, index=selected_index)
        selected = run_map[selected_key]

        events = repo.list_sync_events(selected.id, limit=500)
        errors = repo.list_sync_errors(selected.id, limit=500)

        ev_tab, er_tab, upd_tab = st.tabs(["Events", "Errors", "Update Run"])
        with ev_tab:
            if str(getattr(selected, "job_name", "") or "").strip().lower() == "ebay_store_categories_sync":
                summary_rows = _store_category_sync_event_summary_rows(events)
                if summary_rows:
                    st.markdown("#### Store Category Sync Summary")
                    st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "id": e.id,
                            "entity_type": e.entity_type,
                            "entity_id": e.entity_id,
                            "action": e.action,
                            "status": e.status,
                            "message": e.message,
                            "created_at": e.created_at,
                        }
                        for e in events
                    ]
                ),
                use_container_width=True,
            )
        with er_tab:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "id": e.id,
                            "severity": e.severity,
                            "code": e.code,
                            "message": e.message,
                            "occurred_at": e.occurred_at,
                            "resolved_at": e.resolved_at,
                        }
                        for e in errors
                    ]
                ),
                use_container_width=True,
            )
        with upd_tab:
            ctop1, ctop2 = st.columns(2)
            with ctop1:
                st.caption(f"Retry Parent Run ID: `{selected.retry_of_run_id}`")
            with ctop2:
                st.caption(f"Retry Count: `{selected.retry_count}`")

            can_retry, retry_help = _retry_allowed_for_run(selected, repo)
            if st.button(
                "Retry Failed Run",
                disabled=not can_retry,
                help=retry_help,
                key=f"sync_retry_run_{selected.id}",
            ):
                if not ensure_permission(user, "create", "Retry Sync Run"):
                    return
                try:
                    retry_row = repo.retry_sync_run(selected.id, actor=user.username)
                    st.success(f"Created retry run #{retry_row.id} for source run #{selected.id}.")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

            selected_dispatch = sync_job_dispatch_meta(selected.job_name)
            supports_inline_retry = bool(selected_dispatch.get("supports_retry_execute_now"))
            if supports_inline_retry and can_retry:
                st.markdown("#### Retry + Execute Now")
                if not ebay_pull_enabled:
                    st.warning("`ebay_orders_pull_import` is disabled by configuration. Enable it to execute retries.")
                with st.form(f"sync_retry_execute_form_{selected.id}"):
                    rc1, rc2 = st.columns(2)
                    with rc1:
                        retry_limit = st.number_input(
                            "Retry Fetch Limit",
                            min_value=1,
                            max_value=200,
                            value=max(1, int(selected.records_processed or 25)),
                        )
                    with rc2:
                        retry_offset = st.number_input("Retry Fetch Offset", min_value=0, value=0)
                    retry_token = st.text_area("Access Token", height=100, key=f"sync_retry_token_{selected.id}")
                    retry_execute_submit = st.form_submit_button(
                        "Retry And Execute Now",
                        disabled=not ebay_pull_enabled,
                        help="This action is disabled while `SYNC_JOB_EBAY_ORDERS_PULL_IMPORT_ENABLED=false`.",
                    )

                if retry_execute_submit:
                    if not ensure_permission(user, "create", "Retry Sync Run"):
                        return
                    if not ebay_pull_enabled:
                        st.error("`ebay_orders_pull_import` is disabled by configuration.")
                        return
                    try:
                        retry_row = repo.retry_sync_run(selected.id, actor=user.username)
                        result = execute_sync_job(
                            repo,
                            job_name="ebay_orders_pull_import",
                            access_token=retry_token.strip(),
                            actor=user.username,
                            limit=int(retry_limit),
                            offset=int(retry_offset),
                            run_id=retry_row.id,
                        )
                        st.success(
                            f"Retry run #{result['run_id']} completed with status `{result['status']}`. "
                            f"processed={result['processed']}, created={result['created']}, "
                            f"updated={result['updated']}, failed={result['failed']}."
                        )
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Retry execution failed: {exc}")

            with st.form("sync_update_run_form"):
                s1, s2 = st.columns(2)
                with s1:
                    update_status = st.selectbox(
                        "Status",
                        ["queued", "running", "success", "failed", "partial"],
                        index=["queued", "running", "success", "failed", "partial"].index(selected.status)
                        if selected.status in {"queued", "running", "success", "failed", "partial"}
                        else 0,
                    )
                    processed = st.number_input("Records Processed", min_value=0, value=int(selected.records_processed))
                    created = st.number_input("Records Created", min_value=0, value=int(selected.records_created))
                with s2:
                    updated = st.number_input("Records Updated", min_value=0, value=int(selected.records_updated))
                    failed = st.number_input("Records Failed", min_value=0, value=int(selected.records_failed))
                    completed = st.checkbox(
                        "Set Completed Timestamp",
                        value=bool(selected.completed_at),
                    )
                t1, t2, t3 = st.columns(3)
                with t1:
                    line_items_with_listing_link = st.number_input(
                        "line_items_with_listing_link",
                        min_value=0,
                        value=int(selected.line_items_with_listing_link),
                    )
                with t2:
                    line_items_unmapped_sku = st.number_input(
                        "line_items_unmapped_sku",
                        min_value=0,
                        value=int(selected.line_items_unmapped_sku),
                    )
                with t3:
                    auto_listings_created = st.number_input(
                        "auto_listings_created",
                        min_value=0,
                        value=int(selected.auto_listings_created),
                    )
                update_notes = st.text_area("Notes", value=selected.notes or "")
                run_update_submit = st.form_submit_button("Update Sync Run")
            if run_update_submit:
                if not ensure_permission(user, "update", "Update Sync Run"):
                    return
                updates = {
                    "status": update_status,
                    "records_processed": int(processed),
                    "records_created": int(created),
                    "records_updated": int(updated),
                    "records_failed": int(failed),
                    "line_items_with_listing_link": int(line_items_with_listing_link),
                    "line_items_unmapped_sku": int(line_items_unmapped_sku),
                    "auto_listings_created": int(auto_listings_created),
                    "notes": update_notes,
                    "completed_at": utcnow_naive() if completed else None,
                }
                repo.update_sync_run(selected.id, updates, actor=user.username)
                st.success("Sync run updated.")
                st.rerun()

    st.divider()
    render_workspace_task_completion(
        repo=repo,
        actor=user.username,
        workflow_key="sync",
        section_title="Workflow Completion: Sync",
        tasks=[
            ("Reviewed failed runs", "sync_failures_reviewed"),
            ("Retried failed run", "sync_run_retried"),
            ("Resolved run errors", "sync_errors_resolved"),
        ],
    )
    st.divider()
    render_workspace_feedback(
        repo=repo,
        actor=user.username,
        workspace_key="sync",
        section_title="Workspace Feedback",
    )
