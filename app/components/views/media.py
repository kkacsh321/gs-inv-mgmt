import pandas as pd
import streamlit as st

from app.auth import current_user, ensure_permission
from app.components.ui_helpers import build_listing_options, build_product_options
from app.components.views.shared import (
    render_help_panel,
    render_media_capture_inputs,
    render_media_file_actions,
    render_media_gallery,
    upload_media_for_listing,
)
from app.repository import InventoryRepository
from app.services.media_storage import MediaStorageService

def render_media(repo: InventoryRepository, storage: MediaStorageService) -> None:
    user = current_user()
    st.subheader("Media Library")
    st.caption("Upload product/listing photos and videos to S3 for listing workflows.")
    render_help_panel(
        section_title="Media Library",
        goal="Maintain a reusable media library connected to products and listings.",
        steps=[
            "Upload photos/videos and optionally associate them to product and listing records.",
            "Use consistent file naming to simplify marketplace listing workflows.",
            "Review uploaded assets in the table to validate linkage and storage path.",
            "Keep S3 enabled in environment config for shared access across environments.",
        ],
        roadmap_phase="v0.2 Operations Foundation",
    )

    if not storage.enabled:
        st.warning(
            "S3 media storage is not configured. Set STORAGE_PROVIDER=s3 and S3_BUCKET (plus AWS credentials)."
        )
        return

    products = repo.list_products()
    listings = repo.list_listings()

    uploaded_by = st.text_input("Uploaded By", value="employee")
    product_opts = build_product_options(products, include_none=True, include_id=False)
    listing_opts = build_listing_options(listings, include_none=True, include_id=True)
    product_key = st.selectbox("Associate to Product (Optional)", list(product_opts.keys()))
    listing_key = st.selectbox("Associate to Listing (Optional)", list(listing_opts.keys()))

    uploaded_files = render_media_capture_inputs(
        key_prefix="media_library_upload",
        upload_label="Files",
        allow_enhanced=True,
    )

    submitted = st.button("Upload to S3", key="media_library_upload_submit")

    if submitted:
        if not uploaded_files:
            st.error("Select at least one file.")
        elif not storage.enabled:
            st.error("S3 storage is not configured.")
        else:
            uploaded, errors = upload_media_for_listing(
                repo=repo,
                storage=storage,
                listing_id=listing_opts[listing_key],
                product_id=product_opts[product_key],
                uploaded_files=uploaded_files,
                uploaded_by=uploaded_by,
            )
            if uploaded:
                st.success(f"Uploaded {uploaded} media file(s).")
            for error in errors:
                st.error(f"Upload failed: {error}")

    include_archived = st.checkbox("Include Archived", value=False, key="media_library_include_archived")
    render_full_table = st.checkbox(
        "Render full media table (slower)",
        value=False,
        key="media_library_render_full_table",
    )
    preview_row_limit = int(
        st.number_input(
            "Media preview row limit",
            min_value=25,
            max_value=5000,
            value=250,
            step=25,
            key="media_library_preview_row_limit",
            help="Used when full media table rendering is disabled.",
        )
    )
    media_items = repo.list_media_assets(include_archived=bool(include_archived))
    if not media_items:
        st.info("No media assets uploaded yet.")
        return

    media_df = pd.DataFrame(
        [
            {
                "id": m.id,
                "media_type": m.media_type,
                "filename": m.original_filename,
                "content_type": m.content_type,
                "size_bytes": m.size_bytes,
                "product_id": m.product_id,
                "listing_id": m.listing_id,
                "s3_bucket": m.s3_bucket,
                "s3_key": m.s3_key,
                "url": m.s3_url,
                "uploaded_by": m.uploaded_by,
                "archived": bool(getattr(m, "is_archived", False)),
            }
            for m in media_items
        ]
    )
    bounded_media_df = media_df if render_full_table else media_df.head(max(1, int(preview_row_limit)))
    if not render_full_table and int(len(media_df)) > int(len(bounded_media_df)):
        st.caption(
            f"Showing preview rows only (`{int(len(bounded_media_df))}` of `{int(len(media_df))}`). "
            "Enable `Render full media table` for complete in-app rendering."
        )
    st.dataframe(bounded_media_df, use_container_width=True)
    render_media_gallery(
        media_items,
        section_title="Media Library Preview Gallery",
        columns=4,
        storage=storage,
    )
    render_media_file_actions(
        media_items,
        storage=storage,
        key_prefix="media_library_file_actions",
        section_title="Media Library File Access",
        repo=repo,
        actor=user.username,
        user=user,
    )

    st.markdown("#### Media Lifecycle")
    media_map = {f"#{m.id} | {m.media_type} | {m.original_filename}": m for m in media_items}
    selected_key = st.selectbox(
        "Select Media to Archive/Restore",
        list(media_map.keys()),
        key="media_library_lifecycle_select",
    )
    selected = media_map[selected_key]
    is_archived = bool(getattr(selected, "is_archived", False))
    if is_archived:
        st.info("Selected media is archived.")
        if st.button("Restore Media", key=f"media_library_restore_{selected.id}"):
            if not ensure_permission(user, "update", "Restore Media"):
                st.stop()
            try:
                repo.restore_media_asset(int(selected.id), actor=user.username)
                st.success("Media restored.")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
    else:
        blockers = repo.get_media_asset_archive_blockers(int(selected.id))
        blockers_total = sum(int(v or 0) for v in blockers.values())
        if blockers_total > 0:
            st.warning(
                "Archive preflight: active listing context detected "
                f"(linked_listing_active={int(blockers.get('linked_listing_active', 0))}, "
                f"linked_product_active_listings={int(blockers.get('linked_product_active_listings', 0))})."
            )
        force_archive_media = st.checkbox(
            "Force archive media despite active listing links",
            value=False,
            key=f"media_library_force_archive_{selected.id}",
            disabled=blockers_total <= 0,
            help="Required when this media is tied to active listing context.",
        )
        if st.button("Archive Media", key=f"media_library_archive_{selected.id}"):
            if not ensure_permission(user, "update", "Archive Media"):
                st.stop()
            try:
                repo.archive_media_asset(int(selected.id), actor=user.username, force=bool(force_archive_media))
                st.success("Media archived.")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
