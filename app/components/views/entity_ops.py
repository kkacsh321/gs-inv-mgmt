from datetime import datetime, date, time
import json

import pandas as pd
import streamlit as st

from app.config import settings
from app.repository import InventoryRepository


def _store_key() -> str:
    return "saved_filters_store"


def _filter_store() -> dict:
    if _store_key() not in st.session_state:
        st.session_state[_store_key()] = {}
    return st.session_state[_store_key()]


def _scope_bucket(scope: str, username: str) -> dict[str, dict]:
    store = _filter_store()
    scope_key = (scope or "").strip().lower()
    user_key = (username or "employee").strip().lower()
    store.setdefault(scope_key, {})
    store[scope_key].setdefault(user_key, {})
    return store[scope_key][user_key]


def _parse_saved_filter_payload(raw_payload: str | None) -> tuple[dict, list[str]]:
    try:
        data = json.loads(raw_payload or "{}")
    except Exception:
        return {}, []
    if isinstance(data, dict) and isinstance(data.get("filters"), dict):
        raw_roles = data.get("__meta__", {}).get("roles", [])
        if isinstance(raw_roles, list):
            roles = [str(r).strip().lower() for r in raw_roles if str(r).strip()]
        else:
            roles = []
        return dict(data["filters"]), roles
    if isinstance(data, dict):
        return dict(data), []
    return {}, []


def render_saved_filter_bar(
    *,
    repo: InventoryRepository,
    scope: str,
    username: str,
    current_filters: dict,
    role: str | None = None,
    allow_role_shared: bool = False,
) -> dict:
    bucket: dict[str, dict] = {}
    row_map: dict[str, object] = {}
    db_backed = True
    try:
        rows = repo.list_saved_filter_profiles(
            environment=settings.app_env,
            scope=scope,
            username=username,
            include_shared=True,
            active_only=True,
        )
        for row in rows:
            parsed_filters, role_scope = _parse_saved_filter_payload(row.filter_json or "{}")
            if bool(row.is_shared) and role_scope:
                if not role or role.strip().lower() not in set(role_scope):
                    continue
            owner = str(row.username or "").strip()
            visibility = "Shared" if bool(row.is_shared) else "Mine"
            default_tag = " | Default" if bool(row.is_default) else ""
            owner_tag = f" | Owner:{owner}" if bool(row.is_shared) else ""
            role_tag = f" | Role:{','.join(role_scope)}" if role_scope else ""
            label = f"{row.name} [{visibility}{default_tag}{owner_tag}{role_tag}]"
            if label in row_map:
                label = f"{label} #{row.id}"
            row_map[label] = row
            bucket[label] = parsed_filters
    except Exception:
        # Safe fallback for bootstrap/migration windows.
        db_backed = False
        bucket = _scope_bucket(scope, username)
        row_map = {}

    preset_names = sorted(bucket.keys())
    selected_name = st.selectbox(
        "Saved Filter",
        options=["None"] + preset_names,
        key=f"{scope}_saved_filter_select",
    )
    c1, c2, c3 = st.columns(3)
    with c1:
        apply_clicked = st.button("Apply Saved Filter", key=f"{scope}_saved_filter_apply")
    with c2:
        delete_clicked = st.button("Delete Saved Filter", key=f"{scope}_saved_filter_delete")
    with c3:
        with st.form(f"{scope}_saved_filter_save_form"):
            save_name = st.text_input("Save Current As", key=f"{scope}_saved_filter_name")
            save_shared = st.checkbox("Team-shared", value=False, key=f"{scope}_saved_filter_shared")
            save_role_shared = False
            if allow_role_shared:
                save_role_shared = st.checkbox(
                    f"Role-shared ({(role or '').strip() or 'current role'})",
                    value=False,
                    key=f"{scope}_saved_filter_role_shared",
                    help="Shared filter visible to users with the same role.",
                )
            save_default = st.checkbox("Set as default for this scope", value=False, key=f"{scope}_saved_filter_default")
            save_clicked = st.form_submit_button("Save Filter")

    effective = dict(current_filters)
    default_session_key = f"{scope}_default_loaded_{settings.app_env}_{username}"
    if default_session_key not in st.session_state:
        st.session_state[default_session_key] = False

    default_label = None
    if db_backed:
        own_default = None
        shared_default = None
        for label, row in row_map.items():
            if not bool(getattr(row, "is_default", False)):
                continue
            if str(getattr(row, "username", "")).strip() == username.strip() and not bool(getattr(row, "is_shared", False)):
                own_default = label
                break
            if bool(getattr(row, "is_shared", False)) and shared_default is None:
                shared_default = label
        default_label = own_default or shared_default
        if default_label and not st.session_state.get(default_session_key):
            effective = dict(bucket.get(default_label, current_filters))
            st.session_state[default_session_key] = True

    if apply_clicked and selected_name != "None":
        effective = dict(bucket.get(selected_name, current_filters))
        st.session_state[default_session_key] = True
        st.success(f"Applied saved filter `{selected_name}`.")
    if save_clicked:
        normalized_name = (save_name or "").strip()
        if not normalized_name:
            st.error("Filter name is required.")
        else:
            payload_obj: dict = dict(current_filters)
            if db_backed and allow_role_shared and bool(save_role_shared) and role:
                payload_obj = {
                    "filters": dict(current_filters),
                    "__meta__": {"roles": [str(role).strip().lower()]},
                }
            if db_backed:
                repo.upsert_saved_filter_profile(
                    environment=settings.app_env,
                    username=username,
                    scope=scope,
                    name=normalized_name,
                    filter_json=json.dumps(payload_obj),
                    is_shared=bool(save_shared or save_role_shared),
                    is_default=bool(save_default),
                    is_active=True,
                    actor=username,
                )
            else:
                bucket[normalized_name] = dict(current_filters)
            st.success(f"Saved filter `{normalized_name}`.")
    if delete_clicked and selected_name != "None":
        if db_backed:
            row = row_map.get(selected_name)
            if row is None:
                st.error("Saved filter not found.")
            elif str(row.username or "").strip() != username.strip():
                st.error("Only the filter owner can delete this saved filter.")
            else:
                repo.delete_saved_filter_profile_by_id(profile_id=row.id, actor=username)
        else:
            bucket.pop(selected_name, None)
        if not db_backed or (selected_name in row_map and str(row_map[selected_name].username or "").strip() == username.strip()):
            st.success(f"Deleted saved filter `{selected_name}`.")
    return effective


def render_entity_timeline(
    repo: InventoryRepository,
    *,
    entity_type: str,
    entity_id: int | str,
    title: str = "Timeline",
) -> None:
    resolved_entity_type = (entity_type or "").strip().lower()
    resolved_entity_id = str(entity_id).strip()
    if not resolved_entity_type or not resolved_entity_id:
        st.info("Timeline requires entity type and ID.")
        return

    try:
        audit_logs = repo.list_audit_logs_for_entity(
            entity_type=resolved_entity_type,
            entity_id=resolved_entity_id,
            limit=300,
        )
    except Exception:
        audit_logs = []

    sync_events = repo.list_sync_events_for_entity(
        entity_type=resolved_entity_type,
        entity_id=resolved_entity_id,
        limit=300,
    )
    sync_run_index = {run.id: run for run in repo.list_sync_runs(limit=500)}

    timeline_rows: list[dict] = []
    for row in audit_logs:
        timeline_rows.append(
            {
                "time": row.created_at,
                "source": "audit",
                "action": row.action,
                "status": "",
                "sync_run_id": None,
                "actor": row.actor,
                "message": "",
                "detail": row.changes_json,
            }
        )
    for row in sync_events:
        run = sync_run_index.get(row.sync_run_id)
        timeline_rows.append(
            {
                "time": row.created_at,
                "source": "sync_event",
                "action": row.action,
                "status": row.status,
                "sync_run_id": row.sync_run_id,
                "actor": f"{(run.provider if run else '')}:{(run.job_name if run else '')}",
                "message": row.message,
                "detail": row.payload_json,
            }
        )

    timeline_rows = sorted(timeline_rows, key=lambda item: item["time"], reverse=True)
    st.markdown(f"#### {title}")
    if not timeline_rows:
        st.info("No audit/sync timeline rows found.")
        return

    def _to_date(value: datetime | None) -> date | None:
        if value is None:
            return None
        return value.date()

    dated_rows = [row for row in timeline_rows if row.get("time") is not None]
    min_dt = min((row["time"] for row in dated_rows), default=None)
    max_dt = max((row["time"] for row in dated_rows), default=None)
    default_start = _to_date(min_dt) or date.today()
    default_end = _to_date(max_dt) or date.today()

    st.caption("Timeline Filters")
    t1, t2, t3, t4 = st.columns(4)
    with t1:
        source_options = sorted({str(row.get("source") or "") for row in timeline_rows if str(row.get("source") or "")})
        source_filter = st.multiselect(
            "Source",
            options=source_options,
            default=[],
            key=f"timeline_source_filter_{resolved_entity_type}_{resolved_entity_id}",
        )
    with t2:
        action_options = sorted({str(row.get("action") or "") for row in timeline_rows if str(row.get("action") or "")})
        action_filter = st.multiselect(
            "Action",
            options=action_options,
            default=[],
            key=f"timeline_action_filter_{resolved_entity_type}_{resolved_entity_id}",
        )
    with t3:
        status_options = sorted({str(row.get("status") or "") for row in timeline_rows if str(row.get("status") or "")})
        status_filter = st.multiselect(
            "Status",
            options=status_options,
            default=[],
            key=f"timeline_status_filter_{resolved_entity_type}_{resolved_entity_id}",
        )
    with t4:
        date_range = st.date_input(
            "Date Range",
            value=(default_start, default_end),
            key=f"timeline_date_filter_{resolved_entity_type}_{resolved_entity_id}",
        )

    start_date = default_start
    end_date = default_end
    if isinstance(date_range, tuple) and len(date_range) == 2:
        start_date = date_range[0] or default_start
        end_date = date_range[1] or default_end
    elif isinstance(date_range, date):
        start_date = date_range
        end_date = date_range
    if start_date > end_date:
        start_date, end_date = end_date, start_date
    start_dt = datetime.combine(start_date, time.min)
    end_dt = datetime.combine(end_date, time.max)

    source_set = {s.strip().lower() for s in source_filter if str(s).strip()}
    action_set = {s.strip().lower() for s in action_filter if str(s).strip()}
    status_set = {s.strip().lower() for s in status_filter if str(s).strip()}
    filtered_timeline_rows = []
    for row in timeline_rows:
        row_time = row.get("time")
        if row_time is not None and (row_time < start_dt or row_time > end_dt):
            continue
        if source_set and str(row.get("source") or "").strip().lower() not in source_set:
            continue
        if action_set and str(row.get("action") or "").strip().lower() not in action_set:
            continue
        if status_set and str(row.get("status") or "").strip().lower() not in status_set:
            continue
        filtered_timeline_rows.append(row)
    st.caption(f"Showing {len(filtered_timeline_rows)} of {len(timeline_rows)} timeline rows")
    if not filtered_timeline_rows:
        st.info("No timeline rows match current filters.")
        return

    st.dataframe(
        pd.DataFrame(
            [
                {
                    "time": row["time"].isoformat() if row["time"] else "",
                    "source": row["source"],
                    "action": row["action"],
                    "status": row["status"],
                    "sync_run_id": row["sync_run_id"] or "",
                    "actor": row["actor"],
                    "message": row["message"],
                }
                for row in filtered_timeline_rows
            ]
        ),
        use_container_width=True,
    )
    with st.expander("Timeline Raw Detail", expanded=False):
        idx = st.number_input(
            "Row #",
            min_value=1,
            max_value=len(filtered_timeline_rows),
            value=1,
            step=1,
            key=f"timeline_row_{resolved_entity_type}_{resolved_entity_id}",
        )
        selected_row = filtered_timeline_rows[int(idx) - 1]
        raw_value = selected_row["detail"] or "{}"
        try:
            parsed = json.loads(raw_value)
            st.json(parsed)
        except Exception:
            parsed = None
            st.code(raw_value)

        if selected_row.get("source") == "audit" and isinstance(parsed, dict):
            change_rows: list[dict] = []
            for field, payload in parsed.items():
                if not isinstance(payload, dict):
                    continue
                if "before" in payload or "after" in payload:
                    change_rows.append(
                        {
                            "field": str(field),
                            "before": payload.get("before"),
                            "after": payload.get("after"),
                        }
                    )
            if change_rows:
                st.caption("Change Summary")
                st.dataframe(pd.DataFrame(change_rows), use_container_width=True)
            if st.button(
                "Open Search & Edit",
                key=f"timeline_open_search_edit_{resolved_entity_type}_{resolved_entity_id}",
            ):
                if hasattr(st, "switch_page"):
                    st.switch_page("app/pages/10_Search_Edit.py")
                else:
                    st.info("Open `app/pages/10_Search_Edit.py` from the sidebar.")

        selected_sync_run_id = selected_row.get("sync_run_id")
        if selected_sync_run_id:
            if st.button(
                f"Open Sync Run #{selected_sync_run_id}",
                key=f"timeline_open_sync_{resolved_entity_type}_{resolved_entity_id}_{selected_sync_run_id}",
            ):
                st.session_state["sync_focus_run_id"] = int(selected_sync_run_id)
                if hasattr(st, "switch_page"):
                    st.switch_page("app/pages/18_Sync.py")
                else:
                    st.info("Open `app/pages/18_Sync.py` from the sidebar.")


def render_standard_row_actions(
    repo: InventoryRepository,
    *,
    entity_type: str,
    rows: list[dict],
    id_field: str = "id",
    title: str = "Row Actions",
    search_edit_page: str = "app/pages/10_Search_Edit.py",
    edit_action_label: str = "Open Search & Edit",
    edit_action_caption: str = "Use Search & Edit for canonical updates and audit-safe edits.",
) -> None:
    if not rows:
        st.info("No rows available for actions.")
        return
    label_map: dict[str, dict] = {}
    for row in rows:
        entity_id = row.get(id_field)
        if entity_id is None:
            continue
        label = f"#{entity_id} | {(row.get('title') or row.get('sku') or row.get('marketplace') or '').strip()}"
        label_map[label] = row
    if not label_map:
        st.info("No actionable rows found.")
        return

    st.markdown(f"### {title}")
    selected_label = st.selectbox(
        "Select Row",
        options=list(label_map.keys()),
        key=f"{entity_type}_{title}_select".replace(" ", "_").lower(),
    )
    selected = label_map[selected_label]
    entity_id = selected.get(id_field)
    tab_view, tab_edit, tab_sync, tab_timeline = st.tabs(["View", "Edit", "Sync", "Timeline"])

    with tab_view:
        st.json(selected)

    with tab_edit:
        st.caption(edit_action_caption)
        if st.button(
            edit_action_label,
            key=f"{entity_type}_{entity_id}_open_search_edit",
            use_container_width=True,
        ):
            if hasattr(st, "switch_page"):
                st.switch_page(search_edit_page)
            else:
                st.info(f"Open `{search_edit_page}` from the sidebar.")

    with tab_sync:
        events = repo.list_sync_events_for_entity(entity_type=entity_type, entity_id=entity_id, limit=100)
        if not events:
            st.info("No sync events for this entity.")
        else:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "sync_run_id": row.sync_run_id,
                            "action": row.action,
                            "status": row.status,
                            "message": row.message,
                            "created_at": row.created_at.isoformat() if row.created_at else "",
                        }
                        for row in events
                    ]
                ),
                use_container_width=True,
            )

    with tab_timeline:
        render_entity_timeline(
            repo,
            entity_type=entity_type,
            entity_id=entity_id,
            title=f"{entity_type.title()} #{entity_id} Timeline",
        )
