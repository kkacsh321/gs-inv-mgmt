from dataclasses import dataclass

import streamlit as st


STATUS_SEMANTIC_COLORS: dict[str, tuple[str, str]] = {
    "needs_action": ("#9A6700", "#FFFBEB"),
    "in_progress": ("#0C4A6E", "#E0F2FE"),
    "blocked": ("#991B1B", "#FEE2E2"),
    "done": ("#14532D", "#DCFCE7"),
    "unknown": ("#374151", "#F3F4F6"),
}


def normalize_status_semantic(raw_status: str | None) -> str:
    value = (raw_status or "").strip().lower()
    if value in {
        "draft",
        "pending",
        "needs_label",
        "not_shipped",
        "queued",
        "ready",
        "review_required",
        "needs_listing",
    }:
        return "needs_action"
    if value in {
        "active",
        "running",
        "processing",
        "in_progress",
        "in_transit",
        "label_created",
        "shipped",
    }:
        return "in_progress"
    if value in {
        "failed",
        "partial",
        "error",
        "exception",
        "delivery_exception",
        "blocked",
        "rejected",
    }:
        return "blocked"
    if value in {
        "done",
        "success",
        "completed",
        "delivered",
        "ended",
        "sold",
        "resolved",
        "approved",
    }:
        return "done"
    return "unknown"


def status_semantic_chip(raw_status: str | None, *, include_raw: bool = True) -> str:
    semantic = normalize_status_semantic(raw_status)
    fg, bg = STATUS_SEMANTIC_COLORS.get(semantic, STATUS_SEMANTIC_COLORS["unknown"])
    raw = (raw_status or "").strip().lower()
    label = semantic.replace("_", " ")
    if include_raw and raw and raw != semantic:
        label = f"{label} ({raw})"
    return (
        f"<span style='display:inline-block;padding:2px 8px;border-radius:999px;"
        f"border:1px solid {fg};background:{bg};color:{fg};font-size:0.8rem;"
        f"font-weight:600'>{label}</span>"
    )


def render_status_semantic_legend(*, title: str = "Status Semantics") -> None:
    st.caption(title)
    chips = [
        status_semantic_chip("draft", include_raw=False),
        status_semantic_chip("active", include_raw=False),
        status_semantic_chip("failed", include_raw=False),
        status_semantic_chip("completed", include_raw=False),
    ]
    st.markdown(" ".join(chips), unsafe_allow_html=True)


def render_workspace_empty_state(
    *,
    title: str,
    detail: str,
) -> None:
    st.success(f"{title}: {detail}")


def render_workspace_error_state(
    *,
    title: str,
    detail: str,
) -> None:
    st.error(f"{title}: {detail}")


def render_workspace_loading_state(
    *,
    title: str,
    detail: str,
) -> None:
    st.caption(f"{title}: {detail}")


def render_workspace_feedback(
    *,
    repo,
    actor: str,
    workspace_key: str,
    section_title: str = "Workspace Feedback",
) -> None:
    st.markdown(f"### {section_title}")
    st.caption("Quick operator feedback to improve layout and workflow fit.")
    c1, c2 = st.columns([1, 2])
    with c1:
        sentiment = st.radio(
            "Sentiment",
            options=["up", "down"],
            format_func=lambda v: "Helpful" if v == "up" else "Needs improvement",
            key=f"{workspace_key}_feedback_sentiment",
            horizontal=True,
            label_visibility="collapsed",
        )
    with c2:
        note = st.text_input(
            "Optional note",
            key=f"{workspace_key}_feedback_note",
            placeholder="What slowed you down or worked well?",
            label_visibility="collapsed",
        )
    if st.button("Submit Feedback", key=f"{workspace_key}_feedback_submit"):
        try:
            repo.record_audit_event(
                entity_type="workspace_feedback",
                entity_id=None,
                action="submit",
                actor=actor or "system",
                changes={
                    "workspace": workspace_key,
                    "sentiment": sentiment,
                    "note": (note or "").strip(),
                },
            )
            st.success("Feedback saved.")
        except Exception as exc:
            st.error(f"Unable to save feedback: {exc}")


def render_workspace_task_completion(
    *,
    repo,
    actor: str,
    workflow_key: str,
    tasks: list[tuple[str, str]],
    section_title: str = "Workflow Task Completion",
) -> None:
    st.markdown(f"### {section_title}")
    st.caption("Mark key workflow tasks as completed for rollout baseline evidence.")
    task_labels = [str(label or "").strip() for label, _ in tasks if str(label or "").strip()]
    task_key_by_label = {str(label).strip(): str(key).strip() for label, key in tasks if str(label).strip()}
    if not task_labels:
        st.info("No completion tasks configured for this workspace.")
        return
    c1, c2 = st.columns([2, 3])
    with c1:
        selected_label = st.selectbox(
            "Completed task",
            options=task_labels,
            key=f"{workflow_key}_task_completion_select",
        )
    with c2:
        note = st.text_input(
            "Optional completion note",
            key=f"{workflow_key}_task_completion_note",
            placeholder="Short context (operator, edge case, outcome).",
        )
    if st.button("Record Completion", key=f"{workflow_key}_task_completion_submit"):
        try:
            repo.record_audit_event(
                entity_type="workspace_task_completion",
                entity_id=None,
                action="complete",
                actor=actor or "system",
                changes={
                    "workflow": str(workflow_key or "").strip(),
                    "task_label": selected_label,
                    "task_key": task_key_by_label.get(selected_label, ""),
                    "note": str(note or "").strip(),
                },
            )
            st.success("Task completion recorded.")
        except Exception as exc:
            st.error(f"Unable to record completion: {exc}")


@dataclass
class CommandRailState:
    run_end_selected: bool
    run_relist_selected: bool
    run_add_selected_to_revise: bool
    run_remove_selected_from_revise: bool
    run_revise_queue: bool
    open_sync_page: bool
    clear_revise_queue: bool
    retry_run_now: bool
    resolve_run_errors: bool
    selected_sync_run_id: int | None


def render_ebay_command_rail(
    *,
    key_prefix: str,
    selected_count: int,
    sandbox_seller_ops_blocked: bool,
    sync_run_options: dict[str, int],
) -> CommandRailState:
    st.markdown("### Command Rail")
    st.caption("Run common eBay operations quickly from one action row.")
    r1, r2, r3, r4, r5 = st.columns(5)
    with r1:
        run_end_selected = st.button(
            "End Selected",
            key=f"{key_prefix}_end_selected",
            disabled=sandbox_seller_ops_blocked or selected_count <= 0,
            help="Withdraw selected linked offers and set local status to ended.",
        )
    with r2:
        run_relist_selected = st.button(
            "Relist Selected",
            key=f"{key_prefix}_relist_selected",
            disabled=sandbox_seller_ops_blocked or selected_count <= 0,
            help="Publish selected linked offers and update listing status to active.",
        )
    with r3:
        run_add_selected_to_revise = st.button(
            "Queue Selected",
            key=f"{key_prefix}_queue_selected",
            disabled=sandbox_seller_ops_blocked or selected_count <= 0,
            help="Add selected listings to the revise queue.",
        )
    with r4:
        open_sync_page = st.button(
            "Open Sync",
            key=f"{key_prefix}_open_sync",
            help="Open Sync page to inspect run details.",
        )
    with r5:
        clear_revise_queue = st.button(
            "Clear Revise Queue",
            key=f"{key_prefix}_clear_revise_queue",
            disabled=sandbox_seller_ops_blocked,
        )
    q1, q2 = st.columns(2)
    with q1:
        run_remove_selected_from_revise = st.button(
            "Unqueue Selected",
            key=f"{key_prefix}_unqueue_selected",
            disabled=sandbox_seller_ops_blocked or selected_count <= 0,
            help="Remove selected listings from the revise queue.",
        )
    with q2:
        run_revise_queue = st.button(
            "Run Revise Queue",
            key=f"{key_prefix}_run_revise_queue",
            disabled=sandbox_seller_ops_blocked,
            help="Run revise for all items currently in queue using current revise override fields.",
        )

    selected_sync_run_label = st.selectbox(
        "Sync Run (for retry/resolve)",
        options=["None"] + list(sync_run_options.keys()),
        key=f"{key_prefix}_sync_run_label",
    )
    selected_sync_run_id = (
        sync_run_options.get(selected_sync_run_label) if selected_sync_run_label != "None" else None
    )
    c1, c2 = st.columns(2)
    with c1:
        retry_run_now = st.button(
            "Retry Selected Run",
            key=f"{key_prefix}_retry_run",
            disabled=not selected_sync_run_id,
        )
    with c2:
        resolve_run_errors = st.button(
            "Resolve Unresolved Errors",
            key=f"{key_prefix}_resolve_run_errors",
            disabled=not selected_sync_run_id,
        )

    return CommandRailState(
        run_end_selected=run_end_selected,
        run_relist_selected=run_relist_selected,
        run_add_selected_to_revise=run_add_selected_to_revise,
        run_remove_selected_from_revise=run_remove_selected_from_revise,
        run_revise_queue=run_revise_queue,
        open_sync_page=open_sync_page,
        clear_revise_queue=clear_revise_queue,
        retry_run_now=retry_run_now,
        resolve_run_errors=resolve_run_errors,
        selected_sync_run_id=selected_sync_run_id,
    )
