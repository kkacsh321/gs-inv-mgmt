from __future__ import annotations

from app.services.runtime_settings import get_runtime_int


def cleanup_lifecycle_retention(
    repo,
    *,
    actor: str = "sync_runner",
) -> dict[str, int]:
    media_retain_days = max(1, int(get_runtime_int(repo, "lifecycle_media_archive_retain_days", 180)))
    listing_retain_days = max(1, int(get_runtime_int(repo, "lifecycle_listing_archive_retain_days", 365)))
    lot_retain_days = max(1, int(get_runtime_int(repo, "lifecycle_lot_archive_retain_days", 365)))
    product_retain_days = max(1, int(get_runtime_int(repo, "lifecycle_product_archive_retain_days", 365)))

    media_result = repo.cleanup_archived_media_assets(
        retain_days=media_retain_days,
        actor=actor,
    )
    listing_result = repo.cleanup_archived_listings(
        retain_days=listing_retain_days,
        actor=actor,
    )
    lot_result = repo.cleanup_archived_purchase_lots(
        retain_days=lot_retain_days,
        actor=actor,
    )
    product_result = repo.cleanup_archived_products(
        retain_days=product_retain_days,
        actor=actor,
    )

    return {
        "retain_days_media": int(media_retain_days),
        "retain_days_listing": int(listing_retain_days),
        "retain_days_lot": int(lot_retain_days),
        "retain_days_product": int(product_retain_days),
        "deleted_archived_media": int(media_result.get("deleted_archived_media") or 0),
        "deleted_archived_listings": int(listing_result.get("deleted_archived_listings") or 0),
        "deleted_archived_lots": int(lot_result.get("deleted_archived_lots") or 0),
        "deleted_archived_products": int(product_result.get("deleted_archived_products") or 0),
        "skipped_listings_with_dependencies": int(
            listing_result.get("skipped_listings_with_dependencies") or 0
        ),
        "skipped_lots_with_dependencies": int(lot_result.get("skipped_lots_with_dependencies") or 0),
        "skipped_products_with_dependencies": int(
            product_result.get("skipped_products_with_dependencies") or 0
        ),
    }
