import json
import os
import tempfile
import zipfile
from datetime import datetime
from io import BytesIO
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pandas as pd
import requests
import streamlit as st
from PIL import Image

from app.auth import UserContext, ensure_permission
from app.config import settings
from app.repository import InventoryRepository
from app.services.media_storage import MediaStorageService
from app.services.sync_jobs import is_sync_job_enabled
from app.utils.time import utcnow_naive

MARKETPLACES = ["ebay", "facebook_marketplace", "craigslist", "whatnot", "shopify", "local"]
MEDIA_UPLOAD_TYPES = ["jpg", "jpeg", "png", "webp", "gif", "mp4", "mov", "avi", "mkv", "webm"]
VIDEO_UPLOAD_TYPES = ["mp4", "mov", "avi", "mkv", "webm", "mpeg4"]


class InMemoryMediaFile:
    def __init__(self, *, name: str, content_type: str, data: bytes):
        self.name = name
        self.type = content_type
        self._data = data

    def read(self) -> bytes:
        return self._data


def as_money(value: float) -> str:
    return f"${value:,.2f}"


def safe_switch_page(
    page_path: str,
    *,
    error_prefix: str = "Navigation failed",
    info_message: str = "",
) -> bool:
    resolved = str(page_path or "").strip()
    if not resolved:
        st.error(f"{error_prefix}: missing target page path.")
        return False
    if not hasattr(st, "switch_page"):
        st.error(f"{error_prefix}: switch_page is unavailable in this runtime.")
        if str(info_message or "").strip():
            st.info(str(info_message).strip())
        return False
    try:
        st.switch_page(resolved)
        return True
    except Exception as exc:
        st.error(f"{error_prefix}: {exc}")
        if str(info_message or "").strip():
            st.info(str(info_message).strip())
        return False


def handoff_to_documents_draft(
    *,
    source_type: str,
    source_id: int,
    doc_type: str = "invoice",
    handoff_from: str = "unknown",
    tax_jurisdiction: str | None = None,
    tax_rate_percent: float | None = None,
    tax_shipping_taxable: bool | None = None,
    repo: InventoryRepository | None = None,
    actor: str | None = None,
) -> None:
    def _dedupe_handoffs(rows: list[dict], limit: int = 50) -> list[dict]:
        deduped: list[dict] = []
        seen: set[tuple[str, int, str]] = set()
        for row in rows:
            key = (
                str(row.get("source_type") or "").strip(),
                int(row.get("source_id") or 0),
                str(row.get("doc_type") or "").strip().lower(),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)
        return deduped[:limit]

    history_new = {
        "at": utcnow_naive().isoformat(),
        "source_type": str(source_type or "").strip(),
        "source_id": int(source_id),
        "doc_type": str(doc_type or "invoice").strip().lower(),
        "handoff_from": str(handoff_from or "").strip(),
    }
    history_session = list(st.session_state.get("documents_recent_handoffs") or [])
    history_merged = _dedupe_handoffs([history_new] + history_session, limit=50)
    st.session_state["documents_recent_handoffs"] = history_merged

    if repo is not None and str(actor or "").strip():
        setting_key = f"documents_recent_handoffs_json__{str(actor).strip().lower()}"
        persisted_rows: list[dict] = []
        try:
            from app.services.runtime_settings import get_runtime_str

            raw = get_runtime_str(repo, setting_key, "").strip()
            if raw:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    persisted_rows = [row for row in parsed if isinstance(row, dict)]
        except Exception:
            persisted_rows = []
        persisted_merged = _dedupe_handoffs([history_new] + persisted_rows, limit=50)
        try:
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key=setting_key,
                value=json.dumps(persisted_merged),
                value_type="str",
                description="Recent Documents handoff contexts (per-user) for quick reopen.",
                is_active=True,
                actor=str(actor).strip(),
            )
        except Exception:
            pass
        st.session_state["documents_recent_handoffs"] = _dedupe_handoffs(
            persisted_merged + history_merged,
            limit=50,
        )

    st.session_state["documents_prefill_source_type"] = str(source_type or "").strip()
    st.session_state["documents_prefill_source_id"] = int(source_id)
    st.session_state["documents_prefill_doc_type"] = str(doc_type or "invoice").strip().lower()
    if tax_jurisdiction is not None:
        st.session_state["documents_prefill_tax_jurisdiction"] = str(tax_jurisdiction).strip()
    else:
        st.session_state.pop("documents_prefill_tax_jurisdiction", None)
    if tax_rate_percent is not None:
        st.session_state["documents_prefill_tax_rate_percent"] = float(tax_rate_percent)
    else:
        st.session_state.pop("documents_prefill_tax_rate_percent", None)
    if tax_shipping_taxable is not None:
        st.session_state["documents_prefill_tax_shipping_taxable"] = bool(tax_shipping_taxable)
    else:
        st.session_state.pop("documents_prefill_tax_shipping_taxable", None)
    st.session_state["documents_prefill_applied"] = False
    st.session_state["workspace_handoff_from"] = str(handoff_from or "").strip()
    st.session_state["workspace_handoff_target"] = "documents"
    safe_switch_page("pages/16_Documents.py")


def render_help_panel(
    section_title: str,
    goal: str,
    steps: list[str],
    roadmap_phase: str,
) -> None:
    with st.expander(f"Help: {section_title}", expanded=False):
        st.markdown(f"**Goal:** {goal}")
        if steps:
            st.markdown("**How to use this page:**")
            for idx, step in enumerate(steps, start=1):
                st.markdown(f"{idx}. {step}")
        st.caption(f"Roadmap alignment: {roadmap_phase} (`ROADMAP.md`).")


def dataframe_to_xlsx_bytes(df: pd.DataFrame, sheet_name: str = "report") -> bytes:
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet_name)
    buffer.seek(0)
    return buffer.read()


def render_table_toolbar(
    *,
    df: pd.DataFrame,
    section_key: str,
    export_basename: str,
    active_filters: dict[str, object] | None = None,
    defer_exports: bool = False,
    row_count: int | None = None,
    export_df_factory: Callable[[], pd.DataFrame] | None = None,
) -> None:
    resolved_row_count = int(row_count) if row_count is not None else int(len(df.index))
    st.caption(f"Rows: {resolved_row_count}")
    filter_parts: list[str] = []
    for key, value in (active_filters or {}).items():
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            items = [str(v).strip() for v in value if str(v).strip()]
            if not items:
                continue
            filter_parts.append(f"{key}={', '.join(items)}")
            continue
        raw = str(value).strip()
        if raw:
            filter_parts.append(f"{key}={raw}")
    if filter_parts:
        st.caption("Active filters: " + " | ".join(filter_parts))
    if bool(defer_exports):
        load_exports = st.checkbox(
            "Load Table Exports (slower)",
            value=False,
            key=f"{section_key}_load_exports",
            help="Defers CSV/XLSX export byte generation unless explicitly requested.",
        )
        if not load_exports:
            st.caption(
                "Table exports are deferred. Enable `Load Table Exports (slower)` to prepare CSV/XLSX downloads."
            )
            return
    c1, c2 = st.columns(2)
    export_df = export_df_factory() if export_df_factory is not None else df
    with c1:
        csv_bytes = export_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="Download CSV",
            data=csv_bytes,
            file_name=f"{export_basename}.csv",
            mime="text/csv",
            key=f"{section_key}_export_csv",
            disabled=export_df.empty,
        )
    with c2:
        xlsx_bytes = dataframe_to_xlsx_bytes(export_df, sheet_name="data")
        st.download_button(
            label="Download XLSX",
            data=xlsx_bytes,
            file_name=f"{export_basename}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"{section_key}_export_xlsx",
            disabled=export_df.empty,
        )


def pretty_json(value: str) -> str:
    if not value:
        return "{}"
    try:
        return json.dumps(json.loads(value), indent=2)
    except json.JSONDecodeError:
        return value


def infer_media_type(content_type: str) -> str:
    if content_type.startswith("image/"):
        return "image"
    if content_type.startswith("video/"):
        return "video"
    return "other"


def generate_sku(category: str, metal_type: str) -> str:
    def _abbr(value: str, fallback: str) -> str:
        token = "".join(ch for ch in str(value or "") if ch.isalnum()).upper()
        if not token:
            token = fallback
        return token[:2]

    category_part = _abbr(category, "GN")
    metal_part = _abbr(metal_type, "MX")
    # Very short date code: 2-digit year + day-of-year (e.g., 26102)
    ts = utcnow_naive().strftime("%y%j")
    rand = uuid4().hex[:4].upper()
    return f"GS-{category_part}-{metal_part}-{ts}-{rand}"


def upload_media_for_listing(
    repo: InventoryRepository,
    storage: MediaStorageService,
    listing_id: int | None,
    product_id: int | None,
    uploaded_files,
    uploaded_by: str = "employee",
) -> tuple[int, list[str]]:
    if not uploaded_files:
        return 0, []

    uploaded = 0
    errors: list[str] = []
    for file in uploaded_files:
        file_bytes = file.read()
        content_type = file.type or "application/octet-stream"
        media_type = infer_media_type(content_type)
        try:
            result = storage.upload_file(
                file_name=file.name,
                file_bytes=file_bytes,
                content_type=content_type,
            )
            repo.create_media_asset(
                media_type=media_type,
                original_filename=file.name,
                content_type=result.content_type,
                size_bytes=result.size_bytes,
                s3_bucket=result.bucket,
                s3_key=result.key,
                s3_url=result.url,
                product_id=product_id,
                listing_id=listing_id,
                uploaded_by=uploaded_by.strip() or "employee",
            )
            uploaded += 1
        except Exception as exc:
            errors.append(f"{file.name}: {exc}")

    return uploaded, errors


def render_existing_media_attach_selector(
    *,
    repo: InventoryRepository,
    key_prefix: str,
    section_title: str,
    help_text: str,
    limit: int = 300,
    defer_load: bool = False,
    preloaded_rows: list[Any] | None = None,
) -> list[int]:
    with st.expander(section_title, expanded=False):
        st.caption(help_text)
        if defer_load:
            load_key = f"{key_prefix}_load_media"
            load_media = st.checkbox(
                "Load media options (slower)",
                value=bool(st.session_state.get(load_key, False)),
                key=load_key,
            )
            if not load_media:
                st.caption("Enable media loading to browse and attach existing assets.")
                return []
        if preloaded_rows is not None:
            rows = list(preloaded_rows)
        else:
            rows = repo.list_media_assets(limit=max(1, int(limit)))
        search_text = st.text_input(
            "Filter media (filename/type/id)",
            value="",
            key=f"{key_prefix}_search_text",
            placeholder="e.g. invoice, obverse, .jpg, #123",
        ).strip().lower()
        media_type_filter = st.selectbox(
            "Media Type Filter",
            options=["all", "image", "video", "document"],
            key=f"{key_prefix}_media_type_filter",
        )
        if media_type_filter != "all":
            rows = [row for row in rows if str(row.media_type or "").strip().lower() == media_type_filter]
        if search_text:
            def _matches(row: Any) -> bool:
                row_id = f"#{int(row.id)}".lower()
                filename = str(row.original_filename or "").strip().lower()
                content_type = str(row.content_type or "").strip().lower()
                media_type = str(row.media_type or "").strip().lower()
                return (
                    search_text in row_id
                    or search_text in filename
                    or search_text in content_type
                    or search_text in media_type
                )

            rows = [row for row in rows if _matches(row)]
        only_unlinked = st.checkbox(
            "Only show unlinked media",
            value=True,
            key=f"{key_prefix}_only_unlinked",
        )
        if only_unlinked:
            rows = [row for row in rows if row.product_id is None and row.listing_id is None]
        if not rows:
            st.caption("No media rows available for attachment.")
            return []
        option_map = {
            (
                f"#{int(row.id)} | {str(row.media_type or '').strip()} | "
                f"{str(row.original_filename or '').strip()} | "
                f"p={row.product_id if row.product_id is not None else '-'} | "
                f"l={row.listing_id if row.listing_id is not None else '-'}"
            ): int(row.id)
            for row in rows
        }
        selection_key = f"{key_prefix}_selected_labels"
        if selection_key not in st.session_state:
            st.session_state[selection_key] = []
        current_selection = list(st.session_state.get(selection_key) or [])
        if current_selection:
            visible = set(option_map.keys())
            st.session_state[selection_key] = [label for label in current_selection if label in visible]
        b1, b2 = st.columns(2)
        with b1:
            if st.button("Select All Visible", key=f"{key_prefix}_select_all"):
                st.session_state[selection_key] = list(option_map.keys())
                st.rerun()
        with b2:
            if st.button("Clear Selection", key=f"{key_prefix}_clear_all"):
                st.session_state[selection_key] = []
                st.rerun()
        selected_labels = st.multiselect(
            "Select existing media assets",
            options=list(option_map.keys()),
            key=selection_key,
        )
        return [option_map[label] for label in selected_labels if label in option_map]


def render_media_capture_inputs(
    *,
    key_prefix: str,
    upload_label: str = "Files",
    allow_enhanced: bool = False,
) -> list:
    uploaded_files = st.file_uploader(
        upload_label,
        type=MEDIA_UPLOAD_TYPES,
        accept_multiple_files=True,
        key=f"{key_prefix}_files_upload",
    )
    with st.expander("Camera Capture Tools (Optional)", expanded=False):
        capture_modes = ["Basic"]
        if allow_enhanced:
            capture_modes.append("Enhanced (streamlit-webrtc)")
        capture_mode = st.radio(
            "Capture Mode",
            capture_modes,
            horizontal=True,
            key=f"{key_prefix}_capture_mode",
        )
        st.caption(
            "Camera capture here is quick-capture only. On desktop browsers, Streamlit provides photo capture "
            "but does not provide an in-app video record button."
        )
        st.info(
            "Desktop video: record with your OS/webcam app, then upload via `Record/Select Video`.\n"
            "Mobile browsers usually offer `Take/Record Video` directly in the picker."
        )
        st.warning(
            "Photo quality note: browser camera snapshots can be lower resolution/compressed. "
            "For listing-grade images, use your camera app and upload the full-resolution file."
        )
        captured_photo = None
        if capture_mode == "Basic":
            captured_photo = st.camera_input(
                "Take Photo (Camera)",
                key=f"{key_prefix}_camera_photo",
            )
        elif allow_enhanced:
            try:
                from aiortc.contrib.media import MediaRecorder
                from streamlit_webrtc import WebRtcMode, webrtc_streamer
            except Exception:
                st.error(
                    "`streamlit-webrtc` is not installed in this environment. "
                    "Install dependencies and restart the app container."
                )
            else:
                st.caption("Use Start to open webcam. Click Stop to finalize the recorded video.")

                class FrameCaptureProcessor:
                    def __init__(self) -> None:
                        self.latest_frame = None

                    def recv(self, frame):
                        self.latest_frame = frame.to_ndarray(format="bgr24")
                        return frame

                recorder_path = st.session_state.get(f"{key_prefix}_webrtc_record_path")
                if not recorder_path:
                    suffix = ".webm"
                    tmp_dir = tempfile.gettempdir()
                    recorder_path = os.path.join(tmp_dir, f"{key_prefix}_{uuid4().hex}{suffix}")
                    st.session_state[f"{key_prefix}_webrtc_record_path"] = recorder_path

                def _recorder_factory():
                    return MediaRecorder(recorder_path)

                ctx = webrtc_streamer(
                    key=f"{key_prefix}_webrtc_streamer",
                    mode=WebRtcMode.SENDRECV,
                    media_stream_constraints={
                        "video": {
                            "width": {"ideal": 1920},
                            "height": {"ideal": 1080},
                            "frameRate": {"ideal": 30},
                        },
                        "audio": False,
                    },
                    video_processor_factory=FrameCaptureProcessor,
                    out_recorder_factory=_recorder_factory,
                    async_processing=True,
                )

                c1, c2 = st.columns(2)
                with c1:
                    if st.button("Capture Frame From Webcam", key=f"{key_prefix}_webrtc_capture_frame"):
                        latest = getattr(getattr(ctx, "video_processor", None), "latest_frame", None)
                        if latest is None:
                            st.warning("No frame available yet. Start webcam first.")
                        else:
                            image = Image.fromarray(latest[:, :, ::-1])
                            buffer = BytesIO()
                            image.save(buffer, format="JPEG", quality=95)
                            st.session_state[f"{key_prefix}_webrtc_captured_photo"] = buffer.getvalue()
                            st.success("Captured frame.")
                with c2:
                    if st.button("Use Last Recorded Video", key=f"{key_prefix}_webrtc_use_video"):
                        if os.path.exists(recorder_path) and os.path.getsize(recorder_path) > 0:
                            with open(recorder_path, "rb") as fh:
                                st.session_state[f"{key_prefix}_webrtc_recorded_video"] = fh.read()
                            st.success("Loaded recorded video.")
                        else:
                            st.warning("No recorded video found yet. Start then stop webcam stream first.")
        else:
            st.info("Enhanced capture is only available in non-form upload flows.")
        recorded_or_selected_video = st.file_uploader(
            "Record/Select Video (Optional)",
            type=VIDEO_UPLOAD_TYPES,
            accept_multiple_files=False,
            key=f"{key_prefix}_video_capture",
            help="On mobile: tap browse and choose camera/record video. On desktop: choose an existing file.",
        )
    media_inputs: list = []
    webrtc_photo = st.session_state.get(f"{key_prefix}_webrtc_captured_photo")
    webrtc_video = st.session_state.get(f"{key_prefix}_webrtc_recorded_video")
    if captured_photo is not None:
        media_inputs.append(captured_photo)
    if webrtc_photo:
        media_inputs.append(
            InMemoryMediaFile(
                name=f"{key_prefix}_capture.jpg",
                content_type="image/jpeg",
                data=webrtc_photo,
            )
        )
    if webrtc_video:
        media_inputs.append(
            InMemoryMediaFile(
                name=f"{key_prefix}_capture.webm",
                content_type="video/webm",
                data=webrtc_video,
            )
        )
    if recorded_or_selected_video is not None:
        media_inputs.append(recorded_or_selected_video)
    if uploaded_files:
        media_inputs.extend(uploaded_files)
    return media_inputs


def render_media_gallery(
    media_items,
    section_title: str = "Media Preview",
    columns: int = 3,
    storage: MediaStorageService | None = None,
) -> None:
    st.markdown(f"### {section_title}")
    if not media_items:
        st.info("No media available for preview.")
        return

    col_count = max(1, int(columns))
    gallery_cols = st.columns(col_count)
    for idx, media in enumerate(media_items):
        with gallery_cols[idx % col_count]:
            st.caption(
                f"#{media.id} • {media.media_type} • {media.original_filename}"
            )
            preview_bytes, preview_content_type, _ = load_media_bytes(media, storage=storage)

            if media.media_type == "image":
                if preview_bytes is not None:
                    try:
                        st.image(preview_bytes, use_container_width=True)
                    except Exception:
                        if media.s3_url:
                            try:
                                st.image(media.s3_url, use_container_width=True)
                            except Exception:
                                st.caption("Image preview unavailable (invalid image bytes/URL content).")
                        else:
                            st.caption("Image preview unavailable (invalid image bytes).")
                else:
                    try:
                        st.image(media.s3_url, use_container_width=True)
                    except Exception:
                        st.caption("Image preview unavailable (invalid image URL content).")
            elif media.media_type == "video":
                if preview_bytes is not None:
                    st.video(preview_bytes, format=preview_content_type)
                else:
                    st.video(media.s3_url)
            else:
                st.markdown(f"[Open Asset]({media.s3_url})")
            st.caption(
                f"product_id={media.product_id} | listing_id={media.listing_id} | size={media.size_bytes} bytes"
            )


def load_media_bytes(media, storage: MediaStorageService | None = None) -> tuple[bytes | None, str, str | None]:
    preview_bytes = None
    preview_content_type = media.content_type or "application/octet-stream"
    last_error: str | None = None
    if storage is not None and storage.enabled and media.s3_bucket and media.s3_key:
        try:
            preview_bytes, preview_content_type = storage.get_object_bytes(media.s3_bucket, media.s3_key)
            return preview_bytes, preview_content_type, None
        except Exception as exc:
            last_error = str(exc)
    if media.s3_url:
        try:
            response = requests.get(media.s3_url, timeout=20)
            response.raise_for_status()
            preview_bytes = response.content
            preview_content_type = response.headers.get("Content-Type", preview_content_type)
            return preview_bytes, preview_content_type, None
        except Exception as exc:
            last_error = str(exc)
    return None, preview_content_type, last_error


def render_media_file_actions(
    media_items,
    *,
    storage: MediaStorageService | None = None,
    key_prefix: str = "media_file_actions",
    section_title: str = "Media File Access",
    repo: InventoryRepository | None = None,
    actor: str = "system",
    user: UserContext | None = None,
) -> None:
    st.markdown(f"### {section_title}")
    if not media_items:
        st.info("No media files available.")
        return

    media_map = {
        f"#{m.id} | {m.media_type} | {m.original_filename}": m for m in media_items
    }
    selected_key = st.selectbox(
        "Select Media File",
        options=list(media_map.keys()),
        key=f"{key_prefix}_select",
    )
    selected = media_map[selected_key]
    data, content_type, err = load_media_bytes(selected, storage=storage)

    if selected.media_type == "image":
        if data is not None:
            try:
                st.image(data, use_container_width=True)
            except Exception:
                if selected.s3_url:
                    try:
                        st.image(selected.s3_url, use_container_width=True)
                    except Exception:
                        st.caption("Image preview unavailable (invalid image bytes/URL content).")
                else:
                    st.caption("Image preview unavailable (invalid image bytes).")
        elif selected.s3_url:
            try:
                st.image(selected.s3_url, use_container_width=True)
            except Exception:
                st.caption("Image preview unavailable (invalid image URL content).")
    elif selected.media_type == "video":
        if data is not None:
            st.video(data, format=content_type)
        elif selected.s3_url:
            st.video(selected.s3_url)
    else:
        st.caption("Preview not available for this file type.")

    c1, c2 = st.columns(2)
    with c1:
        if data is not None:
            st.download_button(
                label="Download Selected File",
                data=data,
                file_name=selected.original_filename,
                mime=content_type,
                key=f"{key_prefix}_download_{selected.id}",
            )
        else:
            st.caption("Download unavailable (could not load file bytes in app).")
    with c2:
        if selected.s3_url:
            st.markdown(f"[Open File URL]({selected.s3_url})")
        else:
            st.caption("No URL available.")

    if err:
        st.caption(f"Load warning: {err}")

    st.markdown("#### Bulk Download ZIP")
    bulk_labels = st.multiselect(
        "Select Media Files",
        options=list(media_map.keys()),
        key=f"{key_prefix}_bulk_select",
    )
    if bulk_labels:
        zip_buffer = BytesIO()
        added = 0
        skipped = 0
        used_names: dict[str, int] = {}
        with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for label in bulk_labels:
                media = media_map[label]
                data, _, _ = load_media_bytes(media, storage=storage)
                if data is None:
                    skipped += 1
                    continue
                base_name = (media.original_filename or f"media_{media.id}").strip() or f"media_{media.id}"
                count = used_names.get(base_name, 0)
                used_names[base_name] = count + 1
                name = base_name if count == 0 else f"{count}_{base_name}"
                zf.writestr(name, data)
                added += 1
        zip_buffer.seek(0)
        st.download_button(
            label=f"Download Selected as ZIP ({added} file(s))",
            data=zip_buffer.getvalue(),
            file_name=f"{key_prefix}_media_export_{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.zip",
            mime="application/zip",
            key=f"{key_prefix}_bulk_zip_download",
            disabled=added == 0,
        )
        if skipped:
            st.caption(f"Skipped {skipped} file(s) that could not be loaded.")

    st.markdown("#### Delete From S3 + Library")
    st.caption("Deletes the selected media object from S3 and removes it from the app media library.")
    confirm_delete = st.checkbox(
        "I understand this permanently deletes the selected media file.",
        value=False,
        key=f"{key_prefix}_confirm_delete",
    )
    if st.button(
        "Delete Selected Media",
        key=f"{key_prefix}_delete_selected_btn",
        disabled=not bool(confirm_delete),
    ):
        if repo is None:
            st.error("Delete unavailable: repository context missing.")
            return
        if user is not None:
            if not ensure_permission(user, "delete", "Delete Media Asset"):
                st.stop()
        try:
            if storage is not None and storage.enabled and selected.s3_bucket and selected.s3_key:
                storage.delete_object(selected.s3_bucket, selected.s3_key)
            deleted = repo.delete_media_asset(int(selected.id), actor=(actor or "system").strip() or "system")
            if not deleted:
                st.warning("Media row was already removed.")
            else:
                st.success(f"Deleted media #{int(selected.id)} from S3/library.")
            st.rerun()
        except Exception as exc:
            st.error(f"Delete failed: {exc}")


def render_ebay_push_history(
    repo: InventoryRepository,
    *,
    section_title: str = "eBay Push History",
    key_prefix: str = "ebay_push_history",
    limit: int = 300,
    actor: str = "",
    user: UserContext | None = None,
) -> None:
    st.markdown(f"### {section_title}")
    st.caption("Operational push run history for eBay jobs, including event/error drill-down.")

    runs = repo.list_sync_runs(provider="ebay", limit=max(100, int(limit)))
    push_runs = [
        run
        for run in runs
        if (run.direction or "").strip().lower() == "push"
        or (run.job_name or "").strip().lower().endswith("_push")
        or "push" in (run.job_name or "").strip().lower()
    ]
    if not push_runs:
        st.info("No eBay push runs found yet.")
        return

    status_options = sorted({(r.status or "").strip().lower() for r in push_runs if (r.status or "").strip()})
    selected_status = st.multiselect(
        "Status Filter",
        options=status_options,
        default=status_options,
        key=f"{key_prefix}_status_filter",
    )
    job_options = sorted({(r.job_name or "").strip() for r in push_runs if (r.job_name or "").strip()})
    selected_jobs = st.multiselect(
        "Job Filter",
        options=job_options,
        default=job_options,
        key=f"{key_prefix}_job_filter",
    )
    unresolved_only = st.checkbox("Only runs with unresolved errors", value=False, key=f"{key_prefix}_unresolved_only")

    filtered_runs = []
    for run in push_runs:
        run_status = (run.status or "").strip().lower()
        run_job = (run.job_name or "").strip()
        if selected_status and run_status not in {s.strip().lower() for s in selected_status}:
            continue
        if selected_jobs and run_job not in set(selected_jobs):
            continue
        if unresolved_only:
            run_errors = repo.list_sync_errors(run.id, limit=500)
            if not any(err.resolved_at is None for err in run_errors):
                continue
        filtered_runs.append(run)

    run_rows = []
    for run in filtered_runs:
        run_errors = repo.list_sync_errors(run.id, limit=500)
        unresolved_count = sum(1 for err in run_errors if err.resolved_at is None)
        run_rows.append(
            {
                "run_id": run.id,
                "job_name": run.job_name,
                "status": run.status,
                "started_at": run.started_at,
                "completed_at": run.completed_at,
                "processed": run.records_processed,
                "updated": run.records_updated,
                "failed": run.records_failed,
                "retry_count": run.retry_count,
                "retry_of_run_id": run.retry_of_run_id,
                "unresolved_errors": unresolved_count,
                "notes": run.notes,
            }
        )

    st.dataframe(pd.DataFrame(run_rows), use_container_width=True)
    if not filtered_runs:
        return

    run_map = {
        f"#{run.id} | {run.job_name} | {run.status} | failed={run.records_failed}": run for run in filtered_runs
    }
    selected_key = st.selectbox(
        "Select Push Run",
        options=list(run_map.keys()),
        key=f"{key_prefix}_run_select",
    )
    selected = run_map[selected_key]
    events = repo.list_sync_events(selected.id, limit=500)
    errors = repo.list_sync_errors(selected.id, limit=500)
    unresolved_errors = [err for err in errors if err.resolved_at is None]

    st.markdown("#### Quick Actions")
    retry_enabled_for_run = bool(is_sync_job_enabled(selected.job_name, repo=repo))
    retry_disabled_help = (
        f"Retry is disabled because `{selected.job_name}` is disabled by configuration."
        if not retry_enabled_for_run
        else "Create a queued retry run."
    )
    qa1, qa2, qa3 = st.columns(3)
    with qa1:
        if st.button(
            "Retry Run",
            key=f"{key_prefix}_retry_run_{selected.id}",
            disabled=not retry_enabled_for_run,
            help=retry_disabled_help,
        ):
            if user is None:
                st.error("User context is required for retry action.")
                st.stop()
            if not ensure_permission(user, "create", "Retry Sync Run"):
                st.stop()
            try:
                retry_row = repo.retry_sync_run(selected.id, actor=actor or "system")
                st.success(f"Created retry run #{retry_row.id} for source run #{selected.id}.")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
    with qa2:
        if st.button("Open in Sync", key=f"{key_prefix}_open_sync_{selected.id}"):
            st.session_state["sync_focus_run_id"] = selected.id
            safe_switch_page(
                "pages/18_Sync.py",
                error_prefix="Open Sync failed",
                info_message="Open Sync page from sidebar; selected run will stay preselected.",
            )
    with qa3:
        if st.button(
            "Resolve All Unresolved Errors",
            key=f"{key_prefix}_resolve_run_errors_{selected.id}",
            disabled=not unresolved_errors,
        ):
            if user is None:
                st.error("User context is required for resolve action.")
                st.stop()
            if not ensure_permission(user, "update", "Resolve Sync Exception"):
                st.stop()
            resolved = 0
            for err in unresolved_errors:
                repo.resolve_sync_error(err.id, actor=actor or "system")
                resolved += 1
            st.success(f"Resolved {resolved} error(s) for run #{selected.id}.")
            st.rerun()

    tab_events, tab_errors = st.tabs(["Events", "Errors"])
    with tab_events:
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
    with tab_errors:
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
