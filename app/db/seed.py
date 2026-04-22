import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os

from sqlalchemy import delete, select

from app.config import settings
from app.db.models import (
    AppUser,
    CoinReferenceCatalog,
    MarketplaceListing,
    MediaAsset,
    Product,
    ProductLotAssignment,
    PurchaseLot,
    Sale,
)
from app.db.session import SessionLocal
from app.repository import InventoryRepository


def _utc(days_ago: int) -> datetime:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(tzinfo=None)


def _assert_not_prod() -> None:
    if settings.app_env.lower() == "prod":
        raise RuntimeError("Seeding is blocked in APP_ENV=prod.")


def _wipe_seed_tables(repo: InventoryRepository) -> None:
    repo.db.execute(delete(Sale))
    repo.db.execute(delete(MediaAsset))
    repo.db.execute(delete(MarketplaceListing))
    repo.db.execute(delete(ProductLotAssignment))
    repo.db.execute(delete(Product))
    repo.db.execute(delete(PurchaseLot))
    repo.db.execute(delete(CoinReferenceCatalog))
    repo.db.commit()


def seed_dev_data(wipe: bool = False) -> dict:
    _assert_not_prod()

    db = SessionLocal()
    repo = InventoryRepository(db)
    counts = {
        "lots": 0,
        "products": 0,
        "assignments": 0,
        "listings": 0,
        "sales": 0,
        "media": 0,
        "coin_refs": 0,
        "app_users": 0,
    }
    try:
        if wipe:
            _wipe_seed_tables(repo)

        # Local E2E auth user for Playwright (overridable via env vars).
        # Seeded in non-prod environments only (guarded by _assert_not_prod()).
        e2e_username = (os.getenv("E2E_USERNAME", "e2e") or "e2e").strip()
        e2e_password = (os.getenv("E2E_PASSWORD", "e2e-password-123") or "e2e-password-123").strip()
        e2e_display_name = (os.getenv("E2E_DISPLAY_NAME", "E2E Local User") or "E2E Local User").strip()
        e2e_email = (os.getenv("E2E_EMAIL", "e2e@goldenstackers.local") or "e2e@goldenstackers.local").strip()
        e2e_role = (os.getenv("E2E_ROLE", "admin") or "admin").strip().lower()
        ensure_e2e_permissions = str(
            os.getenv("E2E_ENSURE_ROLE_PERMISSIONS", "true") or "true"
        ).strip().lower() in {"1", "true", "yes", "y", "on"}
        existing_e2e = db.scalar(select(AppUser).where(AppUser.username == e2e_username))
        repo.upsert_app_user(
            username=e2e_username,
            role=e2e_role,
            display_name=e2e_display_name,
            email=e2e_email,
            password=e2e_password,
            is_active=True,
            actor="seed-script",
        )
        if existing_e2e is None:
            counts["app_users"] += 1

        # Keep the default local admin login deterministic for e2e/browser tests.
        # This avoids brittle username-selector interactions in Streamlit sidebar auth controls.
        existing_admin = db.scalar(select(AppUser).where(AppUser.username == "admin"))
        repo.upsert_app_user(
            username="admin",
            role="admin",
            display_name="Admin User",
            email="admin@goldenstackers.local",
            password=e2e_password,
            is_active=True,
            actor="seed-script",
        )
        if existing_admin is None:
            counts["app_users"] += 1

        # Keep local e2e role permissions deterministic for browser tests without
        # overwriting custom permissions: only add missing required permissions.
        if ensure_e2e_permissions:
            required_e2e_permissions = {
                "read",
                "create",
                "update",
                "bulk_update",
                "export",
                "manage_settings",
                "manage_profiles",
                "ai_chat_use",
                "ai_comp_use",
                "ai_coin_grade",
                "ai_coin_identify",
            }
            current_permission_map = repo.list_role_permissions()
            current_role_permissions = set(current_permission_map.get(e2e_role, set()))
            missing_required_permissions = required_e2e_permissions - current_role_permissions
            if missing_required_permissions:
                repo.set_role_permissions(
                    e2e_role,
                    current_role_permissions | required_e2e_permissions,
                    actor="seed-script",
                )

        # Keep sandbox seller-ops controls enabled for deterministic e2e flows
        # in non-production environments.
        if hasattr(repo, "upsert_runtime_setting"):
            repo.upsert_runtime_setting(
                environment=settings.app_env,
                key="ebay_allow_sandbox_seller_ops",
                value="true",
                value_type="bool",
                description="Enable sandbox seller operations during non-prod seed/e2e.",
                is_active=True,
                actor="seed-script",
            )

        lot_specs = [
            {
                "lot_code": "LOT-2026-001",
                "vendor": "Colorado Coin Exchange",
                "purchase_date": _utc(45),
                "total_cost": Decimal("6150.00"),
                "notes": "Mixed silver rounds and US junk silver.",
            },
            {
                "lot_code": "LOT-2026-002",
                "vendor": "Estate Auction #77",
                "purchase_date": _utc(30),
                "total_cost": Decimal("4200.00"),
                "notes": "Collectible coins and antique pocket watches.",
            },
            {
                "lot_code": "LOT-2026-003",
                "vendor": "Bullion Direct Wholesale",
                "purchase_date": _utc(14),
                "total_cost": Decimal("9800.00"),
                "notes": "Gold and platinum bars.",
            },
        ]

        lot_by_code: dict[str, PurchaseLot] = {}
        for spec in lot_specs:
            existing = db.scalar(select(PurchaseLot).where(PurchaseLot.lot_code == spec["lot_code"]))
            if existing:
                lot_by_code[spec["lot_code"]] = existing
                continue
            lot = repo.create_purchase_lot(**spec)
            counts["lots"] += 1
            lot_by_code[spec["lot_code"]] = lot

        product_specs = [
            {
                "sku": "GS-BUL-SIL-001",
                "title": "1 oz Silver Buffalo Round",
                "category": "bullion",
                "description": "Generic silver round, 0.999 fine.",
                "metal_type": "silver",
                "weight_oz": Decimal("1.0000"),
                "package_weight_oz": Decimal("1.6000"),
                "package_length_in": Decimal("2.00"),
                "package_width_in": Decimal("2.00"),
                "package_height_in": Decimal("0.50"),
                "acquisition_cost": Decimal("29.50"),
                "current_quantity": 25,
                "acquired_at": _utc(44),
                "lot_code": "LOT-2026-001",
            },
            {
                "sku": "GS-COI-SIL-002",
                "title": "1964 Kennedy Half Dollar",
                "category": "coins",
                "description": "90% silver US coin.",
                "metal_type": "silver",
                "weight_oz": Decimal("0.3617"),
                "package_weight_oz": Decimal("0.8000"),
                "package_length_in": Decimal("2.00"),
                "package_width_in": Decimal("2.00"),
                "package_height_in": Decimal("0.25"),
                "acquisition_cost": Decimal("10.75"),
                "current_quantity": 40,
                "acquired_at": _utc(43),
                "lot_code": "LOT-2026-001",
            },
            {
                "sku": "GS-COL-MIX-003",
                "title": "1881 Morgan Dollar (VF)",
                "category": "collectibles",
                "description": "Collector-grade Morgan silver dollar.",
                "metal_type": "silver",
                "weight_oz": Decimal("0.7734"),
                "package_weight_oz": Decimal("1.1000"),
                "package_length_in": Decimal("2.25"),
                "package_width_in": Decimal("2.25"),
                "package_height_in": Decimal("0.50"),
                "acquisition_cost": Decimal("38.00"),
                "current_quantity": 8,
                "acquired_at": _utc(29),
                "lot_code": "LOT-2026-002",
            },
            {
                "sku": "GS-ANT-MIX-004",
                "title": "Waltham Antique Pocket Watch",
                "category": "antiques",
                "description": "Antique pocket watch, serviced.",
                "metal_type": "mixed",
                "weight_oz": None,
                "package_weight_oz": Decimal("12.0000"),
                "package_length_in": Decimal("4.00"),
                "package_width_in": Decimal("4.00"),
                "package_height_in": Decimal("2.00"),
                "acquisition_cost": Decimal("185.00"),
                "current_quantity": 3,
                "acquired_at": _utc(28),
                "lot_code": "LOT-2026-002",
            },
            {
                "sku": "GS-BUL-GOL-005",
                "title": "1 oz Gold Bar (PAMP)",
                "category": "bullion",
                "description": "Sealed assay card, 0.9999 fine.",
                "metal_type": "gold",
                "weight_oz": Decimal("1.0000"),
                "package_weight_oz": Decimal("1.7000"),
                "package_length_in": Decimal("3.00"),
                "package_width_in": Decimal("2.00"),
                "package_height_in": Decimal("0.50"),
                "acquisition_cost": Decimal("2145.00"),
                "current_quantity": 4,
                "acquired_at": _utc(13),
                "lot_code": "LOT-2026-003",
            },
            {
                "sku": "GS-BUL-PLA-006",
                "title": "1 oz Platinum Bar",
                "category": "bullion",
                "description": "Cast platinum bar, 0.9995 fine.",
                "metal_type": "platinum",
                "weight_oz": Decimal("1.0000"),
                "package_weight_oz": Decimal("1.7000"),
                "package_length_in": Decimal("3.00"),
                "package_width_in": Decimal("2.00"),
                "package_height_in": Decimal("0.50"),
                "acquisition_cost": Decimal("995.00"),
                "current_quantity": 6,
                "acquired_at": _utc(12),
                "lot_code": "LOT-2026-003",
            },
        ]

        product_by_sku: dict[str, Product] = {}
        for spec in product_specs:
            existing = db.scalar(select(Product).where(Product.sku == spec["sku"]))
            if existing:
                product_by_sku[spec["sku"]] = existing
                continue
            product = repo.create_product(
                sku=spec["sku"],
                title=spec["title"],
                category=spec["category"],
                description=spec["description"],
                metal_type=spec["metal_type"],
                weight_oz=spec["weight_oz"],
                package_weight_oz=spec.get("package_weight_oz"),
                package_length_in=spec.get("package_length_in"),
                package_width_in=spec.get("package_width_in"),
                package_height_in=spec.get("package_height_in"),
                acquisition_cost=spec["acquisition_cost"],
                current_quantity=spec["current_quantity"],
                acquired_at=spec["acquired_at"],
            )
            counts["products"] += 1
            product_by_sku[spec["sku"]] = product

        for spec in product_specs:
            product = product_by_sku[spec["sku"]]
            lot = lot_by_code[spec["lot_code"]]
            existing = db.scalar(
                select(ProductLotAssignment).where(
                    ProductLotAssignment.product_id == product.id,
                    ProductLotAssignment.lot_id == lot.id,
                )
            )
            if existing:
                continue
            repo.assign_product_to_lot(
                product_id=product.id,
                lot_id=lot.id,
                quantity_acquired=max(1, product.current_quantity),
                unit_cost=product.acquisition_cost,
                acquired_at=spec["acquired_at"],
            )
            counts["assignments"] += 1

        listing_specs = [
            {
                "sku": "GS-BUL-SIL-001",
                "marketplace": "ebay",
                "external_listing_id": "EBAY-LIST-10001",
                "listing_title": "1 oz Silver Buffalo Round .999 Fine",
                "listing_price": Decimal("38.99"),
                "quantity_listed": 8,
                "listing_status": "active",
                "marketplace_url": "https://www.ebay.com/itm/EBAY-LIST-10001",
                "marketplace_details": "{\"watchers\": 21, \"promoted\": true}",
                "listed_at": _utc(10),
            },
            {
                "sku": "GS-BUL-SIL-001",
                "marketplace": "ebay",
                "external_listing_id": "EBAY-LIST-E2E-DRAFT",
                "listing_title": "E2E Seed Listing Draft (eBay)",
                "listing_price": Decimal("39.00"),
                "quantity_listed": 3,
                "listing_status": "draft",
                "marketplace_url": "",
                "marketplace_details": "{\"e2e_fixture\": true, \"review_status\": \"pending\"}",
                "listed_at": _utc(1),
            },
            {
                "sku": "GS-COL-MIX-003",
                "marketplace": "whatnot",
                "external_listing_id": "WN-LIST-20001",
                "listing_title": "1881 Morgan Dollar VF Collector Coin",
                "listing_price": Decimal("62.00"),
                "quantity_listed": 2,
                "listing_status": "active",
                "marketplace_url": "https://www.whatnot.com/listing/WN-LIST-20001",
                "marketplace_details": "{\"stream_slot\": \"coin-show-12\"}",
                "listed_at": _utc(9),
            },
            {
                "sku": "GS-ANT-MIX-004",
                "marketplace": "facebook_marketplace",
                "external_listing_id": "FB-LIST-30001",
                "listing_title": "Antique Waltham Pocket Watch",
                "listing_price": Decimal("299.00"),
                "quantity_listed": 1,
                "listing_status": "draft",
                "marketplace_url": "https://www.facebook.com/marketplace/item/FB-LIST-30001",
                "marketplace_details": "{\"pickup\": false}",
                "listed_at": _utc(8),
            },
            {
                "sku": "GS-BUL-GOL-005",
                "marketplace": "shopify",
                "external_listing_id": "SHOP-LIST-40001",
                "listing_title": "1 oz Gold Bar PAMP Suisse",
                "listing_price": Decimal("2360.00"),
                "quantity_listed": 2,
                "listing_status": "active",
                "marketplace_url": "https://shop.goldenstackers.com/products/SHOP-LIST-40001",
                "marketplace_details": "{\"channel\": \"online_store\"}",
                "listed_at": _utc(7),
            },
        ]

        listing_key_to_id: dict[tuple[str, str], int] = {}
        for spec in listing_specs:
            existing = db.scalar(
                select(MarketplaceListing).where(
                    MarketplaceListing.marketplace == spec["marketplace"],
                    MarketplaceListing.external_listing_id == spec["external_listing_id"],
                )
            )
            if existing:
                listing_key_to_id[(spec["marketplace"], spec["external_listing_id"])] = existing.id
                continue
            listing = repo.create_listing(
                product_id=product_by_sku[spec["sku"]].id,
                marketplace=spec["marketplace"],
                listing_title=spec["listing_title"],
                listing_price=spec["listing_price"],
                quantity_listed=spec["quantity_listed"],
                external_listing_id=spec["external_listing_id"],
                marketplace_url=spec.get("marketplace_url", ""),
                marketplace_details=spec.get("marketplace_details", ""),
                listing_status=spec["listing_status"],
                listed_at=spec["listed_at"],
            )
            counts["listings"] += 1
            listing_key_to_id[(spec["marketplace"], spec["external_listing_id"])] = listing.id

        sale_specs = [
            {
                "marketplace": "ebay",
                "external_order_id": "EBAY-ORDER-50001",
                "sku": "GS-BUL-SIL-001",
                "external_listing_id": "EBAY-LIST-10001",
                "sold_price": Decimal("77.98"),
                "fees": Decimal("11.70"),
                "shipping_cost": Decimal("5.25"),
                "shipping_provider": "ebay_shipping",
                "shipping_service": "USPS Ground Advantage",
                "tracking_number": "9400111899223857123456",
                "tracking_status": "in_transit",
                "quantity_sold": 2,
                "shipped_at": _utc(5),
                "sold_at": _utc(6),
            },
            {
                "marketplace": "whatnot",
                "external_order_id": "WN-ORDER-60001",
                "sku": "GS-COL-MIX-003",
                "external_listing_id": "WN-LIST-20001",
                "sold_price": Decimal("62.00"),
                "fees": Decimal("5.89"),
                "shipping_cost": Decimal("0.00"),
                "shipping_provider": "pirateship",
                "shipping_service": "USPS Priority",
                "tracking_number": "9405511899561234567890",
                "tracking_status": "delivered",
                "quantity_sold": 1,
                "shipped_at": _utc(3),
                "delivered_at": _utc(1),
                "sold_at": _utc(4),
            },
            {
                "marketplace": "shopify",
                "external_order_id": "SHOP-ORDER-70001",
                "sku": "GS-BUL-GOL-005",
                "external_listing_id": "SHOP-LIST-40001",
                "sold_price": Decimal("2360.00"),
                "fees": Decimal("68.44"),
                "shipping_cost": Decimal("19.95"),
                "shipping_provider": "pirateship",
                "shipping_service": "UPS Ground",
                "tracking_number": "1Z999AA10123456784",
                "tracking_status": "label_created",
                "quantity_sold": 1,
                "sold_at": _utc(2),
            },
        ]

        for spec in sale_specs:
            existing = db.scalar(
                select(Sale).where(
                    Sale.marketplace == spec["marketplace"],
                    Sale.external_order_id == spec["external_order_id"],
                )
            )
            if existing:
                continue
            listing_id = listing_key_to_id[(spec["marketplace"], spec["external_listing_id"])]
            repo.create_sale(
                marketplace=spec["marketplace"],
                sold_price=spec["sold_price"],
                fees=spec["fees"],
                shipping_cost=spec["shipping_cost"],
                shipping_provider=spec.get("shipping_provider", ""),
                shipping_service=spec.get("shipping_service", ""),
                tracking_number=spec.get("tracking_number", ""),
                tracking_status=spec.get("tracking_status", ""),
                quantity_sold=spec["quantity_sold"],
                product_id=product_by_sku[spec["sku"]].id,
                listing_id=listing_id,
                external_order_id=spec["external_order_id"],
                shipped_at=spec.get("shipped_at"),
                delivered_at=spec.get("delivered_at"),
                sold_at=spec["sold_at"],
            )
            counts["sales"] += 1

        media_specs = [
            {
                "sku": "GS-BUL-SIL-001",
                "marketplace": "ebay",
                "external_listing_id": "EBAY-LIST-10001",
                "media_type": "image",
                "original_filename": "silver_round_front.jpg",
                "content_type": "image/jpeg",
                "size_bytes": 421334,
                "s3_bucket": "goldenstackers-media-dev",
                "s3_key": "seed/GS-BUL-SIL-001/front.jpg",
                "s3_url": "https://media.dev.goldenstackers.com/seed/GS-BUL-SIL-001/front.jpg",
            },
            {
                "sku": "GS-BUL-GOL-005",
                "marketplace": "shopify",
                "external_listing_id": "SHOP-LIST-40001",
                "media_type": "image",
                "original_filename": "gold_bar_assay.jpg",
                "content_type": "image/jpeg",
                "size_bytes": 538230,
                "s3_bucket": "goldenstackers-media-dev",
                "s3_key": "seed/GS-BUL-GOL-005/assay.jpg",
                "s3_url": "https://media.dev.goldenstackers.com/seed/GS-BUL-GOL-005/assay.jpg",
            },
            {
                "sku": "GS-ANT-MIX-004",
                "marketplace": "facebook_marketplace",
                "external_listing_id": "FB-LIST-30001",
                "media_type": "video",
                "original_filename": "pocket_watch_spin.mp4",
                "content_type": "video/mp4",
                "size_bytes": 2412388,
                "s3_bucket": "goldenstackers-media-dev",
                "s3_key": "seed/GS-ANT-MIX-004/watch_spin.mp4",
                "s3_url": "https://media.dev.goldenstackers.com/seed/GS-ANT-MIX-004/watch_spin.mp4",
            },
        ]

        for spec in media_specs:
            existing = db.scalar(select(MediaAsset).where(MediaAsset.s3_key == spec["s3_key"]))
            if existing:
                continue
            listing_id = listing_key_to_id.get((spec["marketplace"], spec["external_listing_id"]))
            repo.create_media_asset(
                media_type=spec["media_type"],
                original_filename=spec["original_filename"],
                content_type=spec["content_type"],
                size_bytes=spec["size_bytes"],
                s3_bucket=spec["s3_bucket"],
                s3_key=spec["s3_key"],
                s3_url=spec["s3_url"],
                product_id=product_by_sku[spec["sku"]].id,
                listing_id=listing_id,
                uploaded_by="seed-script",
            )
            counts["media"] += 1

        coin_reference_specs = [
            {
                "coin_name": "Morgan Dollar",
                "country": "United States",
                "issuer": "U.S. Mint",
                "denomination": "$1",
                "series": "Morgan Dollar",
                "year_start": 1878,
                "year_end": 1921,
                "composition": "90% Silver, 10% Copper",
                "metal_type": "silver",
                "weight_grams": Decimal("26.7300"),
                "asw_oz": Decimal("0.7734"),
                "diameter_mm": Decimal("38.10"),
                "thickness_mm": Decimal("2.40"),
                "pcgs_no": "7132",
                "tags": "morgan,silver dollar,us type",
            },
            {
                "coin_name": "Peace Dollar",
                "country": "United States",
                "issuer": "U.S. Mint",
                "denomination": "$1",
                "series": "Peace Dollar",
                "year_start": 1921,
                "year_end": 1935,
                "composition": "90% Silver, 10% Copper",
                "metal_type": "silver",
                "weight_grams": Decimal("26.7300"),
                "asw_oz": Decimal("0.7734"),
                "diameter_mm": Decimal("38.10"),
                "thickness_mm": Decimal("2.40"),
                "pcgs_no": "7356",
                "tags": "peace,silver dollar,us type",
            },
            {
                "coin_name": "Standing Liberty Quarter",
                "country": "United States",
                "issuer": "U.S. Mint",
                "denomination": "25c",
                "series": "Standing Liberty Quarter",
                "year_start": 1916,
                "year_end": 1930,
                "composition": "90% Silver, 10% Copper",
                "metal_type": "silver",
                "weight_grams": Decimal("6.2500"),
                "asw_oz": Decimal("0.1808"),
                "diameter_mm": Decimal("24.30"),
                "thickness_mm": Decimal("1.75"),
                "pcgs_no": "5706",
                "tags": "standing liberty,quarter,us type",
            },
        ]
        for spec in coin_reference_specs:
            existing = db.scalar(
                select(CoinReferenceCatalog).where(
                    CoinReferenceCatalog.coin_name == spec["coin_name"],
                    CoinReferenceCatalog.series == spec["series"],
                    CoinReferenceCatalog.year_start == spec["year_start"],
                    CoinReferenceCatalog.year_end == spec["year_end"],
                )
            )
            if existing:
                continue
            repo.create_coin_reference(**spec, actor="seed-script")
            counts["coin_refs"] += 1

        return counts
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed development data into the GoldenStackers DB.")
    parser.add_argument(
        "--wipe",
        action="store_true",
        help="Delete existing inventory/listing/sales/media/lot records before seeding.",
    )
    args = parser.parse_args()

    counts = seed_dev_data(wipe=args.wipe)
    print(
        "Seed complete. "
        f"Inserted: lots={counts['lots']}, products={counts['products']}, assignments={counts['assignments']}, "
        f"listings={counts['listings']}, sales={counts['sales']}, media={counts['media']}, "
        f"coin_refs={counts['coin_refs']}, app_users={counts['app_users']}"
    )


if __name__ == "__main__":
    main()
