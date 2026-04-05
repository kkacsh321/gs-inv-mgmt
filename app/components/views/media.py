import pandas as pd
import streamlit as st

from app.auth import current_user
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

    media_items = repo.list_media_assets()
    if not media_items:
        st.info("No media assets uploaded yet.")
        return

    st.dataframe(
        pd.DataFrame(
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
                }
                for m in media_items
            ]
        ),
        use_container_width=True,
    )
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
