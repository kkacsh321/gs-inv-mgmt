import base64
import hashlib
import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from time import perf_counter
from typing import Any

from sqlalchemy import String, and_, case, cast, delete, func, inspect, literal, or_, select, text
from sqlalchemy.orm import aliased

try:
    from app.db.models import (
        AIProviderConfig,
        AppUser,
        AuditLog,
        CoinAIRun,
        CoinReferenceCatalog,
        DocumentArtifact,
        DocumentTemplateProfile,
        EbayCategoryAspect,
        EbayPublishPreset,
        EbayCategorySuggestion,
        EbayListingTemplateProfile,
        InventoryMovement,
        InventorySource,
        IntegrationAutomationApproval,
        IntegrationAutomationRule,
        IntegrationQueueJob,
        MarketplaceListing,
        MediaAsset,
        NotificationOutbox,
        Order,
        OrderFinanceEntry,
        OrderItem,
        Product,
        ProductLotAssignment,
        PurchaseDocument,
        PurchaseLot,
        RuntimeSetting,
        ReturnRecord,
        RolePermission,
        SavedFilterProfile,
        Sale,
        SyncError,
        SyncEvent,
        SyncRun,
        ShippingPreset,
        WorkflowDraft,
        WorkflowEvent,
    )
    from app.services.validation import ValidationService
    from app.services.security import hash_password, verify_password
    from app.utils.time import utcnow_naive
    from app.config import settings
except ModuleNotFoundError:
    # Fallback for script-execution contexts where package root resolution differs.
    from db.models import (
        AIProviderConfig,
        AppUser,
        AuditLog,
        CoinAIRun,
        CoinReferenceCatalog,
        DocumentArtifact,
        DocumentTemplateProfile,
        EbayCategoryAspect,
        EbayPublishPreset,
        EbayCategorySuggestion,
        EbayListingTemplateProfile,
        InventoryMovement,
        InventorySource,
        IntegrationAutomationApproval,
        IntegrationAutomationRule,
        IntegrationQueueJob,
        MarketplaceListing,
        MediaAsset,
        NotificationOutbox,
        Order,
        OrderFinanceEntry,
        OrderItem,
        Product,
        ProductLotAssignment,
        PurchaseDocument,
        PurchaseLot,
        RuntimeSetting,
        ReturnRecord,
        RolePermission,
        SavedFilterProfile,
        Sale,
        SyncError,
        SyncEvent,
        SyncRun,
        ShippingPreset,
        WorkflowDraft,
        WorkflowEvent,
    )
    from services.validation import ValidationService
    from services.security import hash_password, verify_password
    from utils.time import utcnow_naive
    from config import settings


class InventoryRepository:
    PRODUCT_METAL_TYPE_MAX_LENGTH = 64
    PRODUCT_METAL_TYPE_MARKERS = (
        ("copper-nickel", ("copper-nickel", "copper nickel", "cupronickel")),
        ("silver", ("silver",)),
        ("gold", ("gold",)),
        ("copper", ("copper",)),
        ("nickel", ("nickel",)),
        ("platinum", ("platinum",)),
        ("palladium", ("palladium",)),
        ("bronze", ("bronze",)),
        ("brass", ("brass",)),
        ("zinc", ("zinc",)),
        ("steel", ("steel",)),
        ("clad", ("clad",)),
    )

    def __init__(self, db_session):
        self.db = db_session

    @staticmethod
    def _landed_unit_cost_decimal(
        *,
        unit_cost: Decimal | None,
        unit_tax_paid: Decimal | None = None,
        unit_shipping_paid: Decimal | None = None,
        unit_handling_paid: Decimal | None = None,
    ) -> Decimal | None:
        values = [unit_cost, unit_tax_paid, unit_shipping_paid, unit_handling_paid]
        if not any(v is not None for v in values):
            return None
        total = Decimal("0")
        for value in values:
            if value is not None:
                total += Decimal(value)
        return total

    @staticmethod
    def _safe_float(value: Any) -> float:
        try:
            if value is None:
                return 0.0
            return float(value)
        except Exception:
            return 0.0

    @classmethod
    def _landed_unit_cost_from_assignment_row(
        cls,
        row: Any,
        *,
        lot_fallback_unit_costs: dict[int, float],
        assignment_fallback_unit_costs: dict[int, float] | None = None,
    ) -> float:
        explicit_unit = cls._explicit_landed_unit_cost_from_assignment_row(row)
        if explicit_unit > 0:
            return explicit_unit
        assignment_id = int(getattr(row, "assignment_id", 0) or getattr(row, "id", 0) or 0)
        if assignment_id > 0 and assignment_fallback_unit_costs:
            assignment_fallback = cls._safe_float(assignment_fallback_unit_costs.get(assignment_id, 0.0))
            if assignment_fallback > 0:
                return assignment_fallback
        lot_id = int(getattr(row, "lot_id", 0) or 0)
        return cls._safe_float(lot_fallback_unit_costs.get(lot_id, 0.0))

    @classmethod
    def _explicit_landed_unit_cost_from_assignment_row(cls, row: Any) -> float:
        qty = float(max(0, int(getattr(row, "quantity_acquired", 0) or 0)))
        unit_cost = (
            cls._safe_float(getattr(row, "unit_cost", None))
            + cls._safe_float(getattr(row, "unit_tax_paid", None))
            + cls._safe_float(getattr(row, "unit_shipping_paid", None))
            + cls._safe_float(getattr(row, "unit_handling_paid", None))
        )
        if unit_cost > 0:
            return unit_cost

        allocated_landed = (
            cls._safe_float(getattr(row, "allocated_cost", None))
            + cls._safe_float(getattr(row, "allocated_tax_paid", None))
            + cls._safe_float(getattr(row, "allocated_shipping_paid", None))
            + cls._safe_float(getattr(row, "allocated_handling_paid", None))
        )
        if allocated_landed > 0 and qty > 0:
            return allocated_landed / qty
        return 0.0

    @classmethod
    def _lot_landed_total_from_assignment_row(cls, row: Any) -> float:
        return (
            cls._safe_float(getattr(row, "lot_total_cost", None))
            + cls._safe_float(getattr(row, "lot_total_tax_paid", None))
            + cls._safe_float(getattr(row, "lot_total_shipping_paid", None))
            + cls._safe_float(getattr(row, "lot_total_handling_paid", None))
        )

    @classmethod
    def _lot_fallback_unit_costs_by_lot_from_rows(cls, rows: list[Any]) -> dict[int, float]:
        lot_fallbacks, _assignment_fallbacks = cls._lot_fallback_unit_cost_maps_from_rows(rows)
        return lot_fallbacks

    @classmethod
    def _lot_fallback_unit_cost_maps_from_rows(cls, rows: list[Any]) -> tuple[dict[int, float], dict[int, float]]:
        explicit_cost_by_lot: dict[int, float] = {}
        explicit_qty_by_lot: dict[int, float] = {}
        blank_qty_by_lot: dict[int, float] = {}
        lot_total_by_lot: dict[int, float] = {}
        expected_qty_by_lot: dict[int, float] = {}
        blank_rows_by_lot: dict[int, list[dict[str, float | int]]] = {}
        for row in rows:
            lot_id = int(getattr(row, "lot_id", 0) or 0)
            if lot_id <= 0:
                continue
            qty = float(max(0, int(getattr(row, "quantity_acquired", 0) or 0)))
            if qty <= 0:
                continue
            lot_total_by_lot[lot_id] = max(
                lot_total_by_lot.get(lot_id, 0.0),
                cls._lot_landed_total_from_assignment_row(row),
            )
            expected_qty = float(max(0, int(getattr(row, "lot_expected_total_quantity", 0) or 0)))
            if expected_qty > 0:
                expected_qty_by_lot[lot_id] = max(expected_qty_by_lot.get(lot_id, 0.0), expected_qty)
            explicit_unit = cls._explicit_landed_unit_cost_from_assignment_row(row)
            if explicit_unit > 0:
                explicit_cost_by_lot[lot_id] = explicit_cost_by_lot.get(lot_id, 0.0) + (explicit_unit * qty)
                explicit_qty_by_lot[lot_id] = explicit_qty_by_lot.get(lot_id, 0.0) + qty
            else:
                blank_qty_by_lot[lot_id] = blank_qty_by_lot.get(lot_id, 0.0) + qty
                blank_rows_by_lot.setdefault(lot_id, []).append(
                    {
                        "assignment_id": int(getattr(row, "assignment_id", 0) or getattr(row, "id", 0) or 0),
                        "qty": qty,
                        "allocation_weight": cls._safe_float(getattr(row, "allocation_weight", None)),
                    }
                )

        fallback: dict[int, float] = {}
        assignment_fallback: dict[int, float] = {}
        for lot_id, blank_qty in blank_qty_by_lot.items():
            if blank_qty <= 0:
                continue
            remaining_landed = max(0.0, lot_total_by_lot.get(lot_id, 0.0) - explicit_cost_by_lot.get(lot_id, 0.0))
            if remaining_landed > 0:
                weighted_rows = [
                    row
                    for row in blank_rows_by_lot.get(lot_id, [])
                    if cls._safe_float(row.get("allocation_weight")) > 0
                    and int(row.get("assignment_id") or 0) > 0
                    and cls._safe_float(row.get("qty")) > 0
                ]
                total_weight = sum(cls._safe_float(row.get("allocation_weight")) for row in weighted_rows)
                if total_weight > 0:
                    for row in weighted_rows:
                        assignment_id = int(row.get("assignment_id") or 0)
                        qty = cls._safe_float(row.get("qty"))
                        weight = cls._safe_float(row.get("allocation_weight"))
                        assignment_fallback[assignment_id] = (remaining_landed * (weight / total_weight)) / qty
                    continue
                expected_remaining_qty = max(
                    0.0,
                    expected_qty_by_lot.get(lot_id, 0.0) - explicit_qty_by_lot.get(lot_id, 0.0),
                )
                fallback[lot_id] = remaining_landed / max(blank_qty, expected_remaining_qty)
        return fallback, assignment_fallback

    @classmethod
    def _product_default_landed_unit_cost(cls, row: Any) -> float:
        landed = (
            cls._safe_float(getattr(row, "acquisition_cost", None))
            + cls._safe_float(getattr(row, "acquisition_tax_paid", None))
            + cls._safe_float(getattr(row, "acquisition_shipping_paid", None))
            + cls._safe_float(getattr(row, "acquisition_handling_paid", None))
        )
        if landed > 0:
            return landed
        return cls._safe_float(getattr(row, "product_cost", None))

    def _serialize_audit_value(self, value: Any) -> Any:
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return value.isoformat()
        return value

    def _listing_is_archived(self, listing: MarketplaceListing) -> bool:
        raw = str(getattr(listing, "marketplace_details", "") or "").strip()
        if not raw:
            return False
        try:
            parsed = json.loads(raw)
        except Exception:
            return False
        if not isinstance(parsed, dict):
            return False
        lifecycle = parsed.get("lifecycle")
        if not isinstance(lifecycle, dict):
            return False
        return bool(lifecycle.get("archived"))

    @staticmethod
    def _listing_bundle_payload_from_raw(raw_value: Any) -> dict[str, Any]:
        raw = str(raw_value or "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        if not isinstance(parsed, dict):
            return {}
        bundle = parsed.get("bundle")
        if isinstance(bundle, dict):
            return bundle
        ebay_publish = parsed.get("ebay_publish")
        if isinstance(ebay_publish, dict) and isinstance(ebay_publish.get("bundle"), dict):
            return ebay_publish["bundle"]
        return {}

    @classmethod
    def _listing_bundle_payload(cls, listing: MarketplaceListing | None) -> dict[str, Any]:
        return cls._listing_bundle_payload_from_raw(getattr(listing, "marketplace_details", ""))

    @staticmethod
    def _bundle_components_from_payload(
        bundle: dict[str, Any],
        quantity_sold: int,
    ) -> list[dict[str, Any]]:
        if not bool(bundle.get("enabled")):
            return []
        sale_qty = max(1, int(quantity_sold or 1))
        rolled_up: dict[int, dict[str, Any]] = {}
        for component in list(bundle.get("components") or []):
            if not isinstance(component, dict):
                continue
            try:
                product_id = int(component.get("product_id") or 0)
                qty_per_listing = max(1, int(component.get("quantity_per_listing") or 1))
            except Exception:
                continue
            if product_id <= 0:
                continue
            existing = rolled_up.setdefault(
                product_id,
                {
                    "product_id": product_id,
                    "quantity_per_listing": 0,
                    "quantity_total": 0,
                    "sku": str(component.get("sku") or "").strip(),
                },
            )
            existing["quantity_per_listing"] = int(existing["quantity_per_listing"]) + qty_per_listing
            existing["quantity_total"] = int(existing["quantity_total"]) + (qty_per_listing * sale_qty)
        return list(rolled_up.values())

    def _listing_bundle_sale_components(
        self,
        listing_id: int | None,
        quantity_sold: int,
    ) -> list[dict[str, Any]]:
        if listing_id is None:
            return []
        listing = self.db.get(MarketplaceListing, int(listing_id))
        bundle = self._listing_bundle_payload(listing)
        return self._bundle_components_from_payload(bundle, quantity_sold)

    def _return_bundle_restock_components(
        self,
        sale_id: int | None,
        quantity_returned: int,
    ) -> list[dict[str, Any]]:
        if sale_id is None:
            return []
        sale = self.db.get(Sale, int(sale_id))
        if sale is None:
            return []
        return self._listing_bundle_sale_components(sale.listing_id, quantity_returned)

    def _lot_is_archived(self, lot: PurchaseLot) -> bool:
        raw = str(getattr(lot, "notes", "") or "").strip()
        if not raw:
            return False
        try:
            parsed = json.loads(raw)
        except Exception:
            return False
        if not isinstance(parsed, dict):
            return False
        lifecycle = parsed.get("lifecycle")
        if not isinstance(lifecycle, dict):
            return False
        return bool(lifecycle.get("archived"))

    def _allocate_lot_tax_paid(
        self,
        *,
        lot_total_tax_paid: Decimal | None,
        lot_total_cost: Decimal | None,
        allocated_cost: Decimal | None,
    ) -> Decimal | None:
        if lot_total_tax_paid is None or lot_total_cost is None or allocated_cost is None:
            return None
        if lot_total_cost <= 0:
            return None
        if allocated_cost < 0:
            return None
        try:
            return (lot_total_tax_paid * allocated_cost) / lot_total_cost
        except Exception:
            return None

    def _allocate_lot_component_paid(
        self,
        *,
        lot_component_total: Decimal | None,
        lot_total_cost: Decimal | None,
        allocated_cost: Decimal | None,
    ) -> Decimal | None:
        if lot_component_total is None or lot_total_cost is None or allocated_cost is None:
            return None
        if lot_total_cost <= 0:
            return None
        if allocated_cost < 0:
            return None
        try:
            return (lot_component_total * allocated_cost) / lot_total_cost
        except Exception:
            return None

    def _record_audit(
        self,
        entity_type: str,
        entity_id: int | None,
        action: str,
        actor: str,
        changes: dict[str, Any],
    ) -> None:
        self.db.add(
            AuditLog(
                entity_type=entity_type,
                entity_id=entity_id,
                action=action,
                actor=(actor or "system").strip() or "system",
                changes_json=json.dumps(changes, default=self._serialize_audit_value),
                created_at=utcnow_naive(),
            )
        )

    def _record_inventory_movement(
        self,
        *,
        product_id: int | None,
        movement_type: str,
        quantity_before: int,
        quantity_after: int,
        unit_cost: Decimal | None = None,
        reference_type: str = "",
        reference_id: int | None = None,
        notes: str = "",
        occurred_at: datetime | None = None,
    ) -> None:
        self.db.add(
            InventoryMovement(
                product_id=product_id,
                movement_type=movement_type,
                quantity_delta=(quantity_after - quantity_before),
                quantity_before=quantity_before,
                quantity_after=quantity_after,
                unit_cost=unit_cost,
                reference_type=reference_type,
                reference_id=reference_id,
                notes=notes,
                occurred_at=occurred_at or utcnow_naive(),
                created_at=utcnow_naive(),
            )
        )

    @staticmethod
    def _validate_inventory_class(value: str) -> str:
        resolved = (value or "sellable").strip().lower()
        allowed = {"sellable", "raw_material", "supply"}
        if resolved not in allowed:
            raise ValueError(f"Inventory class must be one of: {', '.join(sorted(allowed))}.")
        return resolved

    @staticmethod
    def _metal_marker_matches(value: str, marker: str) -> bool:
        if re.fullmatch(r"[a-z]+", marker):
            return re.search(rf"\b{re.escape(marker)}\b", value) is not None
        return marker in value

    @classmethod
    def _normalize_product_metal_type(cls, value: str | None) -> tuple[str, str]:
        raw = str(value or "").strip()
        if len(raw) <= cls.PRODUCT_METAL_TYPE_MAX_LENGTH:
            return raw, ""

        lowered = raw.lower()
        compact_parts: list[str] = []
        for label, needles in cls.PRODUCT_METAL_TYPE_MARKERS:
            if any(cls._metal_marker_matches(lowered, needle) for needle in needles) and label not in compact_parts:
                if label in {"copper", "nickel"} and "copper-nickel" in compact_parts:
                    continue
                compact_parts.append(label)

        compact = ", ".join(compact_parts)
        if not compact:
            compact = raw[: cls.PRODUCT_METAL_TYPE_MAX_LENGTH - 3].rstrip() + "..."
        return compact[: cls.PRODUCT_METAL_TYPE_MAX_LENGTH].strip(), raw

    @staticmethod
    def _append_product_detail_once(description: str | None, *, label: str, value: str) -> str:
        resolved_description = str(description or "").strip()
        detail = f"{label}: {value.strip()}"
        if not value.strip() or detail in resolved_description:
            return resolved_description
        if not resolved_description:
            return detail
        return f"{resolved_description}\n\n{detail}"

    def create_product(
        self,
        sku: str,
        title: str,
        category: str,
        description: str,
        metal_type: str,
        weight_oz: Decimal | None,
        acquisition_cost: Decimal | None,
        current_quantity: int,
        inventory_class: str = "sellable",
        acquisition_tax_paid: Decimal | None = None,
        acquisition_shipping_paid: Decimal | None = None,
        acquisition_handling_paid: Decimal | None = None,
        product_cost: Decimal | None = None,
        ebay_purchase: bool = False,
        ebay_purchase_item_id: str = "",
        ebay_purchase_url: str = "",
        coin_reference_id: int | None = None,
        package_weight_oz: Decimal | None = None,
        package_length_in: Decimal | None = None,
        package_width_in: Decimal | None = None,
        package_height_in: Decimal | None = None,
        acquired_at: datetime | None = None,
        lot_id: int | None = None,
        actor: str = "system",
    ) -> Product:
        ValidationService.require_non_empty("SKU", sku)
        ValidationService.require_non_empty("Product title", title)
        ValidationService.require_positive_int("Current quantity", current_quantity, min_value=0)
        ValidationService.require_non_negative_decimal("Acquisition cost", acquisition_cost)
        ValidationService.require_non_negative_decimal("Acquisition tax paid", acquisition_tax_paid)
        ValidationService.require_non_negative_decimal("Acquisition shipping paid", acquisition_shipping_paid)
        ValidationService.require_non_negative_decimal("Acquisition handling paid", acquisition_handling_paid)
        ValidationService.require_non_negative_decimal("Product cost", product_cost)
        ValidationService.require_non_negative_decimal("Weight (oz)", weight_oz)
        ValidationService.require_non_negative_decimal("Package weight (oz)", package_weight_oz)
        ValidationService.require_non_negative_decimal("Package length (in)", package_length_in)
        ValidationService.require_non_negative_decimal("Package width (in)", package_width_in)
        ValidationService.require_non_negative_decimal("Package height (in)", package_height_in)
        resolved_inventory_class = self._validate_inventory_class(inventory_class)
        if coin_reference_id is not None:
            if self.db.get(CoinReferenceCatalog, int(coin_reference_id)) is None:
                raise ValueError("Selected coin reference does not exist.")

        ebay_purchase_item_id_value = (ebay_purchase_item_id or "").strip()
        ebay_purchase_url_value = (ebay_purchase_url or "").strip()
        if ebay_purchase and not ebay_purchase_item_id_value:
            raise ValueError("eBay purchase item ID is required when eBay purchase is enabled.")
        if not ebay_purchase:
            ebay_purchase_item_id_value = ""
            ebay_purchase_url_value = ""
        resolved_metal_type, original_metal_detail = self._normalize_product_metal_type(metal_type)
        resolved_description = str(description or "").strip()
        if original_metal_detail:
            resolved_description = self._append_product_detail_once(
                resolved_description,
                label="Metal composition",
                value=original_metal_detail,
            )

        product = Product(
            sku=sku,
            title=title,
            category=category,
            inventory_class=resolved_inventory_class,
            description=resolved_description,
            metal_type=resolved_metal_type,
            weight_oz=weight_oz,
            package_weight_oz=package_weight_oz,
            package_length_in=package_length_in,
            package_width_in=package_width_in,
            package_height_in=package_height_in,
            acquisition_cost=acquisition_cost,
            acquisition_tax_paid=acquisition_tax_paid,
            acquisition_shipping_paid=acquisition_shipping_paid,
            acquisition_handling_paid=acquisition_handling_paid,
            product_cost=product_cost,
            ebay_purchase=bool(ebay_purchase),
            ebay_purchase_item_id=ebay_purchase_item_id_value,
            ebay_purchase_url=ebay_purchase_url_value,
            current_quantity=current_quantity,
            acquired_at=acquired_at or utcnow_naive(),
            coin_reference_id=coin_reference_id,
        )
        self.db.add(product)
        self.db.flush()

        if lot_id is not None:
            allocated_cost = ((acquisition_cost * current_quantity) if acquisition_cost is not None else None)
            allocated_tax_paid = (
                (acquisition_tax_paid * current_quantity) if acquisition_tax_paid is not None else None
            )
            allocated_shipping_paid = (
                (acquisition_shipping_paid * current_quantity) if acquisition_shipping_paid is not None else None
            )
            allocated_handling_paid = (
                (acquisition_handling_paid * current_quantity) if acquisition_handling_paid is not None else None
            )
            if allocated_tax_paid is None and allocated_cost is not None:
                lot_row = self.db.get(PurchaseLot, lot_id)
                allocated_tax_paid = self._allocate_lot_tax_paid(
                    lot_total_tax_paid=(lot_row.total_tax_paid if lot_row is not None else None),
                    lot_total_cost=(lot_row.total_cost if lot_row is not None else None),
                    allocated_cost=allocated_cost,
                )
                if allocated_shipping_paid is None:
                    allocated_shipping_paid = self._allocate_lot_component_paid(
                        lot_component_total=(lot_row.total_shipping_paid if lot_row is not None else None),
                        lot_total_cost=(lot_row.total_cost if lot_row is not None else None),
                        allocated_cost=allocated_cost,
                    )
                if allocated_handling_paid is None:
                    allocated_handling_paid = self._allocate_lot_component_paid(
                        lot_component_total=(lot_row.total_handling_paid if lot_row is not None else None),
                        lot_total_cost=(lot_row.total_cost if lot_row is not None else None),
                        allocated_cost=allocated_cost,
                    )
            assignment = ProductLotAssignment(
                product_id=product.id,
                lot_id=lot_id,
                quantity_acquired=max(1, current_quantity),
                unit_cost=acquisition_cost,
                unit_tax_paid=acquisition_tax_paid,
                unit_shipping_paid=acquisition_shipping_paid,
                unit_handling_paid=acquisition_handling_paid,
                allocated_cost=allocated_cost,
                allocated_tax_paid=allocated_tax_paid,
                allocated_shipping_paid=allocated_shipping_paid,
                allocated_handling_paid=allocated_handling_paid,
                acquired_at=acquired_at or utcnow_naive(),
            )
            self.db.add(assignment)

        if current_quantity > 0:
            landed_unit_cost = self._landed_unit_cost_decimal(
                unit_cost=acquisition_cost,
                unit_tax_paid=acquisition_tax_paid,
                unit_shipping_paid=acquisition_shipping_paid,
                unit_handling_paid=acquisition_handling_paid,
            )
            self._record_inventory_movement(
                product_id=product.id,
                movement_type="initial_stock",
                quantity_before=0,
                quantity_after=current_quantity,
                unit_cost=(landed_unit_cost if landed_unit_cost is not None else acquisition_cost),
                reference_type="product",
                reference_id=product.id,
                notes="Initial inventory created with product record.",
                occurred_at=acquired_at or utcnow_naive(),
            )

        self._record_audit(
            entity_type="product",
            entity_id=product.id,
            action="create",
            actor=actor,
            changes={
                "after": {
                    "sku": sku,
                    "title": title,
                    "category": category,
                    "inventory_class": resolved_inventory_class,
                    "coin_reference_id": coin_reference_id,
                    "product_cost": product_cost,
                    "acquisition_tax_paid": acquisition_tax_paid,
                    "acquisition_shipping_paid": acquisition_shipping_paid,
                    "acquisition_handling_paid": acquisition_handling_paid,
                    "ebay_purchase": bool(ebay_purchase),
                    "ebay_purchase_item_id": ebay_purchase_item_id_value,
                    "ebay_purchase_url": ebay_purchase_url_value,
                }
            },
        )

        self.db.commit()
        self.db.refresh(product)
        return product

    def list_products(
        self,
        *,
        limit: int | None = None,
        search_query: str | None = None,
        product_ids: list[int] | set[int] | tuple[int, ...] | None = None,
    ) -> list[Product]:
        query = select(Product)
        ids = sorted({int(pid) for pid in (product_ids or []) if int(pid or 0) > 0})
        raw_search = str(search_query or "").strip()
        filters = []
        if ids:
            filters.append(Product.id.in_(ids))
        if raw_search:
            pattern = f"%{raw_search.lower()}%"
            search_filters = [
                func.lower(func.coalesce(Product.sku, "")).like(pattern),
                func.lower(func.coalesce(Product.title, "")).like(pattern),
                func.lower(func.coalesce(Product.category, "")).like(pattern),
                func.lower(func.coalesce(Product.metal_type, "")).like(pattern),
            ]
            if raw_search.isdigit():
                search_filters.append(Product.id == int(raw_search))
            filters.append(or_(*search_filters))
        if filters:
            query = query.where(or_(*filters) if ids and raw_search else filters[0])
        query = query.order_by(Product.created_at.desc(), Product.id.desc())
        if limit is not None:
            query = query.limit(max(1, min(1000, int(limit or 1))))
        return self.db.scalars(query).all()

    def create_listing(
        self,
        product_id: int,
        marketplace: str,
        listing_title: str,
        listing_price: Decimal,
        quantity_listed: int,
        external_listing_id: str = "",
        marketplace_url: str = "",
        marketplace_details: str = "",
        listing_status: str = "draft",
        listed_at: datetime | None = None,
        actor: str = "system",
    ) -> MarketplaceListing:
        ValidationService.require_non_empty("Marketplace", marketplace)
        ValidationService.require_non_empty("Listing title", listing_title)
        ValidationService.require_positive_int("Quantity listed", quantity_listed)
        ValidationService.require_non_negative_decimal("Listing price", listing_price)
        ValidationService.ensure_unique_marketplace_listing(
            self.db, marketplace, external_listing_id
        )

        listing = MarketplaceListing(
            product_id=product_id,
            marketplace=marketplace,
            listing_title=listing_title,
            listing_price=listing_price,
            quantity_listed=quantity_listed,
            external_listing_id=external_listing_id,
            marketplace_url=marketplace_url,
            marketplace_details=marketplace_details,
            listing_status="draft",
            review_status="pending",
            reviewed_at=None,
            reviewed_by="",
            listed_at=listed_at or utcnow_naive(),
        )
        self.db.add(listing)
        self.db.flush()
        self._record_audit(
            entity_type="listing",
            entity_id=listing.id,
            action="create",
            actor=actor,
            changes={"after": {"product_id": product_id, "marketplace": marketplace, "title": listing_title}},
        )
        self.db.commit()
        self.db.refresh(listing)
        return listing

    def list_listings(self, *, limit: int | None = None) -> list[MarketplaceListing]:
        query = select(MarketplaceListing).order_by(MarketplaceListing.created_at.desc(), MarketplaceListing.id.desc())
        if limit is not None:
            query = query.limit(max(1, min(5000, int(limit or 1))))
        return self.db.scalars(query).all()

    def get_listing(self, listing_id: int) -> MarketplaceListing | None:
        return self.db.get(MarketplaceListing, int(listing_id))

    def list_listings_for_product(
        self,
        product_id: int,
        *,
        marketplace: str | None = None,
        limit: int | None = None,
    ) -> list[MarketplaceListing]:
        query = select(MarketplaceListing).where(MarketplaceListing.product_id == int(product_id))
        marketplace_clean = str(marketplace or "").strip().lower()
        if marketplace_clean:
            query = query.where(func.lower(func.coalesce(MarketplaceListing.marketplace, "")) == marketplace_clean)
        query = query.order_by(MarketplaceListing.created_at.desc(), MarketplaceListing.id.desc())
        if limit is not None:
            query = query.limit(max(1, min(1000, int(limit or 1))))
        return self.db.scalars(query).all()

    def find_listing_owner_by_external_id(
        self,
        *,
        marketplace: str,
        external_listing_id: str,
        exclude_listing_id: int | None = None,
    ) -> int | None:
        ext_id = str(external_listing_id or "").strip()
        if not ext_id:
            return None
        query = select(MarketplaceListing.id).where(
            func.lower(func.coalesce(MarketplaceListing.marketplace, "")) == str(marketplace or "").strip().lower(),
            MarketplaceListing.external_listing_id == ext_id,
        )
        if exclude_listing_id is not None:
            query = query.where(MarketplaceListing.id != int(exclude_listing_id))
        listing_id = self.db.scalar(query.order_by(MarketplaceListing.id.asc()).limit(1))
        return int(listing_id) if listing_id is not None else None

    def create_sale(
        self,
        marketplace: str,
        sold_price: Decimal,
        fees: Decimal,
        shipping_cost: Decimal,
        quantity_sold: int,
        shipping_label_cost: Decimal | None = None,
        shipping_label_currency: str = "USD",
        shipping_label_id: str = "",
        shipping_label_url: str = "",
        shipping_label_purchased_at: datetime | None = None,
        shipping_provider: str = "",
        shipping_service: str = "",
        shipping_package_type: str = "",
        tracking_number: str = "",
        tracking_status: str = "",
        order_id: int | None = None,
        product_id: int | None = None,
        listing_id: int | None = None,
        external_order_id: str = "",
        shipped_at: datetime | None = None,
        delivered_at: datetime | None = None,
        sold_at: datetime | None = None,
        actor: str = "system",
    ) -> Sale:
        ValidationService.require_non_empty("Marketplace", marketplace)
        ValidationService.require_positive_int("Quantity sold", quantity_sold)
        ValidationService.require_non_negative_decimal("Sold price", sold_price)
        ValidationService.require_non_negative_decimal("Fees", fees)
        ValidationService.require_non_negative_decimal("Shipping cost", shipping_cost)
        ValidationService.require_non_negative_decimal("Shipping label cost", shipping_label_cost)
        ValidationService.validate_sale_tracking_requirements(tracking_status, tracking_number)
        ValidationService.validate_shipping_dates(tracking_status, shipped_at, delivered_at)
        ValidationService.ensure_tracking_number_not_reused(
            self.db, tracking_number, external_order_id
        )

        sale = Sale(
            order_id=order_id,
            product_id=product_id,
            listing_id=listing_id,
            marketplace=marketplace,
            sold_price=sold_price,
            fees=fees,
            shipping_cost=shipping_cost,
            shipping_label_cost=shipping_label_cost,
            shipping_label_currency=(shipping_label_currency or "USD").strip().upper() or "USD",
            shipping_label_id=(shipping_label_id or "").strip(),
            shipping_label_url=(shipping_label_url or "").strip(),
            shipping_label_purchased_at=shipping_label_purchased_at,
            shipping_provider=shipping_provider,
            shipping_service=shipping_service,
            shipping_package_type=shipping_package_type,
            tracking_number=tracking_number,
            tracking_status=tracking_status,
            shipped_at=shipped_at,
            delivered_at=delivered_at,
            quantity_sold=quantity_sold,
            external_order_id=external_order_id,
            sold_at=sold_at or utcnow_naive(),
        )

        movement_payloads: list[dict[str, Any]] = []
        bundle_components = self._listing_bundle_sale_components(listing_id, quantity_sold)
        if bundle_components:
            for component in bundle_components:
                component_product = self.db.get(Product, int(component["product_id"]))
                if component_product is None:
                    continue
                component_qty = max(1, int(component.get("quantity_total") or 1))
                quantity_before = int(component_product.current_quantity)
                quantity_after = max(0, quantity_before - component_qty)
                component_product.current_quantity = quantity_after
                movement_payloads.append(
                    {
                        "product_id": component_product.id,
                        "movement_type": "sale_bundle_component",
                        "quantity_before": quantity_before,
                        "quantity_after": quantity_after,
                        "unit_cost": component_product.acquisition_cost,
                        "reference_type": "sale",
                        "notes": (
                            "Inventory reduced from bundle sale component: "
                            f"{int(component.get('quantity_per_listing') or 1)} unit(s) per listing x "
                            f"{int(quantity_sold or 1)} listing unit(s)."
                        ),
                        "occurred_at": sold_at or utcnow_naive(),
                    }
                )
        elif product_id is not None:
            product = self.db.get(Product, product_id)
            if product:
                quantity_before = int(product.current_quantity)
                quantity_after = max(0, quantity_before - int(quantity_sold))
                product.current_quantity = quantity_after
                movement_payloads.append(
                    {
                        "product_id": product.id,
                        "movement_type": "sale",
                        "quantity_before": quantity_before,
                        "quantity_after": quantity_after,
                        "unit_cost": product.acquisition_cost,
                        "reference_type": "sale",
                        "notes": "Inventory reduced from recorded sale.",
                        "occurred_at": sold_at or utcnow_naive(),
                    }
                )

        self.db.add(sale)
        self.db.flush()
        for movement_payload in movement_payloads:
            self._record_inventory_movement(
                **movement_payload,
                reference_id=sale.id,
            )
        self._record_audit(
            entity_type="sale",
            entity_id=sale.id,
            action="create",
            actor=actor,
            changes={
                "after": {
                    "marketplace": marketplace,
                    "order_id": order_id,
                    "product_id": product_id,
                    "listing_id": listing_id,
                    "external_order_id": external_order_id,
                }
            },
        )
        self.db.commit()
        self.db.refresh(sale)
        return sale

    def list_sales(self) -> list[Sale]:
        return self.db.scalars(select(Sale).order_by(Sale.created_at.desc())).all()

    def list_sales_for_listing(self, listing_id: int) -> list[Sale]:
        listing_id_int = int(listing_id)
        return self.db.scalars(
            select(Sale)
            .where(Sale.listing_id == listing_id_int)
            .order_by(Sale.created_at.desc())
        ).all()

    def create_shipping_preset(
        self,
        name: str,
        shipping_provider: str,
        shipping_service: str,
        shipping_package_type: str = "",
        notes: str = "",
        is_default: bool = False,
        is_active: bool = True,
        actor: str = "system",
    ) -> ShippingPreset:
        if is_default:
            existing_defaults = self.db.scalars(
                select(ShippingPreset).where(ShippingPreset.is_default.is_(True))
            ).all()
            for row in existing_defaults:
                row.is_default = False

        preset = ShippingPreset(
            name=name.strip(),
            shipping_provider=shipping_provider.strip(),
            shipping_service=shipping_service.strip(),
            shipping_package_type=shipping_package_type.strip(),
            notes=notes.strip(),
            is_default=bool(is_default),
            is_active=bool(is_active),
        )
        self.db.add(preset)
        self.db.flush()
        self._record_audit(
            entity_type="shipping_preset",
            entity_id=preset.id,
            action="create",
            actor=actor,
            changes={
                "after": {
                    "name": preset.name,
                    "shipping_provider": preset.shipping_provider,
                    "shipping_service": preset.shipping_service,
                    "is_default": preset.is_default,
                    "is_active": preset.is_active,
                }
            },
        )
        self.db.commit()
        self.db.refresh(preset)
        return preset

    def list_shipping_presets(self, active_only: bool = False) -> list[ShippingPreset]:
        query = select(ShippingPreset)
        if active_only:
            query = query.where(ShippingPreset.is_active.is_(True))
        query = query.order_by(ShippingPreset.is_default.desc(), ShippingPreset.name.asc())
        return self.db.scalars(query).all()

    def update_shipping_preset(
        self, preset_id: int, updates: dict[str, Any], actor: str = "system"
    ) -> ShippingPreset:
        preset = self.db.get(ShippingPreset, preset_id)
        if preset is None:
            raise ValueError(f"Shipping preset {preset_id} not found.")

        changes: dict[str, dict[str, Any]] = {}
        for field, new_value in updates.items():
            if not hasattr(preset, field):
                continue
            old_value = getattr(preset, field)
            if old_value != new_value:
                changes[field] = {
                    "before": self._serialize_audit_value(old_value),
                    "after": self._serialize_audit_value(new_value),
                }
                setattr(preset, field, new_value)

        if changes:
            if changes.get("is_default", {}).get("after") is True:
                existing_defaults = self.db.scalars(
                    select(ShippingPreset).where(
                        ShippingPreset.id != preset.id, ShippingPreset.is_default.is_(True)
                    )
                ).all()
                for row in existing_defaults:
                    row.is_default = False
            self._record_audit("shipping_preset", preset.id, "update", actor, changes)
            self.db.commit()
            self.db.refresh(preset)
        return preset

    def mark_shipments_exported(
        self, sale_ids: list[int], actor: str = "system", exported_at: datetime | None = None
    ) -> int:
        if not sale_ids:
            return 0
        stamp = exported_at or utcnow_naive()
        updated = 0
        for sale_id in sale_ids:
            sale = self.db.get(Sale, int(sale_id))
            if sale is None:
                continue
            old_value = sale.shipment_exported_at
            if old_value == stamp:
                continue
            sale.shipment_exported_at = stamp
            self._record_audit(
                "sale",
                sale.id,
                "update",
                actor,
                {
                    "shipment_exported_at": {
                        "before": self._serialize_audit_value(old_value),
                        "after": self._serialize_audit_value(stamp),
                    }
                },
            )
            updated += 1

        if updated > 0:
            self.db.commit()
        return updated

    def create_document_template_profile(
        self,
        *,
        environment: str,
        doc_type: str,
        name: str,
        template_name: str,
        accent_color: str,
        company_name: str = "",
        company_email: str = "",
        company_phone: str = "",
        company_website: str = "",
        notes: str = "",
        is_default: bool = False,
        is_active: bool = True,
        actor: str = "system",
    ) -> DocumentTemplateProfile:
        resolved_env = (environment or "local").strip().lower()
        resolved_doc_type = (doc_type or "all").strip().lower()
        if is_default:
            defaults = self.db.scalars(
                select(DocumentTemplateProfile).where(
                    DocumentTemplateProfile.environment == resolved_env,
                    DocumentTemplateProfile.doc_type == resolved_doc_type,
                    DocumentTemplateProfile.is_default.is_(True),
                )
            ).all()
            for row in defaults:
                row.is_default = False

        profile = DocumentTemplateProfile(
            environment=resolved_env,
            doc_type=resolved_doc_type,
            name=name.strip(),
            template_name=template_name.strip(),
            accent_color=accent_color.strip() or "#b45309",
            company_name=company_name.strip(),
            company_email=company_email.strip(),
            company_phone=company_phone.strip(),
            company_website=company_website.strip(),
            notes=notes.strip(),
            is_default=bool(is_default),
            is_active=bool(is_active),
        )
        self.db.add(profile)
        self.db.flush()
        self._record_audit(
            entity_type="document_template_profile",
            entity_id=profile.id,
            action="create",
            actor=actor,
            changes={
                "after": {
                    "environment": profile.environment,
                    "doc_type": profile.doc_type,
                    "name": profile.name,
                    "template_name": profile.template_name,
                    "is_default": profile.is_default,
                    "is_active": profile.is_active,
                }
            },
        )
        self.db.commit()
        self.db.refresh(profile)
        return profile

    def create_ebay_publish_preset(
        self,
        *,
        environment: str,
        username: str,
        name: str,
        marketplace_id: str,
        currency: str,
        content_language: str,
        merchant_location_key: str,
        payment_policy_id: str,
        fulfillment_policy_id: str,
        return_policy_id: str,
        category_id: str = "",
        format_type: str = "FIXED_PRICE",
        listing_duration: str = "GTC",
        condition_value: str = "NEW",
        is_default: bool = False,
        is_active: bool = True,
        actor: str = "system",
    ) -> EbayPublishPreset:
        resolved_env = (environment or "local").strip().lower()
        resolved_user = (username or "").strip()
        resolved_name = (name or "").strip()
        if not resolved_user:
            raise ValueError("Username is required.")
        if not resolved_name:
            raise ValueError("Preset name is required.")

        if is_default:
            defaults = self.db.scalars(
                select(EbayPublishPreset).where(
                    EbayPublishPreset.environment == resolved_env,
                    EbayPublishPreset.username == resolved_user,
                    EbayPublishPreset.is_default.is_(True),
                )
            ).all()
            for row in defaults:
                row.is_default = False

        row = EbayPublishPreset(
            environment=resolved_env,
            username=resolved_user,
            name=resolved_name,
            marketplace_id=(marketplace_id or "").strip() or "EBAY_US",
            currency=(currency or "").strip() or "USD",
            content_language=(content_language or "").strip() or "en-US",
            merchant_location_key=(merchant_location_key or "").strip(),
            payment_policy_id=(payment_policy_id or "").strip(),
            fulfillment_policy_id=(fulfillment_policy_id or "").strip(),
            return_policy_id=(return_policy_id or "").strip(),
            category_id=(category_id or "").strip(),
            format_type=(format_type or "FIXED_PRICE").strip().upper(),
            listing_duration=(listing_duration or "GTC").strip().upper(),
            condition_value=(condition_value or "NEW").strip().upper(),
            is_default=bool(is_default),
            is_active=bool(is_active),
        )
        self.db.add(row)
        self.db.flush()
        self._record_audit(
            entity_type="ebay_publish_preset",
            entity_id=row.id,
            action="create",
            actor=actor,
            changes={
                "after": {
                    "environment": row.environment,
                    "username": row.username,
                    "name": row.name,
                    "is_default": row.is_default,
                    "is_active": row.is_active,
                }
            },
        )
        self.db.commit()
        self.db.refresh(row)
        return row

    def list_ebay_publish_presets(
        self,
        *,
        environment: str,
        username: str,
        active_only: bool = True,
    ) -> list[EbayPublishPreset]:
        query = select(EbayPublishPreset).where(
            EbayPublishPreset.environment == (environment or "local").strip().lower(),
            EbayPublishPreset.username == (username or "").strip(),
        )
        if active_only:
            query = query.where(EbayPublishPreset.is_active.is_(True))
        query = query.order_by(
            EbayPublishPreset.is_default.desc(),
            EbayPublishPreset.name.asc(),
        )
        return self.db.scalars(query).all()

    def update_ebay_publish_preset(
        self,
        preset_id: int,
        updates: dict[str, Any],
        actor: str = "system",
    ) -> EbayPublishPreset:
        row = self.db.get(EbayPublishPreset, preset_id)
        if row is None:
            raise ValueError(f"eBay publish preset {preset_id} not found.")

        changes: dict[str, dict[str, Any]] = {}
        for field, new_value in updates.items():
            if not hasattr(row, field):
                continue
            old_value = getattr(row, field)
            if old_value != new_value:
                changes[field] = {
                    "before": self._serialize_audit_value(old_value),
                    "after": self._serialize_audit_value(new_value),
                }
                setattr(row, field, new_value)

        if changes:
            if changes.get("is_default", {}).get("after") is True:
                defaults = self.db.scalars(
                    select(EbayPublishPreset).where(
                        EbayPublishPreset.id != row.id,
                        EbayPublishPreset.environment == row.environment,
                        EbayPublishPreset.username == row.username,
                        EbayPublishPreset.is_default.is_(True),
                    )
                ).all()
                for default_row in defaults:
                    default_row.is_default = False

            self._record_audit("ebay_publish_preset", row.id, "update", actor, changes)
            self.db.commit()
            self.db.refresh(row)
        return row

    def upsert_ebay_listing_template_profile(
        self,
        *,
        environment: str,
        username: str,
        name: str,
        marketplace: str = "ebay",
        listing_title_template: str = "",
        marketplace_details_template: str = "",
        listing_price_default: Decimal | None = None,
        quantity_default: int = 1,
        listing_status_default: str = "draft",
        is_shared: bool = False,
        is_default: bool = False,
        is_active: bool = True,
        actor: str = "system",
    ) -> EbayListingTemplateProfile:
        resolved_env = (environment or "local").strip().lower()
        resolved_user = (username or "").strip()
        resolved_name = (name or "").strip()
        if not resolved_user:
            raise ValueError("Username is required.")
        if not resolved_name:
            raise ValueError("Template name is required.")
        ValidationService.require_positive_int("Template default quantity", quantity_default, min_value=1)
        ValidationService.require_non_negative_decimal("Template default listing price", listing_price_default)

        row = self.db.scalar(
            select(EbayListingTemplateProfile).where(
                EbayListingTemplateProfile.environment == resolved_env,
                EbayListingTemplateProfile.username == resolved_user,
                EbayListingTemplateProfile.name == resolved_name,
            )
        )

        if row is None:
            row = EbayListingTemplateProfile(
                environment=resolved_env,
                username=resolved_user,
                name=resolved_name,
                marketplace=(marketplace or "ebay").strip().lower(),
                listing_title_template=(listing_title_template or "").strip(),
                marketplace_details_template=(marketplace_details_template or "").strip(),
                listing_price_default=listing_price_default,
                quantity_default=max(1, int(quantity_default)),
                listing_status_default=(listing_status_default or "draft").strip().lower(),
                is_shared=bool(is_shared),
                is_default=bool(is_default),
                is_active=bool(is_active),
            )
            self.db.add(row)
            self.db.flush()
            changes = {"after": {"environment": row.environment, "username": row.username, "name": row.name}}
            action = "create"
        else:
            changes: dict[str, dict[str, Any]] = {}
            updates = {
                "marketplace": (marketplace or "ebay").strip().lower(),
                "listing_title_template": (listing_title_template or "").strip(),
                "marketplace_details_template": (marketplace_details_template or "").strip(),
                "listing_price_default": listing_price_default,
                "quantity_default": max(1, int(quantity_default)),
                "listing_status_default": (listing_status_default or "draft").strip().lower(),
                "is_shared": bool(is_shared),
                "is_default": bool(is_default),
                "is_active": bool(is_active),
            }
            for field, new_value in updates.items():
                old_value = getattr(row, field)
                if old_value != new_value:
                    changes[field] = {
                        "before": self._serialize_audit_value(old_value),
                        "after": self._serialize_audit_value(new_value),
                    }
                    setattr(row, field, new_value)
            action = "update"

        if bool(is_default):
            defaults = self.db.scalars(
                select(EbayListingTemplateProfile).where(
                    EbayListingTemplateProfile.id != row.id,
                    EbayListingTemplateProfile.environment == row.environment,
                    EbayListingTemplateProfile.username == row.username,
                    EbayListingTemplateProfile.is_default.is_(True),
                )
            ).all()
            for default_row in defaults:
                default_row.is_default = False

        self._record_audit("ebay_listing_template_profile", row.id, action, actor, changes)
        self.db.commit()
        self.db.refresh(row)
        return row

    def list_ebay_listing_template_profiles(
        self,
        *,
        environment: str,
        username: str,
        include_shared: bool = True,
        active_only: bool = True,
    ) -> list[EbayListingTemplateProfile]:
        resolved_env = (environment or "local").strip().lower()
        resolved_user = (username or "").strip()
        query = select(EbayListingTemplateProfile).where(
            EbayListingTemplateProfile.environment == resolved_env,
        )
        if include_shared:
            query = query.where(
                or_(
                    EbayListingTemplateProfile.username == resolved_user,
                    EbayListingTemplateProfile.is_shared.is_(True),
                )
            )
        else:
            query = query.where(EbayListingTemplateProfile.username == resolved_user)
        if active_only:
            query = query.where(EbayListingTemplateProfile.is_active.is_(True))
        query = query.order_by(
            EbayListingTemplateProfile.is_shared.asc(),
            EbayListingTemplateProfile.is_default.desc(),
            EbayListingTemplateProfile.name.asc(),
        )
        return self.db.scalars(query).all()

    @staticmethod
    def _normalize_ebay_category_query(query: str) -> str:
        parts = [part.strip().lower() for part in str(query or "").split() if part.strip()]
        return " ".join(parts)

    def list_cached_ebay_category_suggestions(
        self,
        *,
        environment: str,
        marketplace_id: str,
        query: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        resolved_env = (environment or "local").strip().lower()
        resolved_marketplace = (marketplace_id or "EBAY_US").strip().upper()
        query_norm = self._normalize_ebay_category_query(query)
        if not query_norm:
            return []
        rows = self.db.scalars(
            select(EbayCategorySuggestion).where(
                EbayCategorySuggestion.environment == resolved_env,
                EbayCategorySuggestion.marketplace_id == resolved_marketplace,
                EbayCategorySuggestion.query_norm == query_norm,
            ).order_by(
                EbayCategorySuggestion.hit_count.desc(),
                EbayCategorySuggestion.last_seen_at.desc(),
                EbayCategorySuggestion.updated_at.desc(),
            ).limit(max(1, min(int(limit), 100)))
        ).all()
        return [
            {
                "category_id": str(row.category_id or "").strip(),
                "category_name": str(row.category_name or "").strip(),
                "path": str(row.path or "").strip(),
                "source": "db_cache",
                "hit_count": int(row.hit_count or 0),
                "last_seen_at": (
                    row.last_seen_at.isoformat() if getattr(row, "last_seen_at", None) is not None else ""
                ),
            }
            for row in rows
            if str(row.category_id or "").strip()
        ]

    def cache_ebay_category_suggestions(
        self,
        *,
        environment: str,
        marketplace_id: str,
        query: str,
        suggestions: list[dict],
        actor: str = "system",
    ) -> int:
        resolved_env = (environment or "local").strip().lower()
        resolved_marketplace = (marketplace_id or "EBAY_US").strip().upper()
        query_raw = str(query or "").strip()
        query_norm = self._normalize_ebay_category_query(query_raw)
        if not query_norm:
            return 0
        upserted = 0
        for row in suggestions or []:
            category_id = str((row or {}).get("category_id") or "").strip()
            if not category_id:
                continue
            category_name = str((row or {}).get("category_name") or "").strip()
            path = str((row or {}).get("path") or "").strip()
            existing = self.db.scalar(
                select(EbayCategorySuggestion).where(
                    EbayCategorySuggestion.environment == resolved_env,
                    EbayCategorySuggestion.marketplace_id == resolved_marketplace,
                    EbayCategorySuggestion.query_norm == query_norm,
                    EbayCategorySuggestion.category_id == category_id,
                )
            )
            if existing is None:
                existing = EbayCategorySuggestion(
                    environment=resolved_env,
                    marketplace_id=resolved_marketplace,
                    query_raw=query_raw,
                    query_norm=query_norm,
                    category_id=category_id,
                    category_name=category_name,
                    path=path,
                    source="ebay_taxonomy",
                    hit_count=1,
                    last_seen_at=utcnow_naive(),
                    created_by=(actor or "system").strip() or "system",
                )
                self.db.add(existing)
            else:
                existing.query_raw = query_raw or existing.query_raw
                existing.category_name = category_name or existing.category_name
                existing.path = path or existing.path
                existing.hit_count = int(existing.hit_count or 0) + 1
                existing.last_seen_at = utcnow_naive()
            upserted += 1
        if upserted:
            self.db.commit()
        return upserted

    def get_cached_ebay_category_aspects(
        self,
        *,
        environment: str,
        marketplace_id: str,
        category_id: str,
    ) -> dict[str, Any] | None:
        resolved_env = (environment or "local").strip().lower()
        resolved_marketplace = (marketplace_id or "EBAY_US").strip().upper()
        resolved_category_id = str(category_id or "").strip()
        if not resolved_category_id:
            return None
        row = self.db.scalar(
            select(EbayCategoryAspect).where(
                EbayCategoryAspect.environment == resolved_env,
                EbayCategoryAspect.marketplace_id == resolved_marketplace,
                EbayCategoryAspect.category_id == resolved_category_id,
            )
        )
        if row is None:
            return None
        try:
            aspects = json.loads(row.aspects_json or "[]")
        except Exception:
            aspects = []
        if not isinstance(aspects, list):
            aspects = []
        return {
            "category_id": str(row.category_id or "").strip(),
            "marketplace_id": str(row.marketplace_id or "").strip(),
            "aspects": aspects,
            "required_count": int(row.required_count or 0),
            "total_count": int(row.total_count or 0),
            "source": "db_cache",
            "hit_count": int(row.hit_count or 0),
            "last_seen_at": (
                row.last_seen_at.isoformat() if getattr(row, "last_seen_at", None) is not None else ""
            ),
        }

    def cache_ebay_category_aspects(
        self,
        *,
        environment: str,
        marketplace_id: str,
        category_id: str,
        aspects: list[dict],
        actor: str = "system",
    ) -> bool:
        resolved_env = (environment or "local").strip().lower()
        resolved_marketplace = (marketplace_id or "EBAY_US").strip().upper()
        resolved_category_id = str(category_id or "").strip()
        if not resolved_category_id:
            return False
        normalized_aspects = aspects if isinstance(aspects, list) else []
        required_count = sum(1 for row in normalized_aspects if bool((row or {}).get("required")))
        total_count = len(normalized_aspects)
        existing = self.db.scalar(
            select(EbayCategoryAspect).where(
                EbayCategoryAspect.environment == resolved_env,
                EbayCategoryAspect.marketplace_id == resolved_marketplace,
                EbayCategoryAspect.category_id == resolved_category_id,
            )
        )
        if existing is None:
            existing = EbayCategoryAspect(
                environment=resolved_env,
                marketplace_id=resolved_marketplace,
                category_id=resolved_category_id,
                aspects_json=json.dumps(normalized_aspects, sort_keys=True),
                required_count=required_count,
                total_count=total_count,
                source="ebay_taxonomy",
                hit_count=1,
                last_seen_at=utcnow_naive(),
                created_by=(actor or "system").strip() or "system",
            )
            self.db.add(existing)
        else:
            existing.aspects_json = json.dumps(normalized_aspects, sort_keys=True)
            existing.required_count = required_count
            existing.total_count = total_count
            existing.hit_count = int(existing.hit_count or 0) + 1
            existing.last_seen_at = utcnow_naive()
        self.db.commit()
        return True

    def update_ebay_listing_template_profile(
        self,
        template_id: int,
        updates: dict[str, Any],
        actor: str = "system",
    ) -> EbayListingTemplateProfile:
        row = self.db.get(EbayListingTemplateProfile, int(template_id))
        if row is None:
            raise ValueError(f"eBay listing template {template_id} not found.")

        changes: dict[str, dict[str, Any]] = {}
        for field, new_value in updates.items():
            if not hasattr(row, field):
                continue
            old_value = getattr(row, field)
            if old_value != new_value:
                changes[field] = {
                    "before": self._serialize_audit_value(old_value),
                    "after": self._serialize_audit_value(new_value),
                }
                setattr(row, field, new_value)
        if changes.get("quantity_default"):
            ValidationService.require_positive_int("Template default quantity", row.quantity_default, min_value=1)
        if changes.get("listing_price_default"):
            ValidationService.require_non_negative_decimal("Template default listing price", row.listing_price_default)

        if changes:
            if changes.get("is_default", {}).get("after") is True:
                defaults = self.db.scalars(
                    select(EbayListingTemplateProfile).where(
                        EbayListingTemplateProfile.id != row.id,
                        EbayListingTemplateProfile.environment == row.environment,
                        EbayListingTemplateProfile.username == row.username,
                        EbayListingTemplateProfile.is_default.is_(True),
                    )
                ).all()
                for default_row in defaults:
                    default_row.is_default = False
            self._record_audit("ebay_listing_template_profile", row.id, "update", actor, changes)
            self.db.commit()
            self.db.refresh(row)
        return row

    def list_ai_provider_configs(
        self,
        *,
        environment: str,
        active_only: bool = False,
    ) -> list[AIProviderConfig]:
        query = select(AIProviderConfig).where(
            AIProviderConfig.environment == (environment or "local").strip().lower()
        )
        if active_only:
            query = query.where(AIProviderConfig.is_active.is_(True))
        query = query.order_by(
            AIProviderConfig.is_default.desc(),
            AIProviderConfig.provider.asc(),
            AIProviderConfig.name.asc(),
        )
        return self.db.scalars(query).all()

    def get_default_ai_provider_config(
        self,
        *,
        environment: str,
    ) -> AIProviderConfig | None:
        resolved_env = (environment or "local").strip().lower()
        default_row = self.db.scalar(
            select(AIProviderConfig).where(
                AIProviderConfig.environment == resolved_env,
                AIProviderConfig.is_default.is_(True),
                AIProviderConfig.is_active.is_(True),
            )
        )
        if default_row is not None:
            return default_row
        return self.db.scalar(
            select(AIProviderConfig).where(
                AIProviderConfig.environment == resolved_env,
                AIProviderConfig.is_active.is_(True),
            )
        )

    def upsert_ai_provider_config(
        self,
        *,
        environment: str,
        name: str,
        provider: str,
        model: str,
        multimodal_model: str = "",
        base_url: str,
        endpoint_type: str,
        api_key: str,
        temperature: Decimal | None,
        max_output_tokens: int,
        timeout_seconds: int,
        notes: str = "",
        is_default: bool = False,
        is_active: bool = True,
        actor: str = "system",
    ) -> AIProviderConfig:
        resolved_env = (environment or "local").strip().lower()
        resolved_name = (name or "").strip()
        resolved_provider = (provider or "openai").strip().lower()
        resolved_model = (model or "").strip()
        resolved_multimodal_model = (multimodal_model or "").strip()
        resolved_base_url = (base_url or "").strip().rstrip("/")
        resolved_endpoint_type = (endpoint_type or "responses").strip().lower()

        if not resolved_name:
            raise ValueError("Profile name is required.")
        if resolved_provider not in {"openai", "localai"}:
            raise ValueError("Provider must be `openai` or `localai`.")
        if not resolved_model:
            raise ValueError("Model is required.")
        if not resolved_base_url:
            raise ValueError("Base URL is required.")
        if resolved_endpoint_type not in {"responses", "chat_completions"}:
            raise ValueError("Endpoint type must be `responses` or `chat_completions`.")
        if max_output_tokens <= 0:
            raise ValueError("Max output tokens must be > 0.")
        if timeout_seconds <= 0:
            raise ValueError("Timeout seconds must be > 0.")

        row = self.db.scalar(
            select(AIProviderConfig).where(
                AIProviderConfig.environment == resolved_env,
                AIProviderConfig.name == resolved_name,
            )
        )
        if row is None:
            row = AIProviderConfig(
                environment=resolved_env,
                name=resolved_name,
                provider=resolved_provider,
                model=resolved_model,
                multimodal_model=(resolved_multimodal_model or resolved_model),
                base_url=resolved_base_url,
                endpoint_type=resolved_endpoint_type,
                api_key=(api_key or "").strip(),
                temperature=temperature if temperature is not None else Decimal("0.20"),
                max_output_tokens=int(max_output_tokens),
                timeout_seconds=int(timeout_seconds),
                notes=(notes or "").strip(),
                is_default=bool(is_default),
                is_active=bool(is_active),
            )
            self.db.add(row)
            self.db.flush()
            if bool(row.is_default):
                defaults = self.db.scalars(
                    select(AIProviderConfig).where(
                        AIProviderConfig.id != row.id,
                        AIProviderConfig.environment == resolved_env,
                        AIProviderConfig.is_default.is_(True),
                    )
                ).all()
                for default_row in defaults:
                    default_row.is_default = False
            self._record_audit(
                "ai_provider_config",
                row.id,
                "create",
                actor,
                {
                    "after": {
                        "environment": row.environment,
                        "name": row.name,
                        "provider": row.provider,
                        "model": row.model,
                        "multimodal_model": row.multimodal_model,
                        "base_url": row.base_url,
                        "endpoint_type": row.endpoint_type,
                        "is_default": row.is_default,
                        "is_active": row.is_active,
                    }
                },
            )
            self.db.commit()
            self.db.refresh(row)
            return row

        updates = {
            "provider": resolved_provider,
            "model": resolved_model,
            "multimodal_model": (resolved_multimodal_model or resolved_model),
            "base_url": resolved_base_url,
            "endpoint_type": resolved_endpoint_type,
            "api_key": ((api_key or "").strip() or row.api_key),
            "temperature": temperature if temperature is not None else Decimal("0.20"),
            "max_output_tokens": int(max_output_tokens),
            "timeout_seconds": int(timeout_seconds),
            "notes": (notes or "").strip(),
            "is_default": bool(is_default),
            "is_active": bool(is_active),
        }
        changes: dict[str, dict[str, Any]] = {}
        for field, new_value in updates.items():
            old_value = getattr(row, field)
            if old_value != new_value:
                changes[field] = {
                    "before": self._serialize_audit_value(old_value),
                    "after": self._serialize_audit_value(new_value),
                }
                setattr(row, field, new_value)

        if changes:
            if bool(updates["is_default"]):
                defaults = self.db.scalars(
                    select(AIProviderConfig).where(
                        AIProviderConfig.id != row.id,
                        AIProviderConfig.environment == row.environment,
                        AIProviderConfig.is_default.is_(True),
                    )
                ).all()
                for default_row in defaults:
                    default_row.is_default = False
            self._record_audit("ai_provider_config", row.id, "update", actor, changes)
            self.db.commit()
            self.db.refresh(row)
        return row

    def update_ai_provider_config(
        self,
        config_id: int,
        updates: dict[str, Any],
        *,
        actor: str = "system",
    ) -> AIProviderConfig:
        row = self.db.get(AIProviderConfig, int(config_id))
        if row is None:
            raise ValueError(f"AI provider config {config_id} not found.")

        changes: dict[str, dict[str, Any]] = {}
        for field, new_value in updates.items():
            if not hasattr(row, field):
                continue
            old_value = getattr(row, field)
            if old_value != new_value:
                changes[field] = {
                    "before": self._serialize_audit_value(old_value),
                    "after": self._serialize_audit_value(new_value),
                }
                setattr(row, field, new_value)

        if changes:
            if changes.get("is_default", {}).get("after") is True:
                defaults = self.db.scalars(
                    select(AIProviderConfig).where(
                        AIProviderConfig.id != row.id,
                        AIProviderConfig.environment == row.environment,
                        AIProviderConfig.is_default.is_(True),
                    )
                ).all()
                for default_row in defaults:
                    default_row.is_default = False
            self._record_audit("ai_provider_config", row.id, "update", actor, changes)
            self.db.commit()
            self.db.refresh(row)
        return row

    def delete_ai_provider_config_by_id(
        self,
        *,
        config_id: int,
        actor: str = "system",
    ) -> bool:
        row = self.db.get(AIProviderConfig, int(config_id))
        if row is None:
            return False
        self.db.delete(row)
        self._record_audit(
            "ai_provider_config",
            int(config_id),
            "delete",
            actor,
            {"name": row.name, "provider": row.provider},
        )
        self.db.commit()
        return True

    def list_runtime_settings(
        self,
        *,
        environment: str,
        active_only: bool = False,
    ) -> list[RuntimeSetting]:
        query = select(RuntimeSetting).where(
            RuntimeSetting.environment == (environment or "local").strip().lower()
        )
        if active_only:
            query = query.where(RuntimeSetting.is_active.is_(True))
        query = query.order_by(RuntimeSetting.key.asc())
        return self.db.scalars(query).all()

    def get_runtime_setting(
        self,
        *,
        environment: str,
        key: str,
        active_only: bool = True,
    ) -> RuntimeSetting | None:
        query = select(RuntimeSetting).where(
            RuntimeSetting.environment == (environment or "local").strip().lower(),
            RuntimeSetting.key == (key or "").strip(),
        )
        if active_only:
            query = query.where(RuntimeSetting.is_active.is_(True))
        return self.db.scalar(query)

    def upsert_runtime_setting(
        self,
        *,
        environment: str,
        key: str,
        value: str,
        value_type: str = "str",
        description: str = "",
        is_active: bool = True,
        actor: str = "system",
    ) -> RuntimeSetting:
        resolved_env = (environment or "local").strip().lower()
        resolved_key = (key or "").strip()
        resolved_value_type = (value_type or "str").strip().lower()
        if not resolved_key:
            raise ValueError("Setting key is required.")
        if resolved_value_type not in {"str", "int", "float", "bool", "json"}:
            raise ValueError("Value type must be one of: str, int, float, bool, json.")

        row = self.db.scalar(
            select(RuntimeSetting).where(
                RuntimeSetting.environment == resolved_env,
                RuntimeSetting.key == resolved_key,
            )
        )
        if row is None:
            row = RuntimeSetting(
                environment=resolved_env,
                key=resolved_key,
                value=(value or "").strip(),
                value_type=resolved_value_type,
                description=(description or "").strip(),
                updated_by=(actor or "system").strip() or "system",
                is_active=bool(is_active),
            )
            self.db.add(row)
            self.db.flush()
            self._record_audit(
                "runtime_setting",
                row.id,
                "create",
                actor,
                {
                    "after": {
                        "environment": row.environment,
                        "key": row.key,
                        "value_type": row.value_type,
                        "is_active": row.is_active,
                    }
                },
            )
            self.db.commit()
            self.db.refresh(row)
            return row

        updates = {
            "value": (value or "").strip(),
            "value_type": resolved_value_type,
            "description": (description or "").strip(),
            "updated_by": (actor or "system").strip() or "system",
            "is_active": bool(is_active),
        }
        changes: dict[str, dict[str, Any]] = {}
        for field, new_value in updates.items():
            old_value = getattr(row, field)
            if old_value != new_value:
                changes[field] = {
                    "before": self._serialize_audit_value(old_value),
                    "after": self._serialize_audit_value(new_value),
                }
                setattr(row, field, new_value)
        if changes:
            self._record_audit("runtime_setting", row.id, "update", actor, changes)
            self.db.commit()
            self.db.refresh(row)
        return row

    def delete_runtime_setting_by_id(
        self,
        *,
        setting_id: int,
        actor: str = "system",
    ) -> bool:
        row = self.db.get(RuntimeSetting, int(setting_id))
        if row is None:
            return False
        self.db.delete(row)
        self._record_audit(
            "runtime_setting",
            int(setting_id),
            "delete",
            actor,
            {"key": row.key},
        )
        self.db.commit()
        return True

    def load_workflow_draft(
        self,
        *,
        environment: str,
        workflow_key: str,
        username: str,
        scope_key: str = "",
        active_only: bool = True,
    ) -> WorkflowDraft | None:
        resolved_env = (environment or "local").strip().lower()
        resolved_workflow = (workflow_key or "").strip().lower()
        resolved_user = (username or "").strip()
        resolved_scope = (scope_key or "").strip()
        if not resolved_workflow or not resolved_user:
            return None
        query = select(WorkflowDraft).where(
            WorkflowDraft.environment == resolved_env,
            WorkflowDraft.workflow_key == resolved_workflow,
            WorkflowDraft.username == resolved_user,
            WorkflowDraft.scope_key == resolved_scope,
        )
        if active_only:
            query = query.where(WorkflowDraft.is_active.is_(True))
        return self.db.scalar(query)

    def resume_latest_workflow_draft(
        self,
        *,
        environment: str,
        workflow_key: str,
        username: str,
        active_only: bool = True,
    ) -> WorkflowDraft | None:
        resolved_env = (environment or "local").strip().lower()
        resolved_workflow = (workflow_key or "").strip().lower()
        resolved_user = (username or "").strip()
        if not resolved_workflow or not resolved_user:
            return None
        query = select(WorkflowDraft).where(
            WorkflowDraft.environment == resolved_env,
            WorkflowDraft.workflow_key == resolved_workflow,
            WorkflowDraft.username == resolved_user,
        )
        if active_only:
            query = query.where(WorkflowDraft.is_active.is_(True))
        query = query.order_by(WorkflowDraft.updated_at.desc(), WorkflowDraft.id.desc())
        row = self.db.scalar(query)
        if row is not None:
            row.resumed_at = utcnow_naive()
            self.db.commit()
            self.db.refresh(row)
        return row

    def save_workflow_draft(
        self,
        *,
        environment: str,
        workflow_key: str,
        username: str,
        scope_key: str = "",
        draft_payload: dict[str, Any] | None = None,
        schema_version: str = "v1",
        status: str = "active",
        last_step: str = "",
        expires_at: datetime | None = None,
        actor: str = "system",
    ) -> WorkflowDraft:
        resolved_env = (environment or "local").strip().lower()
        resolved_workflow = (workflow_key or "").strip().lower()
        resolved_user = (username or "").strip()
        resolved_scope = (scope_key or "").strip()
        if not resolved_workflow:
            raise ValueError("Workflow key is required.")
        if not resolved_user:
            raise ValueError("Username is required.")
        payload = draft_payload if isinstance(draft_payload, dict) else {}
        payload_json = json.dumps(payload, default=self._serialize_audit_value)

        row = self.db.scalar(
            select(WorkflowDraft).where(
                WorkflowDraft.environment == resolved_env,
                WorkflowDraft.workflow_key == resolved_workflow,
                WorkflowDraft.username == resolved_user,
                WorkflowDraft.scope_key == resolved_scope,
            )
        )
        resolved_actor = (actor or "system").strip() or "system"
        if row is None:
            row = WorkflowDraft(
                environment=resolved_env,
                workflow_key=resolved_workflow,
                username=resolved_user,
                scope_key=resolved_scope,
                schema_version=(schema_version or "v1").strip() or "v1",
                status=(status or "active").strip().lower() or "active",
                draft_json=payload_json,
                autosave_count=1,
                last_step=(last_step or "").strip(),
                expires_at=expires_at,
                updated_by=resolved_actor,
                is_active=True,
                cleared_at=None,
            )
            self.db.add(row)
            self.db.flush()
            self._record_audit(
                "workflow_draft",
                row.id,
                "create",
                resolved_actor,
                {
                    "after": {
                        "environment": row.environment,
                        "workflow_key": row.workflow_key,
                        "username": row.username,
                        "scope_key": row.scope_key,
                        "status": row.status,
                    }
                },
            )
        else:
            changes: dict[str, dict[str, Any]] = {}
            updates = {
                "schema_version": (schema_version or "v1").strip() or "v1",
                "status": (status or "active").strip().lower() or "active",
                "draft_json": payload_json,
                "last_step": (last_step or "").strip(),
                "expires_at": expires_at,
                "updated_by": resolved_actor,
                "is_active": True,
                "cleared_at": None,
            }
            for field, new_value in updates.items():
                old_value = getattr(row, field)
                if old_value != new_value:
                    changes[field] = {
                        "before": self._serialize_audit_value(old_value),
                        "after": self._serialize_audit_value(new_value),
                    }
                    setattr(row, field, new_value)
            row.autosave_count = int(row.autosave_count or 0) + 1
            if changes:
                self._record_audit("workflow_draft", row.id, "update", resolved_actor, changes)

        self.db.commit()
        self.db.refresh(row)
        return row

    def clear_workflow_draft(
        self,
        *,
        environment: str,
        workflow_key: str,
        username: str,
        scope_key: str = "",
        actor: str = "system",
        reason: str = "",
    ) -> bool:
        row = self.load_workflow_draft(
            environment=environment,
            workflow_key=workflow_key,
            username=username,
            scope_key=scope_key,
            active_only=False,
        )
        if row is None:
            return False
        row.is_active = False
        row.status = "cleared"
        row.cleared_at = utcnow_naive()
        row.updated_by = (actor or "system").strip() or "system"
        self._record_audit(
            "workflow_draft",
            row.id,
            "clear",
            actor,
            {
                "workflow_key": row.workflow_key,
                "scope_key": row.scope_key,
                "reason": (reason or "").strip(),
            },
        )
        self.db.commit()
        return True

    def append_workflow_event(
        self,
        *,
        environment: str,
        workflow_key: str,
        username: str,
        scope_key: str = "",
        action: str,
        status: str = "ok",
        message: str = "",
        payload: dict[str, Any] | None = None,
        draft_id: int | None = None,
        actor: str = "system",
    ) -> WorkflowEvent:
        resolved_env = (environment or "local").strip().lower()
        resolved_workflow = (workflow_key or "").strip().lower()
        resolved_user = (username or "").strip()
        if not resolved_workflow:
            raise ValueError("Workflow key is required.")
        if not resolved_user:
            raise ValueError("Username is required.")
        resolved_action = (action or "").strip()
        if not resolved_action:
            raise ValueError("Workflow event action is required.")
        row = WorkflowEvent(
            draft_id=int(draft_id) if draft_id else None,
            environment=resolved_env,
            workflow_key=resolved_workflow,
            username=resolved_user,
            scope_key=(scope_key or "").strip(),
            action=resolved_action,
            status=(status or "ok").strip().lower() or "ok",
            message=(message or "").strip(),
            payload_json=json.dumps(payload or {}, default=self._serialize_audit_value),
            created_by=(actor or "system").strip() or "system",
            created_at=utcnow_naive(),
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def list_workflow_events(
        self,
        *,
        environment: str,
        workflow_key: str,
        username: str = "",
        scope_key: str = "",
        limit: int = 100,
    ) -> list[WorkflowEvent]:
        resolved_env = (environment or "local").strip().lower()
        resolved_workflow = (workflow_key or "").strip().lower()
        query = select(WorkflowEvent).where(
            WorkflowEvent.environment == resolved_env,
            WorkflowEvent.workflow_key == resolved_workflow,
        )
        if (username or "").strip():
            query = query.where(WorkflowEvent.username == (username or "").strip())
        if (scope_key or "").strip():
            query = query.where(WorkflowEvent.scope_key == (scope_key or "").strip())
        query = query.order_by(WorkflowEvent.created_at.desc(), WorkflowEvent.id.desc()).limit(
            max(1, min(int(limit), 1000))
        )
        return self.db.scalars(query).all()

    def list_workflow_drafts(
        self,
        *,
        environment: str,
        workflow_key: str = "",
        username: str = "",
        scope_key: str = "",
        active_only: bool = False,
        limit: int = 500,
    ) -> list[WorkflowDraft]:
        resolved_env = (environment or "local").strip().lower()
        resolved_workflow = (workflow_key or "").strip().lower()
        resolved_user = (username or "").strip()
        resolved_scope = (scope_key or "").strip()
        query = select(WorkflowDraft).where(WorkflowDraft.environment == resolved_env)
        if resolved_workflow:
            query = query.where(WorkflowDraft.workflow_key == resolved_workflow)
        if resolved_user:
            query = query.where(WorkflowDraft.username == resolved_user)
        if resolved_scope:
            query = query.where(WorkflowDraft.scope_key == resolved_scope)
        if active_only:
            query = query.where(WorkflowDraft.is_active.is_(True))
        query = query.order_by(WorkflowDraft.updated_at.desc(), WorkflowDraft.id.desc()).limit(
            max(1, min(int(limit), 5000))
        )
        return self.db.scalars(query).all()

    def cleanup_workflow_state(
        self,
        *,
        environment: str,
        draft_retention_days: int = 30,
        event_retention_days: int = 90,
        actor: str = "system",
    ) -> dict[str, int]:
        resolved_env = (environment or "local").strip().lower()
        draft_days = max(1, int(draft_retention_days))
        event_days = max(1, int(event_retention_days))
        now = utcnow_naive()
        draft_cutoff = now - timedelta(days=draft_days)
        event_cutoff = now - timedelta(days=event_days)

        stale_draft_ids = self.db.scalars(
            select(WorkflowDraft.id).where(
                WorkflowDraft.environment == resolved_env,
                or_(
                    WorkflowDraft.is_active.is_(False),
                    WorkflowDraft.cleared_at.is_not(None),
                    WorkflowDraft.expires_at.is_not(None),
                ),
                WorkflowDraft.updated_at <= draft_cutoff,
            )
        ).all()
        stale_draft_ids = [int(x) for x in stale_draft_ids]

        deleted_events_for_stale_drafts = 0
        deleted_stale_drafts = 0
        if stale_draft_ids:
            deleted_events_for_stale_drafts = int(
                self.db.execute(
                    delete(WorkflowEvent).where(
                        WorkflowEvent.environment == resolved_env,
                        WorkflowEvent.draft_id.in_(stale_draft_ids),
                    )
                ).rowcount
                or 0
            )
            deleted_stale_drafts = int(
                self.db.execute(
                    delete(WorkflowDraft).where(
                        WorkflowDraft.environment == resolved_env,
                        WorkflowDraft.id.in_(stale_draft_ids),
                    )
                ).rowcount
                or 0
            )

        deleted_old_events = int(
            self.db.execute(
                delete(WorkflowEvent).where(
                    WorkflowEvent.environment == resolved_env,
                    WorkflowEvent.created_at <= event_cutoff,
                )
            ).rowcount
            or 0
        )
        self.db.commit()

        resolved_actor = (actor or "system").strip() or "system"
        self._record_audit(
            "workflow_state",
            None,
            "cleanup",
            resolved_actor,
            {
                "environment": resolved_env,
                "draft_retention_days": draft_days,
                "event_retention_days": event_days,
                "deleted_stale_drafts": deleted_stale_drafts,
                "deleted_events_for_stale_drafts": deleted_events_for_stale_drafts,
                "deleted_old_events": deleted_old_events,
            },
        )
        self.db.commit()
        return {
            "deleted_stale_drafts": deleted_stale_drafts,
            "deleted_events_for_stale_drafts": deleted_events_for_stale_drafts,
            "deleted_old_events": deleted_old_events,
        }

    def list_saved_filter_profiles(
        self,
        *,
        environment: str,
        scope: str,
        username: str = "",
        include_shared: bool = True,
        active_only: bool = True,
    ) -> list[SavedFilterProfile]:
        resolved_env = (environment or "local").strip().lower()
        resolved_scope = (scope or "").strip().lower()
        resolved_user = (username or "").strip()
        query = select(SavedFilterProfile).where(
            SavedFilterProfile.environment == resolved_env,
            SavedFilterProfile.scope == resolved_scope,
        )
        if include_shared and resolved_user:
            query = query.where(
                or_(
                    SavedFilterProfile.username == resolved_user,
                    SavedFilterProfile.is_shared.is_(True),
                )
            )
        elif resolved_user:
            query = query.where(SavedFilterProfile.username == resolved_user)
        if active_only:
            query = query.where(SavedFilterProfile.is_active.is_(True))
        query = query.order_by(
            SavedFilterProfile.is_shared.asc(),
            SavedFilterProfile.is_default.desc(),
            SavedFilterProfile.name.asc(),
        )
        return self.db.scalars(query).all()

    def upsert_saved_filter_profile(
        self,
        *,
        environment: str,
        username: str,
        scope: str,
        name: str,
        filter_json: str,
        is_shared: bool = False,
        is_default: bool = False,
        is_active: bool = True,
        actor: str = "system",
    ) -> SavedFilterProfile:
        resolved_env = (environment or "local").strip().lower()
        resolved_user = (username or "").strip()
        resolved_scope = (scope or "").strip().lower()
        resolved_name = (name or "").strip()
        if not resolved_user:
            raise ValueError("Username is required.")
        if not resolved_scope:
            raise ValueError("Scope is required.")
        if not resolved_name:
            raise ValueError("Filter name is required.")
        resolved_is_shared = bool(is_shared)
        resolved_is_default = bool(is_default)

        row = self.db.scalar(
            select(SavedFilterProfile).where(
                SavedFilterProfile.environment == resolved_env,
                SavedFilterProfile.username == resolved_user,
                SavedFilterProfile.scope == resolved_scope,
                SavedFilterProfile.name == resolved_name,
            )
        )
        if row is None:
            row = SavedFilterProfile(
                environment=resolved_env,
                username=resolved_user,
                scope=resolved_scope,
                name=resolved_name,
                filter_json=(filter_json or "{}").strip() or "{}",
                is_shared=resolved_is_shared,
                is_default=resolved_is_default,
                is_active=bool(is_active),
            )
            self.db.add(row)
            self.db.flush()
            if resolved_is_default:
                if resolved_is_shared:
                    default_rows = self.db.scalars(
                        select(SavedFilterProfile).where(
                            SavedFilterProfile.id != row.id,
                            SavedFilterProfile.environment == resolved_env,
                            SavedFilterProfile.scope == resolved_scope,
                            SavedFilterProfile.is_shared.is_(True),
                            SavedFilterProfile.is_default.is_(True),
                        )
                    ).all()
                else:
                    default_rows = self.db.scalars(
                        select(SavedFilterProfile).where(
                            SavedFilterProfile.id != row.id,
                            SavedFilterProfile.environment == resolved_env,
                            SavedFilterProfile.username == resolved_user,
                            SavedFilterProfile.scope == resolved_scope,
                            SavedFilterProfile.is_shared.is_(False),
                            SavedFilterProfile.is_default.is_(True),
                        )
                    ).all()
                for default_row in default_rows:
                    default_row.is_default = False
            self._record_audit(
                "saved_filter_profile",
                row.id,
                "create",
                actor,
                {
                    "after": {
                        "environment": row.environment,
                        "username": row.username,
                        "scope": row.scope,
                        "name": row.name,
                        "is_shared": row.is_shared,
                        "is_default": row.is_default,
                        "is_active": row.is_active,
                    }
                },
            )
            self.db.commit()
            self.db.refresh(row)
            return row

        changes: dict[str, dict[str, Any]] = {}
        updates = {
            "filter_json": (filter_json or "{}").strip() or "{}",
            "is_shared": resolved_is_shared,
            "is_default": resolved_is_default,
            "is_active": bool(is_active),
        }
        for field, new_value in updates.items():
            old_value = getattr(row, field)
            if old_value != new_value:
                changes[field] = {
                    "before": self._serialize_audit_value(old_value),
                    "after": self._serialize_audit_value(new_value),
                }
                setattr(row, field, new_value)
        if changes:
            if changes.get("is_default", {}).get("after") is True:
                if bool(row.is_shared):
                    default_rows = self.db.scalars(
                        select(SavedFilterProfile).where(
                            SavedFilterProfile.id != row.id,
                            SavedFilterProfile.environment == row.environment,
                            SavedFilterProfile.scope == row.scope,
                            SavedFilterProfile.is_shared.is_(True),
                            SavedFilterProfile.is_default.is_(True),
                        )
                    ).all()
                else:
                    default_rows = self.db.scalars(
                        select(SavedFilterProfile).where(
                            SavedFilterProfile.id != row.id,
                            SavedFilterProfile.environment == row.environment,
                            SavedFilterProfile.username == row.username,
                            SavedFilterProfile.scope == row.scope,
                            SavedFilterProfile.is_shared.is_(False),
                            SavedFilterProfile.is_default.is_(True),
                        )
                    ).all()
                for default_row in default_rows:
                    default_row.is_default = False
            self._record_audit("saved_filter_profile", row.id, "update", actor, changes)
            self.db.commit()
            self.db.refresh(row)
        return row

    def delete_saved_filter_profile(
        self,
        *,
        environment: str,
        username: str,
        scope: str,
        name: str,
        actor: str = "system",
    ) -> bool:
        row = self.db.scalar(
            select(SavedFilterProfile).where(
                SavedFilterProfile.environment == (environment or "local").strip().lower(),
                SavedFilterProfile.username == (username or "").strip(),
                SavedFilterProfile.scope == (scope or "").strip().lower(),
                SavedFilterProfile.name == (name or "").strip(),
            )
        )
        if row is None:
            return False

        profile_id = row.id
        self.db.delete(row)
        self._record_audit(
            "saved_filter_profile",
            profile_id,
            "delete",
            actor,
            {"name": (name or "").strip(), "scope": (scope or "").strip().lower()},
        )
        self.db.commit()
        return True

    def delete_saved_filter_profile_by_id(
        self,
        *,
        profile_id: int,
        actor: str = "system",
    ) -> bool:
        row = self.db.get(SavedFilterProfile, int(profile_id))
        if row is None:
            return False
        self.db.delete(row)
        self._record_audit(
            "saved_filter_profile",
            int(profile_id),
            "delete",
            actor,
            {"name": row.name, "scope": row.scope},
        )
        self.db.commit()
        return True

    def transfer_shared_filter_ownership(
        self,
        *,
        profile_id: int,
        new_username: str,
        actor: str = "system",
    ) -> SavedFilterProfile:
        row = self.db.get(SavedFilterProfile, int(profile_id))
        if row is None:
            raise ValueError(f"Saved filter profile {profile_id} not found.")
        if not bool(row.is_shared):
            raise ValueError("Ownership transfer is only supported for shared filters.")
        resolved_new_user = (new_username or "").strip()
        if not resolved_new_user:
            raise ValueError("New owner username is required.")
        if row.username == resolved_new_user:
            return row

        existing_conflict = self.db.scalar(
            select(SavedFilterProfile).where(
                SavedFilterProfile.id != row.id,
                SavedFilterProfile.environment == row.environment,
                SavedFilterProfile.username == resolved_new_user,
                SavedFilterProfile.scope == row.scope,
                SavedFilterProfile.name == row.name,
            )
        )
        if existing_conflict is not None:
            raise ValueError(
                "Target owner already has a filter with the same environment/scope/name."
            )

        old_user = row.username
        row.username = resolved_new_user
        self._record_audit(
            "saved_filter_profile",
            row.id,
            "update",
            actor,
            {
                "username": {"before": old_user, "after": resolved_new_user},
                "transfer_reason": {"before": "", "after": "admin_transfer_shared_filter_ownership"},
            },
        )
        self.db.commit()
        self.db.refresh(row)
        return row

    def delete_shared_filter_profile_by_id(
        self,
        *,
        profile_id: int,
        actor: str = "system",
    ) -> bool:
        row = self.db.get(SavedFilterProfile, int(profile_id))
        if row is None:
            return False
        if not bool(row.is_shared):
            raise ValueError("Delete-by-admin in this flow is only for shared filters.")
        self.db.delete(row)
        self._record_audit(
            "saved_filter_profile",
            int(profile_id),
            "delete",
            actor,
            {
                "name": row.name,
                "scope": row.scope,
                "deleted_as": "shared_filter_admin_delete",
            },
        )
        self.db.commit()
        return True

    def list_document_template_profiles(
        self,
        *,
        environment: str | None = None,
        doc_type: str | None = None,
        include_all_doc_type: bool = True,
        active_only: bool = False,
    ) -> list[DocumentTemplateProfile]:
        query = select(DocumentTemplateProfile)
        if environment:
            query = query.where(DocumentTemplateProfile.environment == environment.strip().lower())
        if doc_type:
            resolved = doc_type.strip().lower()
            if include_all_doc_type:
                query = query.where(
                    DocumentTemplateProfile.doc_type.in_([resolved, "all"])
                )
            else:
                query = query.where(DocumentTemplateProfile.doc_type == resolved)
        if active_only:
            query = query.where(DocumentTemplateProfile.is_active.is_(True))
        query = query.order_by(
            DocumentTemplateProfile.is_default.desc(),
            DocumentTemplateProfile.name.asc(),
        )
        return self.db.scalars(query).all()

    def create_document_artifact(
        self,
        *,
        environment: str,
        source_type: str,
        source_id: int | None,
        doc_type: str,
        document_number: str,
        artifact_kind: str,
        file_name: str,
        mime_type: str,
        content_bytes: bytes,
        storage_backend: str = "db_inline",
        storage_ref: str = "",
        actor: str = "system",
    ) -> DocumentArtifact:
        if not isinstance(content_bytes, (bytes, bytearray)):
            raise ValueError("Document artifact content must be bytes.")
        payload_bytes = bytes(content_bytes)
        if not payload_bytes:
            raise ValueError("Document artifact content cannot be empty.")
        encoded = base64.b64encode(payload_bytes).decode("ascii")
        content_sha256 = hashlib.sha256(payload_bytes).hexdigest()
        row = DocumentArtifact(
            environment=(environment or settings.app_env or "local").strip().lower(),
            source_type=(source_type or "").strip(),
            source_id=(int(source_id) if source_id is not None else None),
            doc_type=(doc_type or "").strip().lower(),
            document_number=(document_number or "").strip(),
            artifact_kind=(artifact_kind or "printable_html").strip().lower(),
            file_name=(file_name or "").strip(),
            mime_type=(mime_type or "text/html").strip().lower(),
            content_sha256=content_sha256,
            size_bytes=len(payload_bytes),
            storage_backend=(storage_backend or "db_inline").strip().lower(),
            storage_ref=(storage_ref or "").strip(),
            content_base64=encoded,
            created_by=(actor or "system").strip() or "system",
            created_at=utcnow_naive(),
        )
        self.db.add(row)
        self.db.flush()
        if not row.storage_ref:
            row.storage_ref = f"document_artifacts:{int(row.id)}"
        self._record_audit(
            "document_artifact",
            int(row.id),
            "create",
            actor,
            {
                "environment": row.environment,
                "source_type": row.source_type,
                "source_id": row.source_id,
                "doc_type": row.doc_type,
                "document_number": row.document_number,
                "artifact_kind": row.artifact_kind,
                "file_name": row.file_name,
                "mime_type": row.mime_type,
                "storage_backend": row.storage_backend,
                "storage_ref": row.storage_ref,
                "content_sha256": row.content_sha256,
                "size_bytes": row.size_bytes,
                "immutable_record": True,
            },
        )
        self.db.commit()
        self.db.refresh(row)
        return row

    def list_document_artifacts_for_source(
        self,
        *,
        source_type: str,
        source_id: int | None,
        doc_type: str | None = None,
        limit: int = 50,
    ) -> list[DocumentArtifact]:
        query = select(DocumentArtifact).where(
            DocumentArtifact.source_type == (source_type or "").strip(),
        )
        if source_id is None:
            query = query.where(DocumentArtifact.source_id.is_(None))
        else:
            query = query.where(DocumentArtifact.source_id == int(source_id))
        if doc_type:
            query = query.where(DocumentArtifact.doc_type == (doc_type or "").strip().lower())
        query = query.order_by(DocumentArtifact.created_at.desc(), DocumentArtifact.id.desc()).limit(
            max(1, int(limit))
        )
        return self.db.scalars(query).all()

    def get_document_artifact_content(self, artifact_id: int) -> bytes:
        row = self.db.get(DocumentArtifact, int(artifact_id))
        if row is None:
            raise ValueError(f"Document artifact {artifact_id} not found.")
        encoded = str(row.content_base64 or "").strip()
        if not encoded:
            return b""
        return base64.b64decode(encoded)

    def update_document_template_profile(
        self,
        profile_id: int,
        updates: dict[str, Any],
        actor: str = "system",
    ) -> DocumentTemplateProfile:
        profile = self.db.get(DocumentTemplateProfile, profile_id)
        if profile is None:
            raise ValueError(f"Document template profile {profile_id} not found.")

        changes: dict[str, dict[str, Any]] = {}
        for field, new_value in updates.items():
            if not hasattr(profile, field):
                continue
            old_value = getattr(profile, field)
            if old_value != new_value:
                changes[field] = {
                    "before": self._serialize_audit_value(old_value),
                    "after": self._serialize_audit_value(new_value),
                }
                setattr(profile, field, new_value)

        if changes:
            if changes.get("is_default", {}).get("after") is True:
                defaults = self.db.scalars(
                    select(DocumentTemplateProfile).where(
                        DocumentTemplateProfile.id != profile.id,
                        DocumentTemplateProfile.environment == profile.environment,
                        DocumentTemplateProfile.doc_type == profile.doc_type,
                        DocumentTemplateProfile.is_default.is_(True),
                    )
                ).all()
                for row in defaults:
                    row.is_default = False

            self._record_audit("document_template_profile", profile.id, "update", actor, changes)
            self.db.commit()
            self.db.refresh(profile)
        return profile

    def create_return(
        self,
        marketplace: str,
        quantity: int,
        refund_amount: Decimal,
        sale_id: int | None = None,
        order_id: int | None = None,
        product_id: int | None = None,
        external_return_id: str = "",
        return_status: str = "requested",
        reason: str = "",
        disposition: str = "pending",
        refund_fees: Decimal | None = None,
        refund_shipping: Decimal | None = None,
        restocked: bool = False,
        returned_at: datetime | None = None,
        processed_at: datetime | None = None,
        notes: str = "",
        actor: str = "system",
    ) -> ReturnRecord:
        resolved_product_id = product_id
        if resolved_product_id is None and sale_id is not None:
            sale = self.db.get(Sale, sale_id)
            if sale is not None:
                resolved_product_id = sale.product_id
                if resolved_product_id is None and sale.listing_id is not None:
                    listing = self.db.get(MarketplaceListing, sale.listing_id)
                    if listing is not None:
                        resolved_product_id = listing.product_id
                if not marketplace:
                    marketplace = sale.marketplace
                if order_id is None:
                    order_id = sale.order_id

        ret = ReturnRecord(
            sale_id=sale_id,
            order_id=order_id,
            product_id=resolved_product_id,
            marketplace=marketplace,
            external_return_id=external_return_id,
            return_status=return_status,
            reason=reason,
            disposition=disposition,
            quantity=max(1, int(quantity)),
            refund_amount=refund_amount,
            refund_fees=refund_fees if refund_fees is not None else Decimal("0"),
            refund_shipping=refund_shipping if refund_shipping is not None else Decimal("0"),
            restocked=bool(restocked),
            returned_at=returned_at or utcnow_naive(),
            processed_at=processed_at,
            notes=notes,
        )
        self.db.add(ret)
        self.db.flush()

        bundle_components = self._return_bundle_restock_components(ret.sale_id, ret.quantity)
        if ret.restocked and bundle_components:
            for component in bundle_components:
                product = self.db.get(Product, int(component["product_id"]))
                if product is None:
                    continue
                component_qty = max(1, int(component.get("quantity_total") or 1))
                before = int(product.current_quantity)
                after = before + component_qty
                product.current_quantity = after
                self._record_inventory_movement(
                    product_id=product.id,
                    movement_type="return_bundle_component_restock",
                    quantity_before=before,
                    quantity_after=after,
                    unit_cost=product.acquisition_cost,
                    reference_type="return",
                    reference_id=ret.id,
                    notes=(
                        "Inventory increased from restocked bundle return component: "
                        f"{int(component.get('quantity_per_listing') or 1)} unit(s) per returned listing x "
                        f"{int(ret.quantity or 1)} returned listing unit(s)."
                    ),
                    occurred_at=ret.processed_at or ret.returned_at,
                )
        elif ret.restocked and ret.product_id is not None:
            product = self.db.get(Product, ret.product_id)
            if product is not None:
                before = int(product.current_quantity)
                after = before + int(ret.quantity)
                product.current_quantity = after
                self._record_inventory_movement(
                    product_id=product.id,
                    movement_type="return_restock",
                    quantity_before=before,
                    quantity_after=after,
                    unit_cost=product.acquisition_cost,
                    reference_type="return",
                    reference_id=ret.id,
                    notes="Inventory increased from restocked return.",
                    occurred_at=ret.processed_at or ret.returned_at,
                )

        self._record_audit(
            entity_type="return",
            entity_id=ret.id,
            action="create",
            actor=actor,
            changes={
                "after": {
                    "marketplace": ret.marketplace,
                    "sale_id": ret.sale_id,
                    "order_id": ret.order_id,
                    "product_id": ret.product_id,
                    "quantity": ret.quantity,
                    "restocked": ret.restocked,
                }
            },
        )
        self.db.commit()
        self.db.refresh(ret)
        return ret

    def list_returns(self) -> list[ReturnRecord]:
        return self.db.scalars(select(ReturnRecord).order_by(ReturnRecord.returned_at.desc())).all()

    def update_return(self, return_id: int, updates: dict[str, Any], actor: str = "system") -> ReturnRecord:
        ret = self.db.get(ReturnRecord, return_id)
        if ret is None:
            raise ValueError(f"Return {return_id} not found.")

        old_restocked = bool(ret.restocked)
        old_quantity = int(ret.quantity)
        old_product_id = ret.product_id
        old_sale_id = ret.sale_id
        changes: dict[str, dict[str, Any]] = {}

        for field, new_value in updates.items():
            if not hasattr(ret, field):
                continue
            old_value = getattr(ret, field)
            if old_value != new_value:
                changes[field] = {
                    "before": self._serialize_audit_value(old_value),
                    "after": self._serialize_audit_value(new_value),
                }
                setattr(ret, field, new_value)

        if changes:
            new_restocked = bool(ret.restocked)
            new_quantity = int(ret.quantity)
            new_product_id = ret.product_id
            new_sale_id = ret.sale_id

            if (
                old_restocked != new_restocked
                or old_quantity != new_quantity
                or old_product_id != new_product_id
                or old_sale_id != new_sale_id
            ):
                old_bundle_components = self._return_bundle_restock_components(old_sale_id, old_quantity)
                new_bundle_components = self._return_bundle_restock_components(new_sale_id, new_quantity)
                if old_restocked and old_bundle_components:
                    for component in old_bundle_components:
                        product = self.db.get(Product, int(component["product_id"]))
                        if product is not None:
                            before = int(product.current_quantity)
                            after = max(0, before - max(1, int(component.get("quantity_total") or 1)))
                            product.current_quantity = after
                            self._record_inventory_movement(
                                product_id=product.id,
                                movement_type="return_bundle_component_restock_revert",
                                quantity_before=before,
                                quantity_after=after,
                                unit_cost=product.acquisition_cost,
                                reference_type="return",
                                reference_id=ret.id,
                                notes=f"Reverted previous bundle restock component due to return update by {actor}.",
                                occurred_at=utcnow_naive(),
                            )
                elif old_restocked and old_product_id is not None:
                    product = self.db.get(Product, old_product_id)
                    if product is not None:
                        before = int(product.current_quantity)
                        after = max(0, before - old_quantity)
                        product.current_quantity = after
                        self._record_inventory_movement(
                            product_id=product.id,
                            movement_type="return_restock_revert",
                            quantity_before=before,
                            quantity_after=after,
                            unit_cost=product.acquisition_cost,
                            reference_type="return",
                            reference_id=ret.id,
                            notes=f"Reverted previous restock due to return update by {actor}.",
                            occurred_at=utcnow_naive(),
                        )

                if new_restocked and new_bundle_components:
                    for component in new_bundle_components:
                        product = self.db.get(Product, int(component["product_id"]))
                        if product is not None:
                            before = int(product.current_quantity)
                            after = before + max(1, int(component.get("quantity_total") or 1))
                            product.current_quantity = after
                            self._record_inventory_movement(
                                product_id=product.id,
                                movement_type="return_bundle_component_restock_apply",
                                quantity_before=before,
                                quantity_after=after,
                                unit_cost=product.acquisition_cost,
                                reference_type="return",
                                reference_id=ret.id,
                                notes=f"Applied bundle restock component from updated return by {actor}.",
                                occurred_at=utcnow_naive(),
                            )
                elif new_restocked and new_product_id is not None:
                    product = self.db.get(Product, new_product_id)
                    if product is not None:
                        before = int(product.current_quantity)
                        after = before + new_quantity
                        product.current_quantity = after
                        self._record_inventory_movement(
                            product_id=product.id,
                            movement_type="return_restock_apply",
                            quantity_before=before,
                            quantity_after=after,
                            unit_cost=product.acquisition_cost,
                            reference_type="return",
                            reference_id=ret.id,
                            notes=f"Applied restock from updated return by {actor}.",
                            occurred_at=utcnow_naive(),
                        )

            self._record_audit("return", ret.id, "update", actor, changes)
            self.db.commit()
            self.db.refresh(ret)

        return ret

    def create_order(
        self,
        marketplace: str,
        sold_at: datetime,
        items: list[dict[str, Any]],
        external_order_id: str = "",
        order_status: str = "paid",
        buyer_username: str = "",
        buyer_name: str = "",
        buyer_email: str = "",
        ship_to_city: str = "",
        ship_to_state: str = "",
        ship_to_postal_code: str = "",
        ship_to_country: str = "",
        fees: Decimal | None = None,
        shipping_cost: Decimal | None = None,
        shipping_label_cost: Decimal | None = None,
        shipping_label_currency: str = "USD",
        shipping_provider: str = "",
        shipping_service: str = "",
        tracking_number: str = "",
        tracking_status: str = "",
        shipped_at: datetime | None = None,
        delivered_at: datetime | None = None,
        marketplace_payload_json: str = "{}",
        notes: str = "",
        actor: str = "system",
    ) -> Order:
        ValidationService.require_non_empty("Marketplace", marketplace)
        ValidationService.require_non_negative_decimal("Order fees", fees)
        ValidationService.require_non_negative_decimal("Order shipping cost", shipping_cost)
        ValidationService.require_non_negative_decimal("Order shipping label cost", shipping_label_cost)
        ValidationService.validate_tracking_number((tracking_number or "").strip())
        ValidationService.validate_shipping_dates(tracking_status, shipped_at, delivered_at)

        valid_items = [i for i in items if int(i.get("quantity", 0)) > 0]
        if not valid_items:
            raise ValueError("Order must include at least one valid order item.")
        resolved_external_order_id = (external_order_id or "").strip() or f"internal-{utcnow_naive().strftime('%Y%m%d%H%M%S')}"
        ValidationService.ensure_unique_marketplace_order(
            self.db,
            marketplace,
            resolved_external_order_id,
        )

        subtotal = Decimal("0")
        for item in valid_items:
            quantity = int(item["quantity"])
            unit_price = Decimal(str(item.get("unit_price", 0)))
            ValidationService.require_positive_int("Order item quantity", quantity)
            ValidationService.require_non_negative_decimal("Order item unit price", unit_price)
            subtotal += unit_price * quantity

        fees_value = fees if fees is not None else Decimal("0")
        shipping_cost_value = shipping_cost if shipping_cost is not None else Decimal("0")
        shipping_label_cost_value = shipping_label_cost if shipping_label_cost is not None else None
        total_amount = subtotal

        order = Order(
            marketplace=marketplace,
            external_order_id=resolved_external_order_id,
            order_status=order_status,
            buyer_username=(buyer_username or "").strip(),
            buyer_name=(buyer_name or "").strip(),
            buyer_email=(buyer_email or "").strip(),
            ship_to_city=(ship_to_city or "").strip(),
            ship_to_state=(ship_to_state or "").strip(),
            ship_to_postal_code=(ship_to_postal_code or "").strip(),
            ship_to_country=(ship_to_country or "").strip().upper(),
            subtotal_amount=subtotal,
            fees=fees_value,
            shipping_cost=shipping_cost_value,
            shipping_label_cost=shipping_label_cost_value,
            shipping_label_currency=(shipping_label_currency or "USD").strip().upper() or "USD",
            shipping_provider=(shipping_provider or "").strip(),
            shipping_service=(shipping_service or "").strip(),
            tracking_number=(tracking_number or "").strip(),
            tracking_status=(tracking_status or "").strip(),
            shipped_at=shipped_at,
            delivered_at=delivered_at,
            total_amount=total_amount,
            sold_at=sold_at,
            marketplace_payload_json=marketplace_payload_json if str(marketplace_payload_json or "").strip() else "{}",
            notes=notes,
        )
        self.db.add(order)
        self.db.flush()

        for item in valid_items:
            quantity = int(item["quantity"])
            unit_price = Decimal(str(item.get("unit_price", 0)))
            line_fees = Decimal(str(item.get("line_fees", 0)))
            line_shipping = Decimal(str(item.get("line_shipping", 0)))
            ValidationService.require_non_negative_decimal("Order item fees", line_fees)
            ValidationService.require_non_negative_decimal("Order item shipping", line_shipping)
            line_total = unit_price * quantity
            order_item = OrderItem(
                order_id=order.id,
                product_id=item.get("product_id"),
                listing_id=item.get("listing_id"),
                quantity=quantity,
                unit_price=unit_price,
                line_fees=line_fees,
                line_shipping=line_shipping,
                line_total=line_total,
                notes=(item.get("notes") or "").strip(),
            )
            self.db.add(order_item)

        self._record_audit(
            entity_type="order",
            entity_id=order.id,
            action="create",
            actor=actor,
            changes={
                "after": {
                    "marketplace": marketplace,
                    "external_order_id": resolved_external_order_id,
                    "item_count": len(valid_items),
                    "subtotal_amount": float(subtotal),
                }
            },
        )
        self.db.commit()
        self.db.refresh(order)
        return order

    def list_orders(self) -> list[Order]:
        return self.db.scalars(select(Order).order_by(Order.sold_at.desc())).all()

    def list_orders_by_ids(self, order_ids: set[int] | list[int] | tuple[int, ...]) -> list[Order]:
        normalized_ids = sorted({int(v) for v in (order_ids or []) if v is not None})
        if not normalized_ids:
            return []
        return self.db.scalars(
            select(Order)
            .where(Order.id.in_(normalized_ids))
            .order_by(Order.sold_at.desc())
        ).all()

    def list_order_items(self) -> list[OrderItem]:
        return self.db.scalars(select(OrderItem).order_by(OrderItem.created_at.desc())).all()

    def list_order_items_for_listing(self, listing_id: int) -> list[OrderItem]:
        listing_id_int = int(listing_id)
        return self.db.scalars(
            select(OrderItem)
            .where(OrderItem.listing_id == listing_id_int)
            .order_by(OrderItem.created_at.desc())
        ).all()

    def update_order(self, order_id: int, updates: dict[str, Any], actor: str = "system") -> Order:
        order = self.db.get(Order, order_id)
        if order is None:
            raise ValueError(f"Order {order_id} not found.")

        new_marketplace = updates.get("marketplace", order.marketplace)
        new_external_order_id = updates.get("external_order_id", order.external_order_id)
        new_fees = updates.get("fees", order.fees)
        new_shipping_cost = updates.get("shipping_cost", order.shipping_cost)
        new_shipping_label_cost = updates.get("shipping_label_cost", order.shipping_label_cost)
        new_tracking_number = updates.get("tracking_number", order.tracking_number)
        new_tracking_status = updates.get("tracking_status", order.tracking_status)
        new_shipped_at = updates.get("shipped_at", order.shipped_at)
        new_delivered_at = updates.get("delivered_at", order.delivered_at)
        ValidationService.require_non_empty("Marketplace", new_marketplace)
        ValidationService.require_non_negative_decimal("Order fees", new_fees)
        ValidationService.require_non_negative_decimal("Order shipping cost", new_shipping_cost)
        ValidationService.require_non_negative_decimal("Order shipping label cost", new_shipping_label_cost)
        ValidationService.validate_tracking_number((new_tracking_number or "").strip())
        ValidationService.validate_shipping_dates(new_tracking_status, new_shipped_at, new_delivered_at)
        if "shipping_label_currency" in updates:
            updates["shipping_label_currency"] = (
                str(updates.get("shipping_label_currency") or "USD").strip().upper() or "USD"
            )
        if "ship_to_country" in updates:
            updates["ship_to_country"] = str(updates.get("ship_to_country") or "").strip().upper()
        for _field in (
            "buyer_username",
            "buyer_name",
            "buyer_email",
            "ship_to_city",
            "ship_to_state",
            "ship_to_postal_code",
        ):
            if _field in updates:
                updates[_field] = str(updates.get(_field) or "").strip()
        ValidationService.ensure_unique_marketplace_order(
            self.db,
            new_marketplace,
            new_external_order_id,
            exclude_order_id=order.id,
        )

        changes: dict[str, dict[str, Any]] = {}
        for field, new_value in updates.items():
            if not hasattr(order, field):
                continue
            old_value = getattr(order, field)
            if old_value != new_value:
                changes[field] = {
                    "before": self._serialize_audit_value(old_value),
                    "after": self._serialize_audit_value(new_value),
                }
                setattr(order, field, new_value)

        if changes:
            self._record_audit("order", order.id, "update", actor, changes)
            self.db.commit()
            self.db.refresh(order)
        return order

    def replace_order_finance_entries(
        self,
        order_id: int,
        entries: list[dict[str, Any]],
        actor: str = "system",
    ) -> int:
        order = self.db.get(Order, int(order_id))
        if order is None:
            raise ValueError(f"Order {order_id} not found.")

        self.db.execute(delete(OrderFinanceEntry).where(OrderFinanceEntry.order_id == order.id))
        inserted = 0
        for row in entries or []:
            if not isinstance(row, dict):
                continue
            amount = Decimal(str(row.get("amount", 0) or 0))
            if amount == 0:
                continue
            entry = OrderFinanceEntry(
                order_id=order.id,
                marketplace=(str(row.get("marketplace") or order.marketplace or "ebay").strip().lower() or "ebay"),
                external_order_id=str(row.get("external_order_id") or order.external_order_id or "").strip(),
                transaction_id=str(row.get("transaction_id") or "").strip(),
                line_item_id=str(row.get("line_item_id") or "").strip(),
                legacy_item_id=str(row.get("legacy_item_id") or "").strip(),
                sku=str(row.get("sku") or "").strip(),
                entry_kind=str(row.get("entry_kind") or "other").strip().lower() or "other",
                fee_type=str(row.get("fee_type") or "").strip(),
                amount=amount,
                currency=(str(row.get("currency") or "USD").strip().upper() or "USD"),
                booking_entry=str(row.get("booking_entry") or "").strip().upper(),
                transaction_type=str(row.get("transaction_type") or "").strip().upper(),
                transaction_status=str(row.get("transaction_status") or "").strip().upper(),
                transaction_date=row.get("transaction_date"),
                memo=str(row.get("memo") or "").strip(),
                source=str(row.get("source") or "ebay_finances").strip(),
                raw_json=json.dumps(row.get("raw") or {}, default=str),
            )
            self.db.add(entry)
            inserted += 1

        self._record_audit(
            entity_type="order",
            entity_id=order.id,
            action="sync_finance_entries",
            actor=actor,
            changes={
                "after": {
                    "entry_count": inserted,
                    "external_order_id": order.external_order_id,
                }
            },
        )
        self.db.commit()
        return inserted

    def create_media_asset(
        self,
        media_type: str,
        original_filename: str,
        content_type: str,
        size_bytes: int,
        s3_bucket: str,
        s3_key: str,
        s3_url: str,
        product_id: int | None = None,
        listing_id: int | None = None,
        uploaded_by: str = "system",
    ) -> MediaAsset:
        media = MediaAsset(
            product_id=product_id,
            listing_id=listing_id,
            media_type=media_type,
            original_filename=original_filename,
            content_type=content_type,
            size_bytes=size_bytes,
            s3_bucket=s3_bucket,
            s3_key=s3_key,
            s3_url=s3_url,
            uploaded_by=uploaded_by,
        )
        self.db.add(media)
        self.db.flush()
        self._record_audit(
            entity_type="media_asset",
            entity_id=media.id,
            action="create",
            actor=uploaded_by,
            changes={
                "after": {
                    "product_id": product_id,
                    "listing_id": listing_id,
                    "media_type": media_type,
                    "filename": original_filename,
                    "s3_key": s3_key,
                }
            },
        )
        self.db.commit()
        self.db.refresh(media)
        return media

    def create_purchase_document(
        self,
        *,
        document_kind: str,
        title: str,
        original_filename: str,
        content_type: str,
        size_bytes: int,
        content_sha256: str,
        s3_bucket: str,
        s3_key: str,
        s3_url: str,
        lot_id: int | None = None,
        product_id: int | None = None,
        source_id: int | None = None,
        ai_extracted_json: str = "{}",
        ai_summary: str = "",
        uploaded_by: str = "system",
        actor: str = "system",
    ) -> PurchaseDocument:
        row = PurchaseDocument(
            lot_id=lot_id,
            product_id=product_id,
            source_id=source_id,
            document_kind=(document_kind or "incoming_invoice").strip().lower(),
            title=(title or "").strip(),
            original_filename=(original_filename or "").strip(),
            content_type=(content_type or "application/octet-stream").strip().lower(),
            size_bytes=max(0, int(size_bytes or 0)),
            content_sha256=(content_sha256 or "").strip().lower(),
            s3_bucket=(s3_bucket or "").strip(),
            s3_key=(s3_key or "").strip(),
            s3_url=(s3_url or "").strip(),
            ai_extracted_json=ai_extracted_json if str(ai_extracted_json or "").strip() else "{}",
            ai_summary=(ai_summary or "").strip(),
            uploaded_by=(uploaded_by or "system").strip() or "system",
        )
        self.db.add(row)
        self.db.flush()
        self._record_audit(
            "purchase_document",
            row.id,
            "create",
            actor,
            {
                "after": {
                    "lot_id": row.lot_id,
                    "product_id": row.product_id,
                    "source_id": row.source_id,
                    "document_kind": row.document_kind,
                    "title": row.title,
                    "original_filename": row.original_filename,
                    "content_type": row.content_type,
                    "size_bytes": row.size_bytes,
                    "content_sha256": row.content_sha256,
                    "s3_bucket": row.s3_bucket,
                    "s3_key": row.s3_key,
                    "s3_url": row.s3_url,
                }
            },
        )
        self.db.commit()
        self.db.refresh(row)
        return row

    def list_purchase_documents(self, limit: int = 300) -> list[PurchaseDocument]:
        return self.db.scalars(
            select(PurchaseDocument)
            .order_by(PurchaseDocument.created_at.desc(), PurchaseDocument.id.desc())
            .limit(max(1, int(limit)))
        ).all()

    def update_purchase_document(
        self,
        doc_id: int,
        updates: dict[str, Any],
        actor: str = "system",
    ) -> PurchaseDocument:
        row = self.db.get(PurchaseDocument, int(doc_id))
        if row is None:
            raise ValueError(f"Purchase document {doc_id} not found.")
        changes: dict[str, dict[str, Any]] = {}
        for field, new_value in updates.items():
            if not hasattr(row, field):
                continue
            old_value = getattr(row, field)
            if old_value != new_value:
                changes[field] = {
                    "before": self._serialize_audit_value(old_value),
                    "after": self._serialize_audit_value(new_value),
                }
                setattr(row, field, new_value)
        if changes:
            self._record_audit("purchase_document", row.id, "update", actor, changes)
            self.db.commit()
            self.db.refresh(row)
        return row

    def list_media_assets(
        self,
        *,
        include_archived: bool = False,
        limit: int | None = None,
    ) -> list[MediaAsset]:
        query = select(MediaAsset)
        if not include_archived:
            query = query.where(MediaAsset.is_archived.is_(False))
        query = query.order_by(MediaAsset.created_at.desc())
        if limit is not None:
            query = query.limit(max(1, int(limit)))
        return self.db.scalars(query).all()

    def list_media_assets_for_product(
        self,
        product_id: int,
        *,
        include_archived: bool = False,
    ) -> list[MediaAsset]:
        query = select(MediaAsset).where(MediaAsset.product_id == product_id)
        if not include_archived:
            query = query.where(MediaAsset.is_archived.is_(False))
        return self.db.scalars(query.order_by(MediaAsset.created_at.desc())).all()

    def list_media_assets_for_listing(
        self,
        listing_id: int,
        *,
        include_archived: bool = False,
    ) -> list[MediaAsset]:
        query = select(MediaAsset).where(MediaAsset.listing_id == listing_id)
        if not include_archived:
            query = query.where(MediaAsset.is_archived.is_(False))
        return self.db.scalars(query.order_by(MediaAsset.created_at.desc())).all()

    def count_media_assets_for_listing(
        self,
        listing_id: int,
        *,
        include_archived: bool = False,
    ) -> int:
        query = select(func.count()).select_from(MediaAsset).where(MediaAsset.listing_id == int(listing_id))
        if not include_archived:
            query = query.where(MediaAsset.is_archived.is_(False))
        return int(self.db.scalar(query) or 0)

    def list_media_assets_by_ids(
        self,
        media_ids: list[int],
        *,
        include_archived: bool = True,
    ) -> list[MediaAsset]:
        normalized_ids = sorted({int(v) for v in (media_ids or []) if int(v) > 0})
        if not normalized_ids:
            return []
        query = select(MediaAsset).where(MediaAsset.id.in_(normalized_ids))
        if not include_archived:
            query = query.where(MediaAsset.is_archived.is_(False))
        return self.db.scalars(query.order_by(MediaAsset.created_at.desc())).all()

    def list_unlinked_product_media_ids(
        self,
        product_id: int,
        *,
        include_archived: bool = False,
    ) -> list[int]:
        query = select(MediaAsset.id).where(
            MediaAsset.product_id == int(product_id),
            MediaAsset.listing_id.is_(None),
        )
        if not include_archived:
            query = query.where(MediaAsset.is_archived.is_(False))
        rows = self.db.execute(query).all()
        return [int(getattr(row, "id", 0) or 0) for row in rows if int(getattr(row, "id", 0) or 0) > 0]

    def listing_media_count_map(
        self,
        *,
        listing_ids: list[int] | None = None,
        include_archived: bool = False,
    ) -> dict[int, int]:
        query = select(
            MediaAsset.listing_id,
            func.count(MediaAsset.id).label("media_count"),
        ).where(MediaAsset.listing_id.is_not(None))
        if not include_archived:
            query = query.where(MediaAsset.is_archived.is_(False))
        normalized_ids = [int(v) for v in (listing_ids or []) if int(v) > 0]
        if normalized_ids:
            query = query.where(MediaAsset.listing_id.in_(normalized_ids))
        query = query.group_by(MediaAsset.listing_id)
        rows = self.db.execute(query).all()
        return {
            int(getattr(row, "listing_id", 0) or 0): int(getattr(row, "media_count", 0) or 0)
            for row in rows
            if int(getattr(row, "listing_id", 0) or 0) > 0
        }

    def create_purchase_lot(
        self,
        lot_code: str,
        vendor: str,
        purchase_date: datetime,
        total_cost: Decimal | None,
        total_tax_paid: Decimal | None = None,
        total_shipping_paid: Decimal | None = None,
        total_handling_paid: Decimal | None = None,
        expected_total_quantity: int | None = None,
        notes: str = "",
        source_id: int | None = None,
        ebay_purchase: bool = False,
        ebay_purchase_item_id: str = "",
        ebay_purchase_url: str = "",
    ) -> PurchaseLot:
        resolved_vendor = vendor
        if source_id is not None:
            source = self.db.get(InventorySource, source_id)
            if source is not None and not resolved_vendor.strip():
                resolved_vendor = source.name
        ebay_purchase_item_id_value = (ebay_purchase_item_id or "").strip()
        ebay_purchase_url_value = (ebay_purchase_url or "").strip()
        if ebay_purchase and not ebay_purchase_item_id_value:
            raise ValueError("eBay purchase item ID is required when eBay purchase is enabled.")
        if not ebay_purchase:
            ebay_purchase_item_id_value = ""
            ebay_purchase_url_value = ""
        ValidationService.require_non_negative_decimal("Lot total cost", total_cost)
        ValidationService.require_non_negative_decimal("Lot total tax paid", total_tax_paid)
        ValidationService.require_non_negative_decimal("Lot total shipping paid", total_shipping_paid)
        ValidationService.require_non_negative_decimal("Lot total handling paid", total_handling_paid)
        if expected_total_quantity is not None:
            ValidationService.require_positive_int(
                "Lot expected total quantity",
                int(expected_total_quantity),
                min_value=0,
            )
        lot = PurchaseLot(
            source_id=source_id,
            lot_code=lot_code,
            vendor=resolved_vendor,
            purchase_date=purchase_date,
            total_cost=total_cost,
            total_tax_paid=total_tax_paid,
            total_shipping_paid=total_shipping_paid,
            total_handling_paid=total_handling_paid,
            expected_total_quantity=expected_total_quantity,
            ebay_purchase=bool(ebay_purchase),
            ebay_purchase_item_id=ebay_purchase_item_id_value,
            ebay_purchase_url=ebay_purchase_url_value,
            notes=notes,
        )
        self.db.add(lot)
        self.db.flush()
        self._record_audit(
            entity_type="purchase_lot",
            entity_id=lot.id,
            action="create",
            actor="system",
            changes={
                "after": {
                    "lot_code": lot_code,
                    "vendor": resolved_vendor,
                    "source_id": source_id,
                    "total_tax_paid": total_tax_paid,
                    "total_shipping_paid": total_shipping_paid,
                    "total_handling_paid": total_handling_paid,
                    "expected_total_quantity": expected_total_quantity,
                    "ebay_purchase": bool(ebay_purchase),
                    "ebay_purchase_item_id": ebay_purchase_item_id_value,
                    "ebay_purchase_url": ebay_purchase_url_value,
                }
            },
        )
        self.db.commit()
        self.db.refresh(lot)
        return lot

    def create_inventory_source(
        self,
        name: str,
        source_type: str,
        contact_name: str = "",
        contact_email: str = "",
        contact_phone: str = "",
        source_url: str = "",
        ebay_store_url: str = "",
        account_id: str = "",
        payment_method: str = "",
        notes: str = "",
        is_active: bool = True,
    ) -> InventorySource:
        source = InventorySource(
            name=name,
            source_type=source_type,
            contact_name=contact_name,
            contact_email=contact_email,
            contact_phone=contact_phone,
            source_url=source_url,
            ebay_store_url=ebay_store_url,
            account_id=account_id,
            payment_method=payment_method,
            notes=notes,
            is_active=is_active,
        )
        self.db.add(source)
        self.db.flush()
        self._record_audit(
            entity_type="inventory_source",
            entity_id=source.id,
            action="create",
            actor="system",
            changes={
                "after": {
                    "name": name,
                    "source_type": source_type,
                    "source_url": source_url,
                    "ebay_store_url": ebay_store_url,
                    "account_id": account_id,
                    "payment_method": payment_method,
                    "is_active": is_active,
                }
            },
        )
        self.db.commit()
        self.db.refresh(source)
        return source

    def list_inventory_sources(self, active_only: bool = False) -> list[InventorySource]:
        query = select(InventorySource)
        if active_only:
            query = query.where(InventorySource.is_active.is_(True))
        return self.db.scalars(query.order_by(InventorySource.name.asc())).all()

    def update_inventory_source(
        self, source_id: int, updates: dict[str, Any], actor: str = "system"
    ) -> InventorySource:
        source = self.db.get(InventorySource, source_id)
        if source is None:
            raise ValueError(f"Inventory source {source_id} not found.")

        changes: dict[str, dict[str, Any]] = {}
        for field, new_value in updates.items():
            if not hasattr(source, field):
                continue
            old_value = getattr(source, field)
            if old_value != new_value:
                changes[field] = {
                    "before": self._serialize_audit_value(old_value),
                    "after": self._serialize_audit_value(new_value),
                }
                setattr(source, field, new_value)

        if changes:
            self._record_audit("inventory_source", source.id, "update", actor, changes)
            self.db.commit()
            self.db.refresh(source)
        return source

    def list_purchase_lots(self) -> list[PurchaseLot]:
        return self.db.scalars(select(PurchaseLot).order_by(PurchaseLot.purchase_date.desc())).all()

    def update_purchase_lot(
        self,
        lot_id: int,
        updates: dict[str, Any],
        actor: str = "system",
    ) -> PurchaseLot:
        lot = self.db.get(PurchaseLot, int(lot_id))
        if lot is None:
            raise ValueError(f"Purchase lot {lot_id} not found.")

        changes: dict[str, dict[str, Any]] = {}
        for field, new_value in updates.items():
            if not hasattr(lot, field):
                continue
            if field == "expected_total_quantity" and new_value is not None:
                ValidationService.require_positive_int(
                    "Lot expected total quantity",
                    int(new_value),
                    min_value=0,
                )
            old_value = getattr(lot, field)
            if old_value != new_value:
                changes[field] = {
                    "before": self._serialize_audit_value(old_value),
                    "after": self._serialize_audit_value(new_value),
                }
                setattr(lot, field, new_value)

        if changes:
            self._record_audit("purchase_lot", lot.id, "update", actor, changes)
            self.db.commit()
            self.db.refresh(lot)
        return lot

    def get_purchase_lot_archive_blockers(self, lot_id: int) -> dict[str, int]:
        lot = self.db.get(PurchaseLot, int(lot_id))
        if lot is None:
            raise ValueError(f"Purchase lot {lot_id} not found.")
        lot_id_int = int(lot.id)
        assignments_count = int(
            self.db.scalar(
                select(func.count()).select_from(ProductLotAssignment).where(ProductLotAssignment.lot_id == lot_id_int)
            )
            or 0
        )
        documents_count = int(
            self.db.scalar(select(func.count()).select_from(PurchaseDocument).where(PurchaseDocument.lot_id == lot_id_int))
            or 0
        )
        active_products_count = int(
            self.db.scalar(
                select(func.count())
                .select_from(Product)
                .join(ProductLotAssignment, ProductLotAssignment.product_id == Product.id)
                .where(
                    ProductLotAssignment.lot_id == lot_id_int,
                    func.lower(func.trim(func.coalesce(cast(Product.status, String), ""))) != "archived",
                )
            )
            or 0
        )
        active_listings_count = int(
            self.db.scalar(
                select(func.count())
                .select_from(MarketplaceListing)
                .join(ProductLotAssignment, ProductLotAssignment.product_id == MarketplaceListing.product_id)
                .where(
                    ProductLotAssignment.lot_id == lot_id_int,
                    func.lower(func.trim(func.coalesce(cast(MarketplaceListing.listing_status, String), ""))) == "active",
                )
            )
            or 0
        )
        return {
            "product_assignments": assignments_count,
            "purchase_documents": documents_count,
            "active_products": active_products_count,
            "active_listings": active_listings_count,
        }

    def archive_purchase_lot(
        self,
        lot_id: int,
        *,
        actor: str = "system",
        reason: str = "",
        force: bool = False,
    ) -> PurchaseLot:
        lot = self.db.get(PurchaseLot, int(lot_id))
        if lot is None:
            raise ValueError(f"Purchase lot {lot_id} not found.")
        blockers = self.get_purchase_lot_archive_blockers(int(lot.id))
        has_blockers = any(int(v or 0) > 0 for v in blockers.values())
        if has_blockers and not bool(force):
            raise ValueError(
                "Cannot archive lot with linked records. "
                "Use force=True to confirm archive despite dependencies."
            )

        notes_raw = str(lot.notes or "").strip()
        notes_obj: dict[str, Any] = {}
        if notes_raw:
            try:
                parsed = json.loads(notes_raw)
                if isinstance(parsed, dict):
                    notes_obj = parsed
                else:
                    notes_obj = {"notes": notes_raw}
            except Exception:
                notes_obj = {"notes": notes_raw}

        lifecycle = notes_obj.get("lifecycle")
        if not isinstance(lifecycle, dict):
            lifecycle = {}
        lifecycle["archived"] = True
        lifecycle["archived_at"] = utcnow_naive().isoformat()
        lifecycle["archived_by"] = (actor or "system").strip() or "system"
        lifecycle["archive_reason"] = str(reason or "").strip()
        lifecycle["archive_forced"] = bool(force)
        lifecycle["archive_blockers"] = blockers
        notes_obj["lifecycle"] = lifecycle
        return self.update_purchase_lot(
            int(lot_id),
            {"notes": json.dumps(notes_obj, indent=2)},
            actor=actor,
        )

    def restore_purchase_lot(self, lot_id: int, *, actor: str = "system") -> PurchaseLot:
        lot = self.db.get(PurchaseLot, int(lot_id))
        if lot is None:
            raise ValueError(f"Purchase lot {lot_id} not found.")

        notes_raw = str(lot.notes or "").strip()
        notes_obj: dict[str, Any] = {}
        if notes_raw:
            try:
                parsed = json.loads(notes_raw)
                if isinstance(parsed, dict):
                    notes_obj = parsed
                else:
                    notes_obj = {"notes": notes_raw}
            except Exception:
                notes_obj = {"notes": notes_raw}

        lifecycle = notes_obj.get("lifecycle")
        if not isinstance(lifecycle, dict):
            lifecycle = {}
        lifecycle["archived"] = False
        lifecycle["restored_at"] = utcnow_naive().isoformat()
        lifecycle["restored_by"] = (actor or "system").strip() or "system"
        notes_obj["lifecycle"] = lifecycle
        return self.update_purchase_lot(
            int(lot_id),
            {"notes": json.dumps(notes_obj, indent=2)},
            actor=actor,
        )

    def assign_product_to_lot(
        self,
        product_id: int,
        lot_id: int,
        quantity_acquired: int,
        unit_cost: Decimal | None,
        acquired_at: datetime,
        unit_tax_paid: Decimal | None = None,
        unit_shipping_paid: Decimal | None = None,
        unit_handling_paid: Decimal | None = None,
        allocated_cost: Decimal | None = None,
        allocation_weight: Decimal | None = None,
    ) -> ProductLotAssignment:
        ValidationService.require_non_negative_decimal("Unit cost", unit_cost)
        ValidationService.require_non_negative_decimal("Unit tax paid", unit_tax_paid)
        ValidationService.require_non_negative_decimal("Unit shipping paid", unit_shipping_paid)
        ValidationService.require_non_negative_decimal("Unit handling paid", unit_handling_paid)
        ValidationService.require_non_negative_decimal("Allocated lot cost", allocated_cost)
        ValidationService.require_non_negative_decimal("Allocation weight", allocation_weight)
        if allocated_cost is None:
            allocated_cost = ((unit_cost * quantity_acquired) if unit_cost is not None else None)
        allocated_tax_paid = ((unit_tax_paid * quantity_acquired) if unit_tax_paid is not None else None)
        allocated_shipping_paid = (
            (unit_shipping_paid * quantity_acquired) if unit_shipping_paid is not None else None
        )
        allocated_handling_paid = (
            (unit_handling_paid * quantity_acquired) if unit_handling_paid is not None else None
        )
        if allocated_tax_paid is None and allocated_cost is not None:
            lot_row = self.db.get(PurchaseLot, lot_id)
            allocated_tax_paid = self._allocate_lot_tax_paid(
                lot_total_tax_paid=(lot_row.total_tax_paid if lot_row is not None else None),
                lot_total_cost=(lot_row.total_cost if lot_row is not None else None),
                allocated_cost=allocated_cost,
            )
            if allocated_shipping_paid is None:
                allocated_shipping_paid = self._allocate_lot_component_paid(
                    lot_component_total=(lot_row.total_shipping_paid if lot_row is not None else None),
                    lot_total_cost=(lot_row.total_cost if lot_row is not None else None),
                    allocated_cost=allocated_cost,
                )
            if allocated_handling_paid is None:
                allocated_handling_paid = self._allocate_lot_component_paid(
                    lot_component_total=(lot_row.total_handling_paid if lot_row is not None else None),
                    lot_total_cost=(lot_row.total_cost if lot_row is not None else None),
                    allocated_cost=allocated_cost,
                )
        assignment = ProductLotAssignment(
            product_id=product_id,
            lot_id=lot_id,
            quantity_acquired=quantity_acquired,
            unit_cost=unit_cost,
            unit_tax_paid=unit_tax_paid,
            unit_shipping_paid=unit_shipping_paid,
            unit_handling_paid=unit_handling_paid,
            allocated_cost=allocated_cost,
            allocated_tax_paid=allocated_tax_paid,
            allocated_shipping_paid=allocated_shipping_paid,
            allocated_handling_paid=allocated_handling_paid,
            allocation_weight=allocation_weight,
            acquired_at=acquired_at,
        )
        self.db.add(assignment)
        self.db.flush()
        self._record_audit(
            entity_type="product_lot_assignment",
            entity_id=assignment.id,
            action="create",
            actor="system",
            changes={
                "after": {
                    "product_id": product_id,
                    "lot_id": lot_id,
                    "quantity_acquired": int(quantity_acquired),
                    "allocated_cost": self._serialize_audit_value(allocated_cost),
                    "allocation_weight": self._serialize_audit_value(allocation_weight),
                }
            },
        )
        self.db.commit()
        self.db.refresh(assignment)
        return assignment

    def record_product_repurchase(
        self,
        *,
        product_id: int,
        quantity_acquired: int,
        unit_cost: Decimal | None,
        unit_tax_paid: Decimal | None = None,
        unit_shipping_paid: Decimal | None = None,
        unit_handling_paid: Decimal | None = None,
        unit_product_cost: Decimal | None = None,
        acquired_at: datetime | None = None,
        lot_id: int | None = None,
        notes: str = "",
        actor: str = "system",
    ) -> Product:
        product = self.db.get(Product, product_id)
        if product is None:
            raise ValueError(f"Product {product_id} not found.")
        ValidationService.require_positive_int("Repurchase quantity", quantity_acquired, min_value=1)
        ValidationService.require_non_negative_decimal("Repurchase unit cost", unit_cost)
        ValidationService.require_non_negative_decimal("Repurchase unit tax paid", unit_tax_paid)
        ValidationService.require_non_negative_decimal("Repurchase unit shipping paid", unit_shipping_paid)
        ValidationService.require_non_negative_decimal("Repurchase unit handling paid", unit_handling_paid)
        ValidationService.require_non_negative_decimal("Repurchase unit product cost", unit_product_cost)

        occurred = acquired_at or utcnow_naive()
        qty_before = int(product.current_quantity or 0)
        qty_after = qty_before + int(quantity_acquired)

        assignment = None
        allocated_cost = (unit_cost * int(quantity_acquired)) if unit_cost is not None else None
        allocated_tax_paid = (unit_tax_paid * int(quantity_acquired)) if unit_tax_paid is not None else None
        allocated_shipping_paid = (
            (unit_shipping_paid * int(quantity_acquired)) if unit_shipping_paid is not None else None
        )
        allocated_handling_paid = (
            (unit_handling_paid * int(quantity_acquired)) if unit_handling_paid is not None else None
        )
        if lot_id is not None:
            assignment = ProductLotAssignment(
                product_id=product.id,
                lot_id=lot_id,
                quantity_acquired=int(quantity_acquired),
                unit_cost=unit_cost,
                unit_tax_paid=unit_tax_paid,
                unit_shipping_paid=unit_shipping_paid,
                unit_handling_paid=unit_handling_paid,
                allocated_cost=allocated_cost,
                allocated_tax_paid=allocated_tax_paid,
                allocated_shipping_paid=allocated_shipping_paid,
                allocated_handling_paid=allocated_handling_paid,
                acquired_at=occurred,
            )
            self.db.add(assignment)
            self.db.flush()

        def _weighted_average(
            existing_value: Decimal | None,
            new_value: Decimal | None,
        ) -> Decimal | None:
            resolved_existing = existing_value if existing_value is not None else Decimal("0")
            if new_value is None:
                return existing_value
            if qty_before > 0:
                weighted_total = (resolved_existing * qty_before) + (new_value * int(quantity_acquired))
                return weighted_total / Decimal(qty_after)
            return new_value

        existing_unit_cost = product.acquisition_cost
        existing_unit_tax = product.acquisition_tax_paid
        existing_unit_shipping = product.acquisition_shipping_paid
        existing_unit_handling = product.acquisition_handling_paid
        existing_unit_product_cost = product.product_cost

        new_unit_cost = _weighted_average(existing_unit_cost, unit_cost)
        new_unit_tax = _weighted_average(existing_unit_tax, unit_tax_paid)
        new_unit_shipping = _weighted_average(existing_unit_shipping, unit_shipping_paid)
        new_unit_handling = _weighted_average(existing_unit_handling, unit_handling_paid)
        if unit_product_cost is None:
            unit_product_cost = unit_cost
        new_unit_product_cost = _weighted_average(existing_unit_product_cost, unit_product_cost)

        product.current_quantity = qty_after
        product.acquisition_cost = new_unit_cost
        product.acquisition_tax_paid = new_unit_tax
        product.acquisition_shipping_paid = new_unit_shipping
        product.acquisition_handling_paid = new_unit_handling
        product.product_cost = new_unit_product_cost
        product.acquired_at = occurred
        landed_unit_cost = self._landed_unit_cost_decimal(
            unit_cost=unit_cost,
            unit_tax_paid=unit_tax_paid,
            unit_shipping_paid=unit_shipping_paid,
            unit_handling_paid=unit_handling_paid,
        )

        self._record_inventory_movement(
            product_id=product.id,
            movement_type="repurchase_in",
            quantity_before=qty_before,
            quantity_after=qty_after,
            unit_cost=(landed_unit_cost if landed_unit_cost is not None else unit_cost),
            reference_type="purchase_lot" if assignment is not None else "product",
            reference_id=(assignment.id if assignment is not None else product.id),
            notes=(notes or "").strip() or "Repurchase/restock recorded for existing product.",
            occurred_at=occurred,
        )
        self._record_audit(
            entity_type="product",
            entity_id=product.id,
            action="repurchase",
            actor=actor,
            changes={
                "quantity": {"before": qty_before, "after": qty_after},
                "acquisition_cost": {
                    "before": self._serialize_audit_value(existing_unit_cost),
                    "after": self._serialize_audit_value(new_unit_cost),
                },
                "acquisition_tax_paid": {
                    "before": self._serialize_audit_value(existing_unit_tax),
                    "after": self._serialize_audit_value(new_unit_tax),
                },
                "acquisition_shipping_paid": {
                    "before": self._serialize_audit_value(existing_unit_shipping),
                    "after": self._serialize_audit_value(new_unit_shipping),
                },
                "acquisition_handling_paid": {
                    "before": self._serialize_audit_value(existing_unit_handling),
                    "after": self._serialize_audit_value(new_unit_handling),
                },
                "product_cost": {
                    "before": self._serialize_audit_value(existing_unit_product_cost),
                    "after": self._serialize_audit_value(new_unit_product_cost),
                },
                "repurchase": {
                    "quantity_acquired": int(quantity_acquired),
                    "unit_cost": self._serialize_audit_value(unit_cost),
                    "allocated_cost": self._serialize_audit_value(allocated_cost),
                    "unit_tax_paid": self._serialize_audit_value(unit_tax_paid),
                    "unit_shipping_paid": self._serialize_audit_value(unit_shipping_paid),
                    "unit_handling_paid": self._serialize_audit_value(unit_handling_paid),
                    "unit_product_cost": self._serialize_audit_value(unit_product_cost),
                    "allocated_tax_paid": self._serialize_audit_value(allocated_tax_paid),
                    "allocated_shipping_paid": self._serialize_audit_value(allocated_shipping_paid),
                    "allocated_handling_paid": self._serialize_audit_value(allocated_handling_paid),
                    "landed_unit_cost": self._serialize_audit_value(landed_unit_cost),
                    "lot_id": lot_id,
                    "product_lot_assignment_id": (assignment.id if assignment is not None else None),
                    "notes": (notes or "").strip(),
                },
            },
        )
        self.db.commit()
        self.db.refresh(product)
        return product

    def list_product_lot_assignments(self) -> list[ProductLotAssignment]:
        return self.db.scalars(
            select(ProductLotAssignment).order_by(ProductLotAssignment.created_at.desc())
        ).all()

    def update_product_lot_assignment(
        self,
        assignment_id: int,
        updates: dict[str, Any],
        actor: str = "system",
    ) -> ProductLotAssignment:
        assignment = self.db.get(ProductLotAssignment, int(assignment_id))
        if assignment is None:
            raise ValueError(f"Product lot assignment {int(assignment_id)} not found.")

        allowed_fields = {
            "quantity_acquired",
            "unit_cost",
            "unit_tax_paid",
            "unit_shipping_paid",
            "unit_handling_paid",
            "allocated_cost",
            "allocated_tax_paid",
            "allocated_shipping_paid",
            "allocated_handling_paid",
            "allocation_weight",
            "acquired_at",
        }
        payload = {field: value for field, value in updates.items() if field in allowed_fields}
        if "quantity_acquired" in payload:
            ValidationService.require_positive_int(
                "Quantity acquired",
                int(payload.get("quantity_acquired") or 0),
                min_value=1,
            )
            payload["quantity_acquired"] = int(payload["quantity_acquired"])
        for field, label in {
            "unit_cost": "Unit cost",
            "unit_tax_paid": "Unit tax paid",
            "unit_shipping_paid": "Unit shipping paid",
            "unit_handling_paid": "Unit handling paid",
            "allocated_cost": "Allocated lot cost",
            "allocated_tax_paid": "Allocated tax paid",
            "allocated_shipping_paid": "Allocated shipping paid",
            "allocated_handling_paid": "Allocated handling paid",
            "allocation_weight": "Allocation weight",
        }.items():
            if field in payload:
                ValidationService.require_non_negative_decimal(label, payload[field])

        changes: dict[str, dict[str, Any]] = {}
        for field, new_value in payload.items():
            old_value = getattr(assignment, field)
            if old_value != new_value:
                changes[field] = {
                    "before": self._serialize_audit_value(old_value),
                    "after": self._serialize_audit_value(new_value),
                }
                setattr(assignment, field, new_value)

        if changes:
            self._record_audit(
                entity_type="product_lot_assignment",
                entity_id=int(assignment.id),
                action="update",
                actor=actor,
                changes=changes,
            )
            self.db.commit()
            self.db.refresh(assignment)
        return assignment

    def list_inventory_movements(self, limit: int = 500) -> list[InventoryMovement]:
        return self.db.scalars(
            select(InventoryMovement).order_by(InventoryMovement.occurred_at.desc()).limit(limit)
        ).all()

    def report_inventory_movement_rows(
        self,
        *,
        start_dt: datetime,
        end_dt: datetime,
    ) -> list[dict]:
        query = (
            select(
                InventoryMovement.id.label("movement_id"),
                InventoryMovement.occurred_at.label("occurred_at"),
                InventoryMovement.product_id.label("product_id"),
                Product.sku.label("sku"),
                Product.title.label("product_title"),
                cast(InventoryMovement.movement_type, String).label("movement_type"),
                InventoryMovement.quantity_delta.label("quantity_delta"),
                InventoryMovement.quantity_before.label("quantity_before"),
                InventoryMovement.quantity_after.label("quantity_after"),
                InventoryMovement.unit_cost.label("unit_cost"),
                InventoryMovement.reference_type.label("reference_type"),
                InventoryMovement.reference_id.label("reference_id"),
                InventoryMovement.notes.label("notes"),
            )
            .select_from(InventoryMovement)
            .join(Product, Product.id == InventoryMovement.product_id, isouter=True)
            .where(
                or_(
                    InventoryMovement.occurred_at.is_(None),
                    InventoryMovement.occurred_at.between(start_dt, end_dt),
                )
            )
            .order_by(InventoryMovement.occurred_at.desc(), InventoryMovement.id.desc())
        )
        rows = self.db.execute(query).all()
        output: list[dict] = []
        for row in rows:
            output.append(
                {
                    "movement_id": int(row.movement_id or 0),
                    "occurred_at": row.occurred_at.isoformat() if row.occurred_at else None,
                    "product_id": int(row.product_id) if row.product_id is not None else None,
                    "sku": str(row.sku or "").strip() or None,
                    "product_title": str(row.product_title or "").strip() or None,
                    "movement_type": str(row.movement_type or "").strip(),
                    "quantity_delta": int(row.quantity_delta or 0),
                    "quantity_before": int(row.quantity_before or 0),
                    "quantity_after": int(row.quantity_after or 0),
                    "unit_cost": float(row.unit_cost) if row.unit_cost is not None else None,
                    "reference_type": str(row.reference_type or "").strip() or None,
                    "reference_id": int(row.reference_id) if row.reference_id is not None else None,
                    "notes": str(row.notes or "").strip(),
                }
            )
        return output

    def report_orders_rows(
        self,
        *,
        start_dt: datetime,
        end_dt: datetime,
    ) -> list[dict]:
        item_count_sq = (
            select(
                OrderItem.order_id.label("order_id"),
                func.count(OrderItem.id).label("item_count"),
            )
            .group_by(OrderItem.order_id)
            .subquery()
        )
        query = (
            select(
                Order.id.label("order_id"),
                Order.sold_at.label("sold_at"),
                Order.marketplace.label("marketplace"),
                Order.external_order_id.label("external_order_id"),
                Order.order_status.label("status"),
                Order.subtotal_amount.label("subtotal_amount"),
                Order.fees.label("fees"),
                Order.shipping_cost.label("shipping_cost"),
                Order.shipping_label_cost.label("shipping_label_cost"),
                Order.shipping_label_currency.label("shipping_label_currency"),
                Order.total_amount.label("total_amount"),
                func.coalesce(item_count_sq.c.item_count, 0).label("item_count"),
                Order.notes.label("notes"),
            )
            .select_from(Order)
            .join(item_count_sq, item_count_sq.c.order_id == Order.id, isouter=True)
            .where(Order.sold_at.between(start_dt, end_dt))
            .order_by(Order.sold_at.desc(), Order.id.desc())
        )
        rows = self.db.execute(query).all()
        order_ids = sorted({int(row.order_id) for row in rows if row.order_id is not None})
        normalized_fee_by_order: dict[int, float] = {}
        normalized_label_by_order: dict[int, float] = {}
        if order_ids:
            normalized_fee_by_order = {
                int(order_id): float(total or 0)
                for order_id, total in self.db.execute(
                    select(
                        OrderFinanceEntry.order_id,
                        func.coalesce(func.sum(func.coalesce(OrderFinanceEntry.amount, 0)), 0),
                    )
                    .where(
                        OrderFinanceEntry.order_id.in_(order_ids),
                        OrderFinanceEntry.entry_kind == "marketplace_fee",
                    )
                    .group_by(OrderFinanceEntry.order_id)
                ).all()
            }
            normalized_label_by_order = {
                int(order_id): float(total or 0)
                for order_id, total in self.db.execute(
                    select(
                        OrderFinanceEntry.order_id,
                        func.coalesce(func.sum(func.coalesce(OrderFinanceEntry.amount, 0)), 0),
                    )
                    .where(
                        OrderFinanceEntry.order_id.in_(order_ids),
                        OrderFinanceEntry.entry_kind == "shipping_label",
                    )
                    .group_by(OrderFinanceEntry.order_id)
                ).all()
            }
        output: list[dict] = []
        for row in rows:
            order_id = int(row.order_id or 0)
            field_fees = float(row.fees or 0)
            shipping_cost = float(row.shipping_cost or 0)
            label_cost = float(row.shipping_label_cost or 0)
            actual_fee = normalized_fee_by_order.get(order_id, field_fees)
            actual_label_cost = normalized_label_by_order.get(order_id, label_cost)
            actual_net_before_cogs = float(row.subtotal_amount or 0) + shipping_cost - actual_fee - actual_label_cost
            output.append(
                {
                    "order_id": order_id,
                    "sold_at": row.sold_at.isoformat() if row.sold_at else None,
                    "marketplace": str(row.marketplace or "").strip(),
                    "external_order_id": str(row.external_order_id or "").strip(),
                    "status": str(row.status or "").strip(),
                    "subtotal_amount": float(row.subtotal_amount or 0),
                    "fees": field_fees,
                    "field_fees": field_fees,
                    "shipping_cost": shipping_cost,
                    "shipping_label_cost": label_cost,
                    "field_shipping_label_cost": label_cost,
                    "shipping_label_currency": str(row.shipping_label_currency or "").strip(),
                    "shipping_delta_charged_minus_actual": round(shipping_cost - label_cost, 2),
                    "actual_fee": round(actual_fee, 2),
                    "actual_shipping_label_cost": round(actual_label_cost, 2),
                    "actual_shipping_delta_charged_minus_label": round(shipping_cost - actual_label_cost, 2),
                    "actual_net_before_cogs": round(actual_net_before_cogs, 2),
                    "actual_fee_source": (
                        "normalized_order_finance_entries_marketplace_fee_sum"
                        if order_id in normalized_fee_by_order
                        else "order_fees_field"
                    ),
                    "actual_shipping_source": (
                        "normalized_order_finance_entries_shipping_label_sum"
                        if order_id in normalized_label_by_order
                        else "order_shipping_label_field"
                    ),
                    "total_amount": float(row.total_amount or 0),
                    "item_count": int(row.item_count or 0),
                    "notes": str(row.notes or "").strip(),
                }
            )
        return output

    def report_order_items_rows(
        self,
        *,
        start_dt: datetime,
        end_dt: datetime,
    ) -> list[dict]:
        query = (
            select(
                OrderItem.id.label("order_item_id"),
                OrderItem.order_id.label("order_id"),
                Order.sold_at.label("sold_at"),
                Order.marketplace.label("marketplace"),
                Order.external_order_id.label("external_order_id"),
                OrderItem.product_id.label("product_id"),
                OrderItem.listing_id.label("listing_id"),
                Product.sku.label("sku"),
                Product.title.label("product_title"),
                OrderItem.quantity.label("quantity"),
                OrderItem.unit_price.label("unit_price"),
                OrderItem.line_total.label("line_total"),
                OrderItem.line_fees.label("line_fees"),
                OrderItem.line_shipping.label("line_shipping"),
                OrderItem.notes.label("notes"),
            )
            .select_from(OrderItem)
            .join(Order, Order.id == OrderItem.order_id)
            .join(Product, Product.id == OrderItem.product_id, isouter=True)
            .where(Order.sold_at.between(start_dt, end_dt))
            .order_by(Order.sold_at.desc(), OrderItem.id.desc())
        )
        rows = self.db.execute(query).all()
        output: list[dict] = []
        for row in rows:
            output.append(
                {
                    "order_item_id": int(row.order_item_id or 0),
                    "order_id": int(row.order_id or 0),
                    "sold_at": row.sold_at.isoformat() if row.sold_at else None,
                    "marketplace": str(row.marketplace or "").strip() or None,
                    "external_order_id": str(row.external_order_id or "").strip() or None,
                    "product_id": int(row.product_id) if row.product_id is not None else None,
                    "listing_id": int(row.listing_id) if row.listing_id is not None else None,
                    "sku": str(row.sku or "").strip() or None,
                    "product_title": str(row.product_title or "").strip() or None,
                    "quantity": int(row.quantity or 0),
                    "unit_price": float(row.unit_price or 0),
                    "line_total": float(row.line_total or 0),
                    "line_fees": float(row.line_fees or 0),
                    "line_shipping": float(row.line_shipping or 0),
                    "notes": str(row.notes or "").strip(),
                }
            )
        return output

    def report_products_rows(
        self,
        *,
        start_dt: datetime,
        end_dt: datetime,
    ) -> list[dict]:
        query = (
            select(
                Product.id.label("product_id"),
                Product.sku.label("sku"),
                Product.title.label("title"),
                Product.description.label("description"),
                Product.category.label("category"),
                Product.metal_type.label("metal_type"),
                Product.current_quantity.label("current_quantity"),
                Product.acquisition_cost.label("acquisition_cost"),
                Product.product_cost.label("product_cost"),
                Product.acquired_at.label("acquired_at"),
                Product.acquisition_tax_paid.label("acquisition_tax_paid"),
                Product.acquisition_shipping_paid.label("acquisition_shipping_paid"),
                Product.acquisition_handling_paid.label("acquisition_handling_paid"),
                Product.weight_oz.label("weight_oz"),
                Product.package_weight_oz.label("package_weight_oz"),
                Product.package_length_in.label("package_length_in"),
                Product.package_width_in.label("package_width_in"),
                Product.package_height_in.label("package_height_in"),
            )
            .select_from(Product)
            .where(
                or_(
                    Product.acquired_at.between(start_dt, end_dt),
                    and_(
                        Product.acquired_at.is_(None),
                        Product.created_at.between(start_dt, end_dt),
                    ),
                )
            )
            .order_by(Product.acquired_at.desc(), Product.id.desc())
        )
        rows = self.db.execute(query).all()
        output: list[dict] = []
        for row in rows:
            output.append(
                {
                    "product_id": int(row.product_id or 0),
                    "sku": str(row.sku or "").strip() or None,
                    "title": str(row.title or "").strip() or None,
                    "description": str(row.description or "").strip(),
                    "category": str(row.category or "").strip(),
                    "metal_type": str(row.metal_type or "").strip(),
                    "current_quantity": int(row.current_quantity or 0),
                    "acquisition_cost": float(row.acquisition_cost) if row.acquisition_cost is not None else None,
                    "product_cost": float(row.product_cost) if row.product_cost is not None else None,
                    "acquired_at": row.acquired_at.isoformat() if row.acquired_at else None,
                    "acquisition_tax_paid": (
                        float(row.acquisition_tax_paid) if row.acquisition_tax_paid is not None else None
                    ),
                    "acquisition_shipping_paid": (
                        float(row.acquisition_shipping_paid)
                        if row.acquisition_shipping_paid is not None
                        else None
                    ),
                    "acquisition_handling_paid": (
                        float(row.acquisition_handling_paid)
                        if row.acquisition_handling_paid is not None
                        else None
                    ),
                    "weight_oz": float(row.weight_oz) if row.weight_oz is not None else None,
                    "package_weight_oz": (
                        float(row.package_weight_oz) if row.package_weight_oz is not None else None
                    ),
                    "package_length_in": float(row.package_length_in) if row.package_length_in is not None else None,
                    "package_width_in": float(row.package_width_in) if row.package_width_in is not None else None,
                    "package_height_in": float(row.package_height_in) if row.package_height_in is not None else None,
                }
            )
        return output

    def report_listings_rows(
        self,
        *,
        start_dt: datetime,
        end_dt: datetime,
    ) -> list[dict]:
        query = (
            select(
                MarketplaceListing.id.label("listing_id"),
                MarketplaceListing.listed_at.label("listed_at"),
                MarketplaceListing.marketplace.label("marketplace"),
                MarketplaceListing.product_id.label("product_id"),
                Product.sku.label("sku"),
                MarketplaceListing.listing_title.label("listing_title"),
                MarketplaceListing.listing_status.label("listing_status"),
                MarketplaceListing.marketplace_url.label("marketplace_url"),
                MarketplaceListing.marketplace_details.label("marketplace_details"),
                MarketplaceListing.quantity_listed.label("quantity_listed"),
                MarketplaceListing.listing_price.label("listing_price"),
                MarketplaceListing.external_listing_id.label("external_listing_id"),
            )
            .select_from(MarketplaceListing)
            .join(Product, Product.id == MarketplaceListing.product_id, isouter=True)
            .where(
                or_(
                    MarketplaceListing.listed_at.between(start_dt, end_dt),
                    and_(
                        MarketplaceListing.listed_at.is_(None),
                        MarketplaceListing.created_at.between(start_dt, end_dt),
                    ),
                )
            )
            .order_by(MarketplaceListing.listed_at.desc(), MarketplaceListing.id.desc())
        )
        rows = self.db.execute(query).all()
        output: list[dict] = []
        for row in rows:
            output.append(
                {
                    "listing_id": int(row.listing_id or 0),
                    "listed_at": row.listed_at.isoformat() if row.listed_at else None,
                    "marketplace": str(row.marketplace or "").strip() or None,
                    "product_id": int(row.product_id) if row.product_id is not None else None,
                    "sku": str(row.sku or "").strip() or None,
                    "listing_title": str(row.listing_title or "").strip() or None,
                    "listing_status": str(row.listing_status or "").strip() or None,
                    "marketplace_url": str(row.marketplace_url or "").strip(),
                    "marketplace_details": str(row.marketplace_details or "").strip(),
                    "quantity_listed": int(row.quantity_listed or 0),
                    "listing_price": float(row.listing_price or 0),
                    "external_listing_id": str(row.external_listing_id or "").strip(),
                }
            )
        return output

    def report_sales_rows(
        self,
        *,
        start_dt: datetime,
        end_dt: datetime,
    ) -> list[dict]:
        ListingProduct = aliased(Product)
        resolved_product_id_expr = func.coalesce(Sale.product_id, MarketplaceListing.product_id)
        query = (
            select(
                Sale.id.label("sale_id"),
                Sale.sold_at.label("sold_at"),
                Sale.marketplace.label("marketplace"),
                Sale.order_id.label("order_id"),
                resolved_product_id_expr.label("product_id"),
                Sale.product_id.label("sale_product_id"),
                MarketplaceListing.product_id.label("listing_product_id"),
                func.coalesce(Product.sku, ListingProduct.sku).label("sku"),
                func.coalesce(Product.title, ListingProduct.title).label("product_title"),
                func.coalesce(Product.acquisition_cost, ListingProduct.acquisition_cost).label("product_acquisition_cost"),
                func.coalesce(Product.product_cost, ListingProduct.product_cost).label("product_cost"),
                Sale.listing_id.label("listing_id"),
                Sale.external_order_id.label("external_order_id"),
                Sale.quantity_sold.label("quantity_sold"),
                Sale.sold_price.label("sold_price"),
                Sale.fees.label("fees"),
                Sale.shipping_cost.label("shipping_cost"),
                Sale.shipping_provider.label("shipping_provider"),
                Sale.shipping_service.label("shipping_service"),
                Sale.shipping_package_type.label("shipping_package_type"),
                Sale.tracking_number.label("tracking_number"),
                Sale.tracking_status.label("tracking_status"),
                Sale.shipping_exception_code.label("shipping_exception_code"),
                Sale.shipping_exception_action.label("shipping_exception_action"),
                Sale.shipping_exception_notes.label("shipping_exception_notes"),
                Sale.shipping_exception_resolved_at.label("shipping_exception_resolved_at"),
                Sale.shipping_exception_resolved_by.label("shipping_exception_resolved_by"),
                Sale.shipment_exported_at.label("shipment_exported_at"),
                Sale.shipped_at.label("shipped_at"),
                Sale.delivered_at.label("delivered_at"),
                Sale.shipping_label_cost.label("shipping_label_cost"),
                MarketplaceListing.marketplace_details.label("listing_marketplace_details"),
            )
            .select_from(Sale)
            .join(Product, Product.id == Sale.product_id, isouter=True)
            .join(MarketplaceListing, MarketplaceListing.id == Sale.listing_id, isouter=True)
            .join(ListingProduct, ListingProduct.id == MarketplaceListing.product_id, isouter=True)
            .where(Sale.sold_at.between(start_dt, end_dt))
            .order_by(Sale.sold_at.desc(), Sale.id.desc())
        )
        rows = self.db.execute(query).all()
        actual_econ_by_sale_id = {
            int(actual_row.get("sale_id") or 0): actual_row
            for actual_row in self.report_sales_actual_econ_rows(start_dt=start_dt, end_dt=end_dt)
            if int(actual_row.get("sale_id") or 0) > 0
        }
        output: list[dict] = []
        for row in rows:
            actual = actual_econ_by_sale_id.get(int(row.sale_id or 0)) or {}
            bundle_payload = self._listing_bundle_payload_from_raw(row.listing_marketplace_details)
            bundle_components = self._bundle_components_from_payload(bundle_payload, int(row.quantity_sold or 0))
            bundle_units_per_listing = sum(
                max(1, int(component.get("quantity_per_listing") or 1))
                for component in bundle_components
            )
            bundle_inventory_units_sold = sum(
                max(1, int(component.get("quantity_total") or 1))
                for component in bundle_components
            )
            field_net = (
                float(row.sold_price or 0)
                + float(row.shipping_cost or 0)
                - float(row.fees or 0)
                - float(row.shipping_label_cost or 0)
            )
            output.append(
                {
                    "sale_id": int(row.sale_id or 0),
                    "sold_at": row.sold_at.isoformat() if row.sold_at else None,
                    "marketplace": str(row.marketplace or "").strip() or None,
                    "order_id": int(row.order_id) if row.order_id is not None else None,
                    "product_id": int(row.product_id) if row.product_id is not None else None,
                    "sku": str(row.sku or "").strip() or None,
                    "product_title": str(row.product_title or "").strip() or None,
                    "product_acquisition_cost": (
                        float(row.product_acquisition_cost)
                        if row.product_acquisition_cost is not None
                        else None
                    ),
                    "product_cost": float(row.product_cost) if row.product_cost is not None else None,
                    "listing_id": int(row.listing_id) if row.listing_id is not None else None,
                    "listing_is_bundle": bool(bundle_components),
                    "listing_bundle_kind": str(bundle_payload.get("kind") or "").strip(),
                    "listing_bundle_component_count": len(bundle_components),
                    "listing_bundle_units_per_listing": int(bundle_units_per_listing or 0),
                    "listing_bundle_inventory_units_sold": int(bundle_inventory_units_sold or 0),
                    "external_order_id": str(row.external_order_id or "").strip(),
                    "quantity_sold": int(row.quantity_sold or 0),
                    "sold_price": float(row.sold_price or 0),
                    "fees": float(row.fees or 0),
                    "shipping_cost": float(row.shipping_cost or 0),
                    "field_net_before_cogs": round(field_net, 2),
                    "actual_fee": round(
                        self._safe_float(actual.get("allocated_fee_actual", float(row.fees or 0))),
                        2,
                    ),
                    "actual_shipping_charged": round(
                        self._safe_float(actual.get("allocated_shipping_charged", float(row.shipping_cost or 0))),
                        2,
                    ),
                    "actual_shipping_label_cost": round(
                        self._safe_float(
                            actual.get("allocated_shipping_actual", float(row.shipping_label_cost or 0))
                        ),
                        2,
                    ),
                    "actual_net_before_cogs": round(
                        self._safe_float(actual.get("net_before_cogs_actual", field_net)),
                        2,
                    ),
                    "actual_fee_source": str(actual.get("actual_fee_source") or "sale_fees_field"),
                    "actual_shipping_source": str(actual.get("actual_shipping_source") or "sale_shipping_label_field"),
                    "shipping_provider": str(row.shipping_provider or "").strip(),
                    "shipping_service": str(row.shipping_service or "").strip(),
                    "shipping_package_type": str(row.shipping_package_type or "").strip(),
                    "tracking_number": str(row.tracking_number or "").strip(),
                    "tracking_status": str(row.tracking_status or "").strip(),
                    "shipping_exception_code": str(row.shipping_exception_code or "").strip(),
                    "shipping_exception_action": str(row.shipping_exception_action or "").strip(),
                    "shipping_exception_notes": str(row.shipping_exception_notes or "").strip(),
                    "shipping_exception_resolved_at": (
                        row.shipping_exception_resolved_at.isoformat()
                        if row.shipping_exception_resolved_at
                        else None
                    ),
                    "shipping_exception_resolved_by": str(row.shipping_exception_resolved_by or "").strip(),
                    "shipment_exported_at": row.shipment_exported_at.isoformat() if row.shipment_exported_at else None,
                    "shipped_at": row.shipped_at.isoformat() if row.shipped_at else None,
                    "delivered_at": row.delivered_at.isoformat() if row.delivered_at else None,
                    "shipping_label_cost": (
                        float(row.shipping_label_cost)
                        if row.shipping_label_cost is not None
                        else None
                    ),
                }
            )
        return output

    def report_returns_rows(
        self,
        *,
        start_dt: datetime,
        end_dt: datetime,
    ) -> list[dict]:
        ReturnProduct = aliased(Product)
        SaleProduct = aliased(Product)
        ListingProduct = aliased(Product)
        query = (
            select(
                ReturnRecord.id.label("return_id"),
                ReturnRecord.returned_at.label("returned_at"),
                ReturnRecord.processed_at.label("processed_at"),
                ReturnRecord.marketplace.label("marketplace"),
                ReturnRecord.external_return_id.label("external_return_id"),
                ReturnRecord.sale_id.label("sale_id"),
                ReturnRecord.order_id.label("order_id"),
                func.coalesce(ReturnRecord.product_id, Sale.product_id, MarketplaceListing.product_id).label("product_id"),
                ReturnRecord.product_id.label("return_product_id"),
                Sale.product_id.label("sale_product_id"),
                MarketplaceListing.product_id.label("listing_product_id"),
                func.coalesce(ReturnProduct.sku, SaleProduct.sku, ListingProduct.sku).label("sku"),
                func.coalesce(ReturnProduct.title, SaleProduct.title, ListingProduct.title).label("product_title"),
                ReturnRecord.return_status.label("status"),
                ReturnRecord.reason.label("reason"),
                ReturnRecord.disposition.label("disposition"),
                ReturnRecord.quantity.label("quantity"),
                ReturnRecord.refund_amount.label("refund_amount"),
                ReturnRecord.refund_fees.label("refund_fees"),
                ReturnRecord.refund_shipping.label("refund_shipping"),
                ReturnRecord.restocked.label("restocked"),
                ReturnRecord.notes.label("notes"),
                Sale.external_order_id.label("source_order"),
                MarketplaceListing.marketplace_details.label("listing_marketplace_details"),
            )
            .select_from(ReturnRecord)
            .join(Sale, Sale.id == ReturnRecord.sale_id, isouter=True)
            .join(MarketplaceListing, MarketplaceListing.id == Sale.listing_id, isouter=True)
            .join(ReturnProduct, ReturnProduct.id == ReturnRecord.product_id, isouter=True)
            .join(SaleProduct, SaleProduct.id == Sale.product_id, isouter=True)
            .join(ListingProduct, ListingProduct.id == MarketplaceListing.product_id, isouter=True)
            .where(ReturnRecord.returned_at.between(start_dt, end_dt))
            .order_by(ReturnRecord.returned_at.desc(), ReturnRecord.id.desc())
        )
        rows = self.db.execute(query).all()
        output: list[dict] = []
        for row in rows:
            bundle_payload = self._listing_bundle_payload_from_raw(row.listing_marketplace_details)
            bundle_components = self._bundle_components_from_payload(bundle_payload, int(row.quantity or 0))
            bundle_units_per_return = sum(
                max(1, int(component.get("quantity_per_listing") or 1))
                for component in bundle_components
            )
            bundle_inventory_units_returned = sum(
                max(1, int(component.get("quantity_total") or 1))
                for component in bundle_components
            )
            output.append(
                {
                    "return_id": int(row.return_id or 0),
                    "returned_at": row.returned_at.isoformat() if row.returned_at else None,
                    "processed_at": row.processed_at.isoformat() if row.processed_at else None,
                    "marketplace": str(row.marketplace or "").strip(),
                    "external_return_id": str(row.external_return_id or "").strip(),
                    "sale_id": int(row.sale_id) if row.sale_id is not None else None,
                    "order_id": int(row.order_id) if row.order_id is not None else None,
                    "product_id": int(row.product_id) if row.product_id is not None else None,
                    "sku": str(row.sku or "").strip() or None,
                    "product_title": str(row.product_title or "").strip() or None,
                    "status": str(row.status or "").strip(),
                    "reason": str(row.reason or "").strip(),
                    "disposition": str(row.disposition or "").strip(),
                    "quantity": int(row.quantity or 0),
                    "refund_amount": float(row.refund_amount or 0),
                    "refund_fees": float(row.refund_fees or 0),
                    "refund_shipping": float(row.refund_shipping or 0),
                    "restocked": bool(row.restocked),
                    "listing_is_bundle": bool(bundle_components),
                    "listing_bundle_kind": str(bundle_payload.get("kind") or "").strip(),
                    "listing_bundle_component_count": len(bundle_components),
                    "listing_bundle_units_per_return": int(bundle_units_per_return or 0),
                    "listing_bundle_inventory_units_returned": int(bundle_inventory_units_returned or 0),
                    "notes": str(row.notes or "").strip(),
                    "source_order": str(row.source_order or "").strip(),
                }
            )
        return output

    def report_lot_assignment_rows(
        self,
        *,
        start_dt: datetime,
        end_dt: datetime,
    ) -> list[dict]:
        query = (
            select(
                ProductLotAssignment.id.label("assignment_id"),
                PurchaseLot.lot_code.label("lot_code"),
                InventorySource.name.label("source_name"),
                InventorySource.source_type.label("source_type"),
                PurchaseLot.vendor.label("vendor"),
                PurchaseLot.purchase_date.label("purchase_date"),
                PurchaseLot.total_cost.label("lot_total_cost"),
                PurchaseLot.total_tax_paid.label("lot_total_tax_paid"),
                PurchaseLot.total_shipping_paid.label("lot_total_shipping_paid"),
                PurchaseLot.total_handling_paid.label("lot_total_handling_paid"),
                PurchaseLot.expected_total_quantity.label("lot_expected_total_quantity"),
                Product.sku.label("sku"),
                Product.title.label("product_title"),
                ProductLotAssignment.lot_id.label("lot_id"),
                ProductLotAssignment.quantity_acquired.label("quantity_acquired"),
                ProductLotAssignment.unit_cost.label("unit_cost"),
                ProductLotAssignment.unit_tax_paid.label("unit_tax_paid"),
                ProductLotAssignment.unit_shipping_paid.label("unit_shipping_paid"),
                ProductLotAssignment.unit_handling_paid.label("unit_handling_paid"),
                ProductLotAssignment.allocated_cost.label("allocated_cost"),
                ProductLotAssignment.allocated_tax_paid.label("allocated_tax_paid"),
                ProductLotAssignment.allocated_shipping_paid.label("allocated_shipping_paid"),
                ProductLotAssignment.allocated_handling_paid.label("allocated_handling_paid"),
                ProductLotAssignment.allocation_weight.label("allocation_weight"),
                ProductLotAssignment.acquired_at.label("acquired_at"),
            )
            .select_from(ProductLotAssignment)
            .join(PurchaseLot, PurchaseLot.id == ProductLotAssignment.lot_id, isouter=True)
            .join(InventorySource, InventorySource.id == PurchaseLot.source_id, isouter=True)
            .join(Product, Product.id == ProductLotAssignment.product_id, isouter=True)
            .where(
                or_(
                    ProductLotAssignment.acquired_at.between(start_dt, end_dt),
                    and_(
                        ProductLotAssignment.acquired_at.is_(None),
                        ProductLotAssignment.created_at.between(start_dt, end_dt),
                    ),
                )
            )
            .order_by(ProductLotAssignment.acquired_at.desc(), ProductLotAssignment.id.desc())
        )
        rows = self.db.execute(query).all()
        lot_fallback_unit_costs, assignment_fallback_unit_costs = self._lot_fallback_unit_cost_maps_from_rows(
            list(rows)
        )
        output: list[dict] = []
        for row in rows:
            resolved_unit_cost = self._landed_unit_cost_from_assignment_row(
                row,
                lot_fallback_unit_costs=lot_fallback_unit_costs,
                assignment_fallback_unit_costs=assignment_fallback_unit_costs,
            )
            explicit_unit_cost = self._explicit_landed_unit_cost_from_assignment_row(row)
            assignment_id = int(row.assignment_id or 0)
            lot_id = int(row.lot_id or 0)
            if explicit_unit_cost > 0 and row.unit_cost is not None:
                cost_source = "assignment_unit_landed_cost"
            elif explicit_unit_cost > 0:
                cost_source = "assignment_allocated_landed_cost"
            elif assignment_id in assignment_fallback_unit_costs:
                cost_source = "lot_allocation_weight"
            elif lot_id in lot_fallback_unit_costs and int(row.lot_expected_total_quantity or 0) > 0:
                cost_source = "lot_expected_quantity_fallback"
            elif lot_id in lot_fallback_unit_costs:
                cost_source = "lot_equal_quantity_fallback"
            else:
                cost_source = "missing_cost_basis"
            output.append(
                {
                    "assignment_id": int(row.assignment_id or 0),
                    "lot_id": int(row.lot_id or 0),
                    "lot_code": str(row.lot_code or "").strip() or None,
                    "source_name": str(row.source_name or "").strip() or None,
                    "source_type": str(row.source_type or "").strip() or None,
                    "vendor": str(row.vendor or "").strip() or None,
                    "purchase_date": row.purchase_date.isoformat() if row.purchase_date else None,
                    "lot_landed_total": round(self._lot_landed_total_from_assignment_row(row), 2),
                    "lot_expected_total_quantity": (
                        int(row.lot_expected_total_quantity)
                        if row.lot_expected_total_quantity is not None
                        else None
                    ),
                    "sku": str(row.sku or "").strip() or None,
                    "product_title": str(row.product_title or "").strip() or None,
                    "quantity_acquired": int(row.quantity_acquired or 0),
                    "unit_cost": float(row.unit_cost) if row.unit_cost is not None else None,
                    "unit_tax_paid": float(row.unit_tax_paid) if row.unit_tax_paid is not None else None,
                    "unit_shipping_paid": float(row.unit_shipping_paid) if row.unit_shipping_paid is not None else None,
                    "unit_handling_paid": float(row.unit_handling_paid) if row.unit_handling_paid is not None else None,
                    "allocated_cost": float(row.allocated_cost) if row.allocated_cost is not None else None,
                    "allocated_tax_paid": float(row.allocated_tax_paid) if row.allocated_tax_paid is not None else None,
                    "allocated_shipping_paid": float(row.allocated_shipping_paid) if row.allocated_shipping_paid is not None else None,
                    "allocated_handling_paid": float(row.allocated_handling_paid) if row.allocated_handling_paid is not None else None,
                    "allocation_weight": float(row.allocation_weight) if row.allocation_weight is not None else None,
                    "resolved_landed_unit_cost": round(float(resolved_unit_cost), 4),
                    "resolved_landed_total_cost": round(
                        float(resolved_unit_cost) * float(max(0, int(row.quantity_acquired or 0))),
                        2,
                    ),
                    "cost_source": cost_source,
                    "acquired_at": row.acquired_at.isoformat() if row.acquired_at else None,
                }
            )
        return output

    def lot_profitability_snapshot(self, lot_id: int) -> dict[str, Any]:
        lot = self.db.get(PurchaseLot, int(lot_id))
        if lot is None:
            raise ValueError(f"Purchase lot {int(lot_id)} not found.")
        assignment_rows = self.db.execute(
            select(
                ProductLotAssignment.id.label("assignment_id"),
                ProductLotAssignment.lot_id.label("lot_id"),
                ProductLotAssignment.product_id.label("product_id"),
                ProductLotAssignment.quantity_acquired.label("quantity_acquired"),
                ProductLotAssignment.allocated_cost.label("allocated_cost"),
                ProductLotAssignment.allocated_tax_paid.label("allocated_tax_paid"),
                ProductLotAssignment.allocated_shipping_paid.label("allocated_shipping_paid"),
                ProductLotAssignment.allocated_handling_paid.label("allocated_handling_paid"),
                ProductLotAssignment.allocation_weight.label("allocation_weight"),
                ProductLotAssignment.unit_cost.label("unit_cost"),
                ProductLotAssignment.unit_tax_paid.label("unit_tax_paid"),
                ProductLotAssignment.unit_shipping_paid.label("unit_shipping_paid"),
                ProductLotAssignment.unit_handling_paid.label("unit_handling_paid"),
                ProductLotAssignment.acquired_at.label("acquired_at"),
                PurchaseLot.total_cost.label("lot_total_cost"),
                PurchaseLot.total_tax_paid.label("lot_total_tax_paid"),
                PurchaseLot.total_shipping_paid.label("lot_total_shipping_paid"),
                PurchaseLot.total_handling_paid.label("lot_total_handling_paid"),
                PurchaseLot.expected_total_quantity.label("lot_expected_total_quantity"),
                Product.sku.label("sku"),
                Product.title.label("product_title"),
            )
            .select_from(ProductLotAssignment)
            .join(PurchaseLot, PurchaseLot.id == ProductLotAssignment.lot_id, isouter=True)
            .join(Product, Product.id == ProductLotAssignment.product_id, isouter=True)
            .where(ProductLotAssignment.lot_id == int(lot_id))
        ).all()
        if not assignment_rows:
            return {
                "lot_id": int(lot.id),
                "lot_code": str(lot.lot_code or "").strip(),
                "vendor": str(lot.vendor or "").strip(),
                "purchase_date": lot.purchase_date.isoformat() if lot.purchase_date else None,
                "summary": {
                    "assigned_products": 0,
                    "assigned_qty": 0,
                    "allocated_landed_cost": 0.0,
                    "estimated_gross_sales": 0.0,
                    "estimated_net_before_cogs": 0.0,
                    "estimated_lot_cogs": 0.0,
                    "estimated_lot_profit": 0.0,
                },
                "rows": [],
            }

        lot_fallback_unit_costs, assignment_fallback_unit_costs = self._lot_fallback_unit_cost_maps_from_rows(
            list(assignment_rows)
        )

        product_rollup: dict[int, dict[str, Any]] = {}
        for row in assignment_rows:
            product_id = int(row.product_id or 0)
            if product_id <= 0:
                continue
            qty = max(0, int(row.quantity_acquired or 0))
            unit_landed = self._landed_unit_cost_from_assignment_row(
                row,
                lot_fallback_unit_costs=lot_fallback_unit_costs,
                assignment_fallback_unit_costs=assignment_fallback_unit_costs,
            )
            explicit_unit_cost = self._explicit_landed_unit_cost_from_assignment_row(row)
            assignment_id = int(row.assignment_id or 0)
            row_lot_id = int(row.lot_id or 0)
            if explicit_unit_cost > 0 and row.unit_cost is not None:
                cost_source = "assignment_unit_landed_cost"
            elif explicit_unit_cost > 0:
                cost_source = "assignment_allocated_landed_cost"
            elif assignment_id in assignment_fallback_unit_costs:
                cost_source = "lot_allocation_weight"
            elif row_lot_id in lot_fallback_unit_costs and int(row.lot_expected_total_quantity or 0) > 0:
                cost_source = "lot_expected_quantity_fallback"
            elif row_lot_id in lot_fallback_unit_costs:
                cost_source = "lot_equal_quantity_fallback"
            else:
                cost_source = "missing_cost_basis"
            allocated_landed = unit_landed * float(qty)
            current = product_rollup.setdefault(
                product_id,
                {
                    "product_id": product_id,
                    "sku": str(row.sku or "").strip() or None,
                    "product_title": str(row.product_title or "").strip() or None,
                    "assigned_qty": 0,
                    "allocated_landed_cost": 0.0,
                    "unit_landed_cost": 0.0,
                    "cost_source_totals": {},
                },
            )
            current["assigned_qty"] += int(qty)
            current["allocated_landed_cost"] += float(allocated_landed or 0.0)
            source_totals = current.setdefault("cost_source_totals", {})
            source_totals[cost_source] = float(source_totals.get(cost_source, 0.0) or 0.0) + float(
                allocated_landed or 0.0
            )
            if current["assigned_qty"] > 0:
                current["unit_landed_cost"] = (
                    float(current["allocated_landed_cost"]) / float(current["assigned_qty"])
                )
            elif unit_landed > 0:
                current["unit_landed_cost"] = float(unit_landed)

        product_ids = [int(pid) for pid in product_rollup.keys()]
        sale_rows = []
        if product_ids:
            sale_rows = self.db.execute(
                select(
                    Sale.id.label("sale_id"),
                    Sale.product_id.label("product_id"),
                    Sale.sold_at.label("sold_at"),
                    func.coalesce(Sale.quantity_sold, 0).label("qty_sold"),
                    func.coalesce(Sale.sold_price, 0).label("gross_sales"),
                    func.coalesce(Sale.fees, 0).label("fees"),
                    func.coalesce(Sale.shipping_cost, 0).label("shipping_cost"),
                    func.coalesce(Sale.shipping_label_cost, 0).label("shipping_label_cost"),
                    MarketplaceListing.marketplace_details.label("listing_marketplace_details"),
                )
                .select_from(Sale)
                .outerjoin(MarketplaceListing, MarketplaceListing.id == Sale.listing_id)
                .where(
                    or_(
                        Sale.product_id.in_(product_ids),
                        MarketplaceListing.marketplace_details.is_not(None),
                    )
                )
                .order_by(Sale.sold_at.asc(), Sale.id.asc())
            ).all()
        actual_econ_by_sale_id: dict[int, dict[str, Any]] = {}
        sale_dates = [row.sold_at for row in sale_rows if row.sold_at is not None]
        if sale_dates:
            actual_econ_by_sale_id = {
                int(actual_row.get("sale_id") or 0): actual_row
                for actual_row in self.report_sales_actual_econ_rows(
                    start_dt=min(sale_dates),
                    end_dt=max(sale_dates),
                )
                if int(actual_row.get("sale_id") or 0) > 0
            }
        all_assignment_rows = []
        if product_ids:
            all_assignment_rows = self.db.execute(
                select(
                    ProductLotAssignment.id.label("assignment_id"),
                    ProductLotAssignment.lot_id.label("lot_id"),
                    ProductLotAssignment.product_id.label("product_id"),
                    ProductLotAssignment.quantity_acquired.label("quantity_acquired"),
                    ProductLotAssignment.allocated_cost.label("allocated_cost"),
                    ProductLotAssignment.allocated_tax_paid.label("allocated_tax_paid"),
                    ProductLotAssignment.allocated_shipping_paid.label("allocated_shipping_paid"),
                    ProductLotAssignment.allocated_handling_paid.label("allocated_handling_paid"),
                    ProductLotAssignment.allocation_weight.label("allocation_weight"),
                    ProductLotAssignment.unit_cost.label("unit_cost"),
                    ProductLotAssignment.unit_tax_paid.label("unit_tax_paid"),
                    ProductLotAssignment.unit_shipping_paid.label("unit_shipping_paid"),
                    ProductLotAssignment.unit_handling_paid.label("unit_handling_paid"),
                    ProductLotAssignment.acquired_at.label("acquired_at"),
                    PurchaseLot.total_cost.label("lot_total_cost"),
                    PurchaseLot.total_tax_paid.label("lot_total_tax_paid"),
                    PurchaseLot.total_shipping_paid.label("lot_total_shipping_paid"),
                    PurchaseLot.total_handling_paid.label("lot_total_handling_paid"),
                    PurchaseLot.expected_total_quantity.label("lot_expected_total_quantity"),
                )
                .select_from(ProductLotAssignment)
                .join(PurchaseLot, PurchaseLot.id == ProductLotAssignment.lot_id, isouter=True)
                .where(ProductLotAssignment.product_id.in_(product_ids))
            ).all()
        all_lot_fallback_unit_costs, all_assignment_fallback_unit_costs = (
            self._lot_fallback_unit_cost_maps_from_rows(list(all_assignment_rows))
        )
        lots_by_product: dict[int, list[dict[str, Any]]] = {}
        for assignment_row in all_assignment_rows:
            product_id = int(assignment_row.product_id or 0)
            assignment_qty = max(0, int(assignment_row.quantity_acquired or 0))
            if product_id <= 0 or assignment_qty <= 0:
                continue
            unit_landed = self._landed_unit_cost_from_assignment_row(
                assignment_row,
                lot_fallback_unit_costs=all_lot_fallback_unit_costs,
                assignment_fallback_unit_costs=all_assignment_fallback_unit_costs,
            )
            explicit_unit_cost = self._explicit_landed_unit_cost_from_assignment_row(assignment_row)
            assignment_id = int(assignment_row.assignment_id or 0)
            row_lot_id = int(assignment_row.lot_id or 0)
            if explicit_unit_cost > 0 and assignment_row.unit_cost is not None:
                cost_source = "assignment_unit_landed_cost"
            elif explicit_unit_cost > 0:
                cost_source = "assignment_allocated_landed_cost"
            elif assignment_id in all_assignment_fallback_unit_costs:
                cost_source = "lot_allocation_weight"
            elif row_lot_id in all_lot_fallback_unit_costs and int(assignment_row.lot_expected_total_quantity or 0) > 0:
                cost_source = "lot_expected_quantity_fallback"
            elif row_lot_id in all_lot_fallback_unit_costs:
                cost_source = "lot_equal_quantity_fallback"
            else:
                cost_source = "missing_cost_basis"
            lots_by_product.setdefault(product_id, []).append(
                {
                    "lot_id": row_lot_id,
                    "remaining_qty": int(assignment_qty),
                    "unit_cost": float(unit_landed),
                    "cost_source": cost_source,
                    "acquired_at": assignment_row.acquired_at,
                    "assignment_id": assignment_id,
                }
            )
        lot_sources = {
            product_id: sorted(
                rows,
                key=lambda item: (
                    item.get("acquired_at") or datetime.min,
                    int(item.get("assignment_id") or 0),
                ),
            )
            for product_id, rows in lots_by_product.items()
        }
        source_index_by_product = {product_id: 0 for product_id in lot_sources}
        from collections import deque

        queues = {product_id: deque() for product_id in lot_sources}

        def _queue_available_lots(product_id: int, cutoff: datetime) -> None:
            rows = lot_sources.get(product_id) or []
            idx = int(source_index_by_product.get(product_id, 0))
            queue = queues.setdefault(product_id, deque())
            while idx < len(rows):
                acquired_at = rows[idx].get("acquired_at") or datetime.min
                if acquired_at > cutoff:
                    break
                queue.append(dict(rows[idx]))
                idx += 1
            source_index_by_product[product_id] = idx

        sales_map: dict[int, dict[str, Any]] = {}
        sale_lot_allocations: dict[int, list[dict[str, Any]]] = {}
        for row in sale_rows:
            sale_id = int(row.sale_id or 0)
            actual = actual_econ_by_sale_id.get(sale_id) or {}
            gross_sales = self._safe_float(row.gross_sales)
            fees = self._safe_float(actual.get("allocated_fee_actual", row.fees))
            shipping_cost = self._safe_float(actual.get("allocated_shipping_charged", row.shipping_cost))
            shipping_label_cost = self._safe_float(
                actual.get("allocated_shipping_actual", row.shipping_label_cost)
            )
            net_before_cogs = self._safe_float(
                actual.get(
                    "net_before_cogs_actual",
                    gross_sales + shipping_cost - fees - shipping_label_cost,
                )
            )
            listing_qty = max(1, int(row.qty_sold or 1))
            component_events: list[dict[str, Any]] = []
            bundle_components = self._bundle_components_from_payload(
                self._listing_bundle_payload_from_raw(row.listing_marketplace_details),
                listing_qty,
            )
            if bundle_components:
                bundle_units_per_listing = sum(
                    max(1, int(component.get("quantity_per_listing") or 1))
                    for component in bundle_components
                )
                bundle_units_total = sum(
                    max(1, int(component.get("quantity_total") or 1))
                    for component in bundle_components
                )
                for component in bundle_components:
                    component_product_id = int(component.get("product_id") or 0)
                    if component_product_id not in product_ids:
                        continue
                    component_qty = max(1, int(component.get("quantity_total") or 1))
                    portion = float(component_qty) / float(max(1, bundle_units_total))
                    component_events.append(
                        {
                            "product_id": component_product_id,
                            "quantity": component_qty,
                            "quantity_per_listing": max(1, int(component.get("quantity_per_listing") or 1)),
                            "bundle_units_per_listing": max(1, int(bundle_units_per_listing or 1)),
                            "gross_sales": gross_sales * portion,
                            "fees": fees * portion,
                            "shipping_cost": shipping_cost * portion,
                            "shipping_label_cost": shipping_label_cost * portion,
                            "net_before_cogs": net_before_cogs * portion,
                        }
                    )
            else:
                product_id = int(row.product_id or 0)
                if product_id in product_ids:
                    component_events.append(
                        {
                            "product_id": product_id,
                            "quantity": listing_qty,
                            "quantity_per_listing": 1,
                            "bundle_units_per_listing": 1,
                            "gross_sales": gross_sales,
                            "fees": fees,
                            "shipping_cost": shipping_cost,
                            "shipping_label_cost": shipping_label_cost,
                            "net_before_cogs": net_before_cogs,
                        }
                    )
            for event in component_events:
                product_id = int(event.get("product_id") or 0)
                sale_qty = max(1, int(event.get("quantity") or 1))
                _queue_available_lots(product_id, row.sold_at or datetime.min)
                queue = queues.setdefault(product_id, deque())
                qty_remaining = sale_qty
                while qty_remaining > 0 and queue:
                    if int(queue[0].get("remaining_qty") or 0) <= 0:
                        queue.popleft()
                        continue
                    use_qty = min(qty_remaining, int(queue[0].get("remaining_qty") or 0))
                    consumed_lot_id = int(queue[0].get("lot_id") or 0)
                    if consumed_lot_id == int(lot_id):
                        portion = float(use_qty) / float(sale_qty)
                        bucket = sales_map.setdefault(
                            product_id,
                            {
                                "qty_sold": 0,
                                "gross_sales": 0.0,
                                "fees": 0.0,
                                "shipping_cost": 0.0,
                                "shipping_label_cost": 0.0,
                                "net_before_cogs": 0.0,
                                "lot_cogs": 0.0,
                                "cost_source_totals": {},
                            },
                        )
                        bucket["qty_sold"] = int(bucket.get("qty_sold") or 0) + int(use_qty)
                        bucket["gross_sales"] = float(bucket.get("gross_sales") or 0.0) + (
                            self._safe_float(event.get("gross_sales")) * portion
                        )
                        bucket["fees"] = float(bucket.get("fees") or 0.0) + (
                            self._safe_float(event.get("fees")) * portion
                        )
                        bucket["shipping_cost"] = float(bucket.get("shipping_cost") or 0.0) + (
                            self._safe_float(event.get("shipping_cost")) * portion
                        )
                        bucket["shipping_label_cost"] = float(bucket.get("shipping_label_cost") or 0.0) + (
                            self._safe_float(event.get("shipping_label_cost")) * portion
                        )
                        bucket["net_before_cogs"] = float(bucket.get("net_before_cogs") or 0.0) + (
                            self._safe_float(event.get("net_before_cogs")) * portion
                        )
                        consumed_cost = float(use_qty) * self._safe_float(queue[0].get("unit_cost"))
                        bucket["lot_cogs"] = float(bucket.get("lot_cogs") or 0.0) + consumed_cost
                        source = str(queue[0].get("cost_source") or "missing_cost_basis")
                        source_totals = bucket.setdefault("cost_source_totals", {})
                        source_totals[source] = float(source_totals.get(source, 0.0) or 0.0) + consumed_cost
                        if sale_id > 0:
                            sale_lot_allocations.setdefault(sale_id, []).append(
                                {
                                    "product_id": product_id,
                                    "quantity": int(use_qty),
                                    "quantity_per_listing": max(1, int(event.get("quantity_per_listing") or 1)),
                                    "bundle_units_per_listing": max(
                                        1,
                                        int(event.get("bundle_units_per_listing") or 1),
                                    ),
                                    "unit_cost": self._safe_float(queue[0].get("unit_cost")),
                                }
                            )
                    queue[0]["remaining_qty"] = int(queue[0].get("remaining_qty") or 0) - use_qty
                    qty_remaining -= use_qty
                    if int(queue[0].get("remaining_qty") or 0) <= 0:
                        queue.popleft()

        return_adjustments_by_product: dict[int, dict[str, float]] = {}
        if sale_lot_allocations:
            return_rows = self.db.execute(
                select(
                    ReturnRecord.sale_id.label("sale_id"),
                    ReturnRecord.quantity.label("quantity"),
                    ReturnRecord.refund_amount.label("refund_amount"),
                    ReturnRecord.refund_fees.label("refund_fees"),
                    ReturnRecord.refund_shipping.label("refund_shipping"),
                )
                .where(ReturnRecord.sale_id.in_(sorted(sale_lot_allocations.keys())))
                .order_by(ReturnRecord.returned_at.asc(), ReturnRecord.id.asc())
            ).all()
            for return_row in return_rows:
                sale_id = int(return_row.sale_id or 0)
                allocations = sale_lot_allocations.get(sale_id) or []
                if not allocations:
                    continue
                return_listing_qty = max(1, int(return_row.quantity or 1))
                refund_total = (
                    self._safe_float(return_row.refund_amount)
                    + self._safe_float(return_row.refund_fees)
                    + self._safe_float(return_row.refund_shipping)
                )
                for allocation in allocations:
                    allocation_qty = max(0, int(allocation.get("quantity") or 0))
                    if allocation_qty <= 0:
                        continue
                    returned_qty = min(
                        allocation_qty,
                        max(1, int(allocation.get("quantity_per_listing") or 1)) * return_listing_qty,
                    )
                    refund_portion = float(returned_qty) / float(
                        max(1, int(allocation.get("bundle_units_per_listing") or 1)) * return_listing_qty
                    )
                    product_id = int(allocation.get("product_id") or 0)
                    if product_id <= 0:
                        continue
                    adjustment = return_adjustments_by_product.setdefault(
                        product_id,
                        {
                            "qty_returned": 0.0,
                            "refund_total": 0.0,
                            "cogs_reversal": 0.0,
                        },
                    )
                    adjustment["qty_returned"] += float(returned_qty)
                    adjustment["refund_total"] += refund_total * refund_portion
                    adjustment["cogs_reversal"] += returned_qty * self._safe_float(allocation.get("unit_cost"))

        rows: list[dict[str, Any]] = []
        summary_assigned_qty = 0
        summary_allocated_cost = 0.0
        summary_est_gross = 0.0
        summary_est_net_before_cogs_before_returns = 0.0
        summary_est_cogs_before_returns = 0.0
        summary_est_net_before_cogs = 0.0
        summary_est_cogs = 0.0
        summary_returns_refund_total = 0.0
        summary_returns_cogs_reversal = 0.0
        summary_cost_source_totals: dict[str, float] = {}
        for product_id, data in product_rollup.items():
            assigned_qty = int(data.get("assigned_qty") or 0)
            unit_landed = float(data.get("unit_landed_cost") or 0)
            allocated_landed = float(data.get("allocated_landed_cost") or 0)
            sold = sales_map.get(product_id) or {}
            qty_sold_total = int(sold.get("qty_sold") or 0)
            gross_sales_total = float(sold.get("gross_sales") or 0.0)
            fees_total = float(sold.get("fees") or 0.0)
            shipping_total = float(sold.get("shipping_cost") or 0.0)
            shipping_label_total = float(sold.get("shipping_label_cost") or 0.0)
            net_before_cogs_total = float(sold.get("net_before_cogs") or 0.0)

            qty_sold_est_from_lot = qty_sold_total
            est_gross_sales = gross_sales_total
            est_net_before_cogs = net_before_cogs_total
            est_lot_cogs = float(sold.get("lot_cogs") or 0.0)
            est_net_before_cogs_before_returns = est_net_before_cogs
            est_lot_cogs_before_returns = est_lot_cogs
            est_lot_profit_before_returns = est_net_before_cogs_before_returns - est_lot_cogs_before_returns
            return_adjustment = return_adjustments_by_product.get(product_id) or {}
            qty_returned_from_lot = int(return_adjustment.get("qty_returned") or 0)
            returns_refund_total = float(return_adjustment.get("refund_total") or 0.0)
            returns_cogs_reversal = float(return_adjustment.get("cogs_reversal") or 0.0)
            returns_profit_impact = -returns_refund_total + returns_cogs_reversal
            est_net_before_cogs -= returns_refund_total
            est_lot_cogs = max(0.0, est_lot_cogs - returns_cogs_reversal)
            est_lot_profit = est_net_before_cogs - est_lot_cogs
            sold_source_totals = {
                str(source): float(total or 0.0)
                for source, total in dict(sold.get("cost_source_totals") or {}).items()
                if float(total or 0.0) > 0
            }
            row_basis_source_totals = sold_source_totals or {
                str(source): float(total or 0.0)
                for source, total in dict(data.get("cost_source_totals") or {}).items()
                if float(total or 0.0) > 0
            }
            row_sources = {
                source
                for source, total in row_basis_source_totals.items()
                if float(total or 0.0) > 0
            }
            if row_sources:
                row_cost_source = sorted(row_sources)[0] if len(row_sources) == 1 else "mixed_lot_cost"
            else:
                row_cost_source = "missing_cost_basis"

            summary_assigned_qty += assigned_qty
            summary_allocated_cost += allocated_landed
            summary_est_gross += est_gross_sales
            summary_est_net_before_cogs_before_returns += est_net_before_cogs_before_returns
            summary_est_cogs_before_returns += est_lot_cogs_before_returns
            summary_est_net_before_cogs += est_net_before_cogs
            summary_est_cogs += est_lot_cogs
            summary_returns_refund_total += returns_refund_total
            summary_returns_cogs_reversal += returns_cogs_reversal
            for source, total in sold_source_totals.items():
                summary_cost_source_totals[source] = summary_cost_source_totals.get(source, 0.0) + float(total)
            rows.append(
                {
                    "product_id": int(product_id),
                    "sku": data.get("sku"),
                    "product_title": data.get("product_title"),
                    "assigned_qty": assigned_qty,
                    "qty_sold_total": qty_sold_total,
                    "qty_sold_est_from_lot": int(qty_sold_est_from_lot),
                    "qty_returned_from_lot": int(qty_returned_from_lot),
                    "unit_landed_cost": round(unit_landed, 4),
                    "allocated_landed_cost": round(allocated_landed, 2),
                    "estimated_gross_sales_from_lot": round(est_gross_sales, 2),
                    "estimated_net_before_cogs_before_returns": round(est_net_before_cogs_before_returns, 2),
                    "estimated_lot_cogs_before_returns": round(est_lot_cogs_before_returns, 2),
                    "estimated_lot_profit_before_returns": round(est_lot_profit_before_returns, 2),
                    "estimated_net_before_cogs_from_lot": round(est_net_before_cogs, 2),
                    "estimated_lot_cogs": round(est_lot_cogs, 2),
                    "estimated_lot_profit": round(est_lot_profit, 2),
                    "returns_refund_total": round(returns_refund_total, 2),
                    "returns_cogs_reversal": round(returns_cogs_reversal, 2),
                    "returns_profit_impact": round(returns_profit_impact, 2),
                    "cost_source": row_cost_source,
                }
            )
        summary_cost_sources = {
            source
            for source, total in summary_cost_source_totals.items()
            if float(total or 0.0) > 0
        }

        return {
            "lot_id": int(lot.id),
            "lot_code": str(lot.lot_code or "").strip(),
            "vendor": str(lot.vendor or "").strip(),
            "purchase_date": lot.purchase_date.isoformat() if lot.purchase_date else None,
            "summary": {
                "assigned_products": int(len(rows)),
                "assigned_qty": int(summary_assigned_qty),
                "allocated_landed_cost": round(float(summary_allocated_cost), 2),
                "estimated_gross_sales": round(float(summary_est_gross), 2),
                "estimated_net_before_cogs_before_returns": round(
                    float(summary_est_net_before_cogs_before_returns),
                    2,
                ),
                "estimated_lot_cogs_before_returns": round(float(summary_est_cogs_before_returns), 2),
                "estimated_lot_profit_before_returns": round(
                    float(summary_est_net_before_cogs_before_returns - summary_est_cogs_before_returns),
                    2,
                ),
                "estimated_net_before_cogs": round(float(summary_est_net_before_cogs), 2),
                "estimated_lot_cogs": round(float(summary_est_cogs), 2),
                "estimated_lot_profit": round(float(summary_est_net_before_cogs - summary_est_cogs), 2),
                "returns_refund_total": round(float(summary_returns_refund_total), 2),
                "returns_cogs_reversal": round(float(summary_returns_cogs_reversal), 2),
                "returns_profit_impact": round(
                    float(-summary_returns_refund_total + summary_returns_cogs_reversal),
                    2,
                ),
                "cost_source": (
                    sorted(summary_cost_sources)[0] if len(summary_cost_sources) == 1 else "mixed_lot_cost"
                )
                if summary_cost_sources
                else "missing_cost_basis",
                "cost_source_totals": {
                    source: round(float(total), 2)
                    for source, total in sorted(summary_cost_source_totals.items())
                },
            },
            "rows": sorted(rows, key=lambda x: str(x.get("sku") or "")),
        }

    def report_sale_unit_cost_maps(
        self,
        *,
        end_dt: datetime,
        default_unit_cost_by_product: dict[int, float] | None = None,
    ) -> dict[str, Any]:
        def _safe_float(value: Any) -> float:
            try:
                if value is None:
                    return 0.0
                return float(value)
            except Exception:
                return 0.0

        defaults = {
            int(k): max(0.0, _safe_float(v))
            for k, v in (default_unit_cost_by_product or {}).items()
            if k is not None
        }

        assignment_rows = self.db.execute(
            select(
                ProductLotAssignment.id.label("assignment_id"),
                ProductLotAssignment.product_id.label("product_id"),
                ProductLotAssignment.lot_id.label("lot_id"),
                ProductLotAssignment.acquired_at.label("acquired_at"),
                ProductLotAssignment.quantity_acquired.label("quantity_acquired"),
                ProductLotAssignment.unit_cost.label("unit_cost"),
                ProductLotAssignment.unit_tax_paid.label("unit_tax_paid"),
                ProductLotAssignment.unit_shipping_paid.label("unit_shipping_paid"),
                ProductLotAssignment.unit_handling_paid.label("unit_handling_paid"),
                ProductLotAssignment.allocated_cost.label("allocated_cost"),
                ProductLotAssignment.allocated_tax_paid.label("allocated_tax_paid"),
                ProductLotAssignment.allocated_shipping_paid.label("allocated_shipping_paid"),
                ProductLotAssignment.allocated_handling_paid.label("allocated_handling_paid"),
                ProductLotAssignment.allocation_weight.label("allocation_weight"),
                PurchaseLot.total_cost.label("lot_total_cost"),
                PurchaseLot.total_tax_paid.label("lot_total_tax_paid"),
                PurchaseLot.total_shipping_paid.label("lot_total_shipping_paid"),
                PurchaseLot.total_handling_paid.label("lot_total_handling_paid"),
                PurchaseLot.expected_total_quantity.label("lot_expected_total_quantity"),
            )
            .select_from(ProductLotAssignment)
            .join(PurchaseLot, PurchaseLot.id == ProductLotAssignment.lot_id, isouter=True)
            .where(
                ProductLotAssignment.product_id.is_not(None),
                ProductLotAssignment.acquired_at <= end_dt,
            )
            .order_by(ProductLotAssignment.acquired_at.asc(), ProductLotAssignment.id.asc())
        ).all()

        sale_rows = self.db.execute(
            select(
                Sale.id.label("sale_id"),
                Sale.product_id.label("product_id"),
                Sale.listing_id.label("listing_id"),
                Sale.sold_at.label("sold_at"),
                Sale.quantity_sold.label("quantity_sold"),
                MarketplaceListing.marketplace_details.label("listing_marketplace_details"),
            )
            .select_from(Sale)
            .outerjoin(MarketplaceListing, MarketplaceListing.id == Sale.listing_id)
            .where(
                Sale.sold_at.is_not(None),
                Sale.sold_at <= end_dt,
            )
            .order_by(Sale.sold_at.asc(), Sale.id.asc())
        ).all()

        lots_by_product: dict[int, list[dict[str, float | int | datetime | None]]] = {}
        totals: dict[int, dict[str, float]] = {}
        source_totals: dict[int, dict[str, float]] = {}
        lot_fallback_unit_costs, assignment_fallback_unit_costs = self._lot_fallback_unit_cost_maps_from_rows(
            list(assignment_rows)
        )

        for row in assignment_rows:
            pid = int(row.product_id or 0)
            qty = float(max(0, int(row.quantity_acquired or 0)))
            if pid <= 0 or qty <= 0:
                continue
            unit_cost = self._landed_unit_cost_from_assignment_row(
                row,
                lot_fallback_unit_costs=lot_fallback_unit_costs,
                assignment_fallback_unit_costs=assignment_fallback_unit_costs,
            )
            explicit_unit_cost = self._explicit_landed_unit_cost_from_assignment_row(row)
            assignment_id = int(row.assignment_id or 0)
            lot_id = int(row.lot_id or 0)
            if explicit_unit_cost > 0 and row.unit_cost is not None:
                cost_source = "assignment_unit_landed_cost"
            elif explicit_unit_cost > 0:
                cost_source = "assignment_allocated_landed_cost"
            elif assignment_id in assignment_fallback_unit_costs:
                cost_source = "lot_allocation_weight"
            elif lot_id in lot_fallback_unit_costs and int(row.lot_expected_total_quantity or 0) > 0:
                cost_source = "lot_expected_quantity_fallback"
            elif lot_id in lot_fallback_unit_costs:
                cost_source = "lot_equal_quantity_fallback"
            else:
                cost_source = "missing_cost_basis"
            lots_by_product.setdefault(pid, []).append(
                {
                    "product_id": pid,
                    "remaining_qty": int(qty),
                    "unit_cost": float(unit_cost),
                    "cost_source": cost_source,
                    "acquired_at": row.acquired_at,
                    "assignment_id": int(row.assignment_id or 0),
                    "lot_id": int(row.lot_id or 0),
                }
            )
            totals.setdefault(pid, {"qty": 0.0, "cost": 0.0})
            totals[pid]["qty"] += qty
            totals[pid]["cost"] += float(unit_cost) * qty
            source_totals.setdefault(pid, {})
            source_totals[pid][cost_source] = source_totals[pid].get(cost_source, 0.0) + qty

        lot_weighted_unit_cost_by_product: dict[int, float] = {}
        lot_weighted_unit_cost_source_by_product: dict[int, str] = {}
        for pid, agg in totals.items():
            if float(agg["qty"] or 0) > 0:
                lot_weighted_unit_cost_by_product[pid] = float(agg["cost"] / agg["qty"])
                sources = {
                    source
                    for source, qty in source_totals.get(pid, {}).items()
                    if float(qty or 0.0) > 0
                }
                lot_weighted_unit_cost_source_by_product[pid] = (
                    sorted(sources)[0] if len(sources) == 1 else "mixed_lot_cost"
                )
        for pid, default_cost in defaults.items():
            lot_weighted_unit_cost_by_product.setdefault(pid, max(0.0, _safe_float(default_cost)))
            lot_weighted_unit_cost_source_by_product.setdefault(
                pid,
                "product_default_landed_cost" if _safe_float(default_cost) > 0 else "missing_cost_basis",
            )

        from collections import deque

        lot_sources = {
            pid: sorted(
                rows,
                key=lambda item: (
                    item.get("acquired_at") or datetime.min,
                    int(item.get("assignment_id") or 0),
                ),
            )
            for pid, rows in lots_by_product.items()
        }
        source_index_by_product = {pid: 0 for pid in lot_sources}
        queues = {pid: deque() for pid in set(lot_sources) | set(defaults)}

        def _queue_available_lots(pid: int, cutoff: datetime) -> None:
            rows = lot_sources.get(pid) or []
            idx = int(source_index_by_product.get(pid, 0))
            queue = queues.setdefault(pid, deque())
            while idx < len(rows):
                acquired_at = rows[idx].get("acquired_at") or datetime.min
                if acquired_at > cutoff:
                    break
                queue.append(dict(rows[idx]))
                idx += 1
            source_index_by_product[pid] = idx

        fifo_unit_cost_by_sale: dict[int, float] = {}
        fifo_total_cost_by_sale: dict[int, float] = {}
        fifo_unit_cost_source_by_sale: dict[int, str] = {}
        fifo_cogs_evidence_by_sale: dict[int, list[dict[str, Any]]] = {}
        for row in sale_rows:
            sale_id = int(row.sale_id or 0)
            pid = int(row.product_id or 0)
            qty = max(1, int(row.quantity_sold or 1))
            if sale_id <= 0:
                continue

            total_cost = 0.0
            consumed_sources: set[str] = set()
            bundle_components = self._bundle_components_from_payload(
                self._listing_bundle_payload_from_raw(row.listing_marketplace_details),
                qty,
            )
            consumption_rows = (
                [
                    {
                        "product_id": int(component["product_id"]),
                        "quantity_total": max(1, int(component.get("quantity_total") or 1)),
                    }
                    for component in bundle_components
                    if int(component.get("product_id") or 0) > 0
                ]
                if bundle_components
                else ([{"product_id": pid, "quantity_total": qty}] if pid > 0 else [])
            )
            if not consumption_rows:
                continue
            sale_evidence: list[dict[str, Any]] = []
            for consumption in consumption_rows:
                consume_pid = int(consumption["product_id"])
                qty_remaining = max(1, int(consumption["quantity_total"]))
                _queue_available_lots(consume_pid, row.sold_at or datetime.min)
                queue = queues.setdefault(consume_pid, deque())
                default_cost = max(0.0, _safe_float(defaults.get(consume_pid)))
                while qty_remaining > 0:
                    if queue and int(queue[0]["remaining_qty"]) > 0:
                        use_qty = min(qty_remaining, int(queue[0]["remaining_qty"]))
                        unit_cost = _safe_float(queue[0]["unit_cost"])
                        source = str(queue[0].get("cost_source") or "missing_cost_basis")
                        allocation_total = float(use_qty) * unit_cost
                        total_cost += allocation_total
                        consumed_sources.add(source)
                        sale_evidence.append(
                            {
                                "product_id": consume_pid,
                                "lot_id": int(queue[0].get("lot_id") or 0) or None,
                                "assignment_id": int(queue[0].get("assignment_id") or 0) or None,
                                "quantity": int(use_qty),
                                "unit_cost": round(float(unit_cost), 6),
                                "total_cost": round(float(allocation_total), 6),
                                "cost_source": source,
                            }
                        )
                        queue[0]["remaining_qty"] = int(queue[0]["remaining_qty"]) - use_qty
                        qty_remaining -= use_qty
                        if int(queue[0]["remaining_qty"]) <= 0:
                            queue.popleft()
                    else:
                        source = "product_default_landed_cost" if default_cost > 0 else "missing_cost_basis"
                        allocation_total = float(qty_remaining) * default_cost
                        total_cost += allocation_total
                        consumed_sources.add(source)
                        sale_evidence.append(
                            {
                                "product_id": consume_pid,
                                "lot_id": None,
                                "assignment_id": None,
                                "quantity": int(qty_remaining),
                                "unit_cost": round(float(default_cost), 6),
                                "total_cost": round(float(allocation_total), 6),
                                "cost_source": source,
                            }
                        )
                        qty_remaining = 0
            fifo_total_cost_by_sale[sale_id] = round(float(total_cost), 6)
            fifo_unit_cost_by_sale[sale_id] = (total_cost / float(qty)) if qty > 0 else 0.0
            fifo_unit_cost_source_by_sale[sale_id] = (
                sorted(consumed_sources)[0] if len(consumed_sources) == 1 else "mixed_fifo_cost"
            )
            fifo_cogs_evidence_by_sale[sale_id] = sale_evidence

        for pid in lot_sources:
            _queue_available_lots(pid, end_dt)

        fifo_remaining_unit_cost_by_product: dict[int, float] = {}
        fifo_remaining_unit_cost_source_by_product: dict[int, str] = {}
        for pid, queue in queues.items():
            remaining_qty = 0.0
            remaining_cost = 0.0
            remaining_sources: set[str] = set()
            for item in list(queue):
                qty = float(max(0, int(item.get("remaining_qty") or 0)))
                if qty <= 0:
                    continue
                remaining_qty += qty
                remaining_cost += qty * _safe_float(item.get("unit_cost"))
                remaining_sources.add(str(item.get("cost_source") or "missing_cost_basis"))
            if remaining_qty > 0:
                fifo_remaining_unit_cost_by_product[int(pid)] = remaining_cost / remaining_qty
                fifo_remaining_unit_cost_source_by_product[int(pid)] = (
                    sorted(remaining_sources)[0] if len(remaining_sources) == 1 else "mixed_fifo_cost"
                )
        for pid, default_cost in defaults.items():
            fifo_remaining_unit_cost_by_product.setdefault(pid, max(0.0, _safe_float(default_cost)))
            fifo_remaining_unit_cost_source_by_product.setdefault(
                pid,
                "product_default_landed_cost" if _safe_float(default_cost) > 0 else "missing_cost_basis",
            )

        return {
            "fifo_unit_cost_by_sale": fifo_unit_cost_by_sale,
            "fifo_total_cost_by_sale": fifo_total_cost_by_sale,
            "fifo_unit_cost_source_by_sale": fifo_unit_cost_source_by_sale,
            "fifo_cogs_evidence_by_sale": fifo_cogs_evidence_by_sale,
            "lot_weighted_unit_cost_by_product": lot_weighted_unit_cost_by_product,
            "lot_weighted_unit_cost_source_by_product": lot_weighted_unit_cost_source_by_product,
            "fifo_remaining_unit_cost_by_product": fifo_remaining_unit_cost_by_product,
            "fifo_remaining_unit_cost_source_by_product": fifo_remaining_unit_cost_source_by_product,
        }

    def update_product(self, product_id: int, updates: dict[str, Any], actor: str = "system") -> Product:
        product = self.db.get(Product, product_id)
        if product is None:
            raise ValueError(f"Product {product_id} not found.")

        updates = dict(updates)
        if "metal_type" in updates:
            resolved_metal_type, original_metal_detail = self._normalize_product_metal_type(
                str(updates.get("metal_type") or "")
            )
            updates["metal_type"] = resolved_metal_type
            if original_metal_detail:
                updates["description"] = self._append_product_detail_once(
                    str(updates.get("description", product.description) or ""),
                    label="Metal composition",
                    value=original_metal_detail,
                )

        new_title = updates.get("title", product.title)
        new_quantity = updates.get("current_quantity", product.current_quantity)
        new_acquisition_cost = updates.get("acquisition_cost", product.acquisition_cost)
        new_acquisition_tax_paid = updates.get("acquisition_tax_paid", product.acquisition_tax_paid)
        new_acquisition_shipping_paid = updates.get("acquisition_shipping_paid", product.acquisition_shipping_paid)
        new_acquisition_handling_paid = updates.get("acquisition_handling_paid", product.acquisition_handling_paid)
        new_product_cost = updates.get("product_cost", product.product_cost)
        ValidationService.require_non_empty("Product title", new_title)
        ValidationService.require_positive_int("Current quantity", new_quantity, min_value=0)
        ValidationService.require_non_negative_decimal("Acquisition cost", new_acquisition_cost)
        ValidationService.require_non_negative_decimal("Acquisition tax paid", new_acquisition_tax_paid)
        ValidationService.require_non_negative_decimal("Acquisition shipping paid", new_acquisition_shipping_paid)
        ValidationService.require_non_negative_decimal("Acquisition handling paid", new_acquisition_handling_paid)
        ValidationService.require_non_negative_decimal("Product cost", new_product_cost)
        if "inventory_class" in updates:
            updates["inventory_class"] = self._validate_inventory_class(str(updates.get("inventory_class") or ""))
        new_ebay_purchase = bool(updates.get("ebay_purchase", product.ebay_purchase))
        new_ebay_purchase_item_id = str(
            updates.get("ebay_purchase_item_id", product.ebay_purchase_item_id or "")
        ).strip()
        if new_ebay_purchase and not new_ebay_purchase_item_id:
            raise ValueError("eBay purchase item ID is required when eBay purchase is enabled.")
        if "ebay_purchase" in updates and not new_ebay_purchase:
            updates["ebay_purchase_item_id"] = ""
            updates["ebay_purchase_url"] = ""
        if "coin_reference_id" in updates and updates.get("coin_reference_id") is not None:
            if self.db.get(CoinReferenceCatalog, int(updates.get("coin_reference_id"))) is None:
                raise ValueError("Selected coin reference does not exist.")

        changes: dict[str, dict[str, Any]] = {}
        for field, new_value in updates.items():
            if not hasattr(product, field):
                continue
            old_value = getattr(product, field)
            if old_value != new_value:
                changes[field] = {
                    "before": self._serialize_audit_value(old_value),
                    "after": self._serialize_audit_value(new_value),
                }
                setattr(product, field, new_value)

        if changes:
            self._record_audit("product", product.id, "update", actor, changes)
            self.db.commit()
            self.db.refresh(product)
        return product

    def archive_product(
        self,
        product_id: int,
        *,
        actor: str = "system",
        force: bool = False,
    ) -> Product:
        product = self.db.get(Product, int(product_id))
        if product is None:
            raise ValueError(f"Product {product_id} not found.")

        active_listing_count = int(
            self.db.query(func.count(MarketplaceListing.id))
            .filter(
                MarketplaceListing.product_id == int(product_id),
                MarketplaceListing.listing_status == "active",
            )
            .scalar()
            or 0
        )
        if active_listing_count > 0 and not bool(force):
            raise ValueError(
                "Cannot archive product with active listings. End/archive linked listings first, or force archive."
            )
        return self.update_product(int(product_id), {"status": "archived"}, actor=actor)

    def restore_product(self, product_id: int, *, actor: str = "system") -> Product:
        product = self.db.get(Product, int(product_id))
        if product is None:
            raise ValueError(f"Product {product_id} not found.")
        return self.update_product(int(product_id), {"status": "active"}, actor=actor)

    def convert_inventory_to_product(
        self,
        *,
        source_product_id: int,
        source_quantity_used: int,
        target_sku: str,
        target_title: str,
        target_category: str,
        target_inventory_class: str = "sellable",
        target_description: str = "",
        target_metal_type: str = "",
        target_weight_oz: Decimal | None = None,
        target_quantity_created: int = 1,
        target_unit_cost: Decimal | None = None,
        acquired_at: datetime | None = None,
        lot_id: int | None = None,
        notes: str = "",
        actor: str = "system",
    ) -> Product:
        source = self.db.get(Product, int(source_product_id))
        if source is None:
            raise ValueError(f"Source product {source_product_id} not found.")
        ValidationService.require_non_empty("Target SKU", target_sku)
        ValidationService.require_non_empty("Target title", target_title)
        ValidationService.require_positive_int("Source quantity used", source_quantity_used, min_value=1)
        ValidationService.require_positive_int("Target quantity created", target_quantity_created, min_value=1)
        ValidationService.require_non_negative_decimal("Target weight (oz)", target_weight_oz)
        resolved_target_inventory_class = self._validate_inventory_class(target_inventory_class)
        qty_before_source = int(source.current_quantity or 0)
        if int(source_quantity_used) > qty_before_source:
            raise ValueError(
                f"Not enough source quantity on hand. Requested {int(source_quantity_used)}; on hand {qty_before_source}."
            )

        occurred = acquired_at or utcnow_naive()
        source_base_unit_cost = (
            source.product_cost
            if source.product_cost is not None
            else (source.acquisition_cost if source.acquisition_cost is not None else Decimal("0"))
        )
        source_unit_tax = source.acquisition_tax_paid or Decimal("0")
        source_unit_shipping = source.acquisition_shipping_paid or Decimal("0")
        source_unit_handling = source.acquisition_handling_paid or Decimal("0")

        allocated_cost_total = (source_base_unit_cost or Decimal("0")) * int(source_quantity_used)
        allocated_tax_total = source_unit_tax * int(source_quantity_used)
        allocated_shipping_total = source_unit_shipping * int(source_quantity_used)
        allocated_handling_total = source_unit_handling * int(source_quantity_used)

        resolved_target_unit_cost = target_unit_cost
        if resolved_target_unit_cost is None:
            resolved_target_unit_cost = (
                (allocated_cost_total / Decimal(int(target_quantity_created)))
                if int(target_quantity_created) > 0
                else Decimal("0")
            )
        ValidationService.require_non_negative_decimal("Target unit cost", resolved_target_unit_cost)

        target_unit_tax = allocated_tax_total / Decimal(int(target_quantity_created))
        target_unit_shipping = allocated_shipping_total / Decimal(int(target_quantity_created))
        target_unit_handling = allocated_handling_total / Decimal(int(target_quantity_created))

        target = Product(
            sku=target_sku.strip(),
            title=target_title.strip(),
            category=(target_category or source.category or "other").strip(),
            inventory_class=resolved_target_inventory_class,
            description=(target_description or "").strip(),
            metal_type=(target_metal_type or "").strip(),
            weight_oz=target_weight_oz,
            acquisition_cost=resolved_target_unit_cost,
            acquisition_tax_paid=target_unit_tax,
            acquisition_shipping_paid=target_unit_shipping,
            acquisition_handling_paid=target_unit_handling,
            product_cost=resolved_target_unit_cost,
            current_quantity=int(target_quantity_created),
            acquired_at=occurred,
            status="active",
        )
        self.db.add(target)
        self.db.flush()

        if lot_id is not None:
            assignment = ProductLotAssignment(
                product_id=int(target.id),
                lot_id=int(lot_id),
                quantity_acquired=int(target_quantity_created),
                unit_cost=resolved_target_unit_cost,
                unit_tax_paid=target_unit_tax,
                unit_shipping_paid=target_unit_shipping,
                unit_handling_paid=target_unit_handling,
                allocated_cost=(resolved_target_unit_cost * int(target_quantity_created)),
                allocated_tax_paid=allocated_tax_total,
                allocated_shipping_paid=allocated_shipping_total,
                allocated_handling_paid=allocated_handling_total,
                acquired_at=occurred,
            )
            self.db.add(assignment)

        qty_after_source = qty_before_source - int(source_quantity_used)
        source.current_quantity = qty_after_source

        self._record_inventory_movement(
            product_id=int(source.id),
            movement_type="conversion_out",
            quantity_before=qty_before_source,
            quantity_after=qty_after_source,
            unit_cost=source_base_unit_cost,
            reference_type="product_conversion",
            reference_id=int(target.id),
            notes=(notes or "").strip() or f"Converted {int(source_quantity_used)} into product #{int(target.id)}.",
            occurred_at=occurred,
        )
        self._record_inventory_movement(
            product_id=int(target.id),
            movement_type="conversion_in",
            quantity_before=0,
            quantity_after=int(target_quantity_created),
            unit_cost=resolved_target_unit_cost,
            reference_type="product_conversion",
            reference_id=int(source.id),
            notes=(notes or "").strip()
            or f"Created from source product #{int(source.id)} using {int(source_quantity_used)} units.",
            occurred_at=occurred,
        )

        self._record_audit(
            entity_type="product",
            entity_id=int(source.id),
            action="convert_out",
            actor=actor,
            changes={
                "source_product_id": int(source.id),
                "target_product_id": int(target.id),
                "source_quantity_used": int(source_quantity_used),
                "source_quantity_before": qty_before_source,
                "source_quantity_after": qty_after_source,
                "notes": (notes or "").strip(),
            },
        )
        self._record_audit(
            entity_type="product",
            entity_id=int(target.id),
            action="convert_in",
            actor=actor,
            changes={
                "source_product_id": int(source.id),
                "target_product_id": int(target.id),
                "target_quantity_created": int(target_quantity_created),
                "target_inventory_class": resolved_target_inventory_class,
                "target_unit_cost": self._serialize_audit_value(resolved_target_unit_cost),
                "allocated_cost_total": self._serialize_audit_value(allocated_cost_total),
                "notes": (notes or "").strip(),
            },
        )

        self.db.commit()
        self.db.refresh(target)
        return target

    def convert_inventory_to_multiple_products(
        self,
        *,
        source_product_id: int,
        source_quantity_used: int,
        targets: list[dict[str, Any]],
        acquired_at: datetime | None = None,
        lot_id: int | None = None,
        notes: str = "",
        actor: str = "system",
    ) -> list[Product]:
        source = self.db.get(Product, int(source_product_id))
        if source is None:
            raise ValueError(f"Source product {source_product_id} not found.")
        ValidationService.require_positive_int("Source quantity used", source_quantity_used, min_value=1)
        if not targets:
            raise ValueError("At least one target product is required.")

        normalized_targets: list[dict[str, Any]] = []
        total_target_quantity = 0
        for idx, row in enumerate(targets, start=1):
            sku = str(row.get("sku") or "").strip()
            title = str(row.get("title") or "").strip()
            category = str(row.get("category") or "other").strip()
            inventory_class = self._validate_inventory_class(str(row.get("inventory_class") or "sellable"))
            description = str(row.get("description") or "").strip()
            metal_type = str(row.get("metal_type") or source.metal_type or "").strip()
            quantity_created = int(row.get("quantity_created") or 0)
            target_weight_oz = row.get("weight_oz")
            target_unit_cost = row.get("unit_cost")
            ValidationService.require_non_empty(f"Target {idx} SKU", sku)
            ValidationService.require_non_empty(f"Target {idx} title", title)
            ValidationService.require_positive_int(f"Target {idx} quantity", quantity_created, min_value=1)
            ValidationService.require_non_negative_decimal(f"Target {idx} weight (oz)", target_weight_oz)
            ValidationService.require_non_negative_decimal(f"Target {idx} unit cost", target_unit_cost)
            total_target_quantity += int(quantity_created)
            normalized_targets.append(
                {
                    "sku": sku,
                    "title": title,
                    "category": category,
                    "inventory_class": inventory_class,
                    "description": description,
                    "metal_type": metal_type,
                    "quantity_created": int(quantity_created),
                    "weight_oz": target_weight_oz,
                    "unit_cost": target_unit_cost,
                }
            )
        if total_target_quantity <= 0:
            raise ValueError("Total target quantity must be > 0.")

        qty_before_source = int(source.current_quantity or 0)
        if int(source_quantity_used) > qty_before_source:
            raise ValueError(
                f"Not enough source quantity on hand. Requested {int(source_quantity_used)}; on hand {qty_before_source}."
            )

        occurred = acquired_at or utcnow_naive()
        source_base_unit_cost = (
            source.product_cost
            if source.product_cost is not None
            else (source.acquisition_cost if source.acquisition_cost is not None else Decimal("0"))
        )
        source_unit_tax = source.acquisition_tax_paid or Decimal("0")
        source_unit_shipping = source.acquisition_shipping_paid or Decimal("0")
        source_unit_handling = source.acquisition_handling_paid or Decimal("0")

        allocated_cost_total = (source_base_unit_cost or Decimal("0")) * int(source_quantity_used)
        allocated_tax_total = source_unit_tax * int(source_quantity_used)
        allocated_shipping_total = source_unit_shipping * int(source_quantity_used)
        allocated_handling_total = source_unit_handling * int(source_quantity_used)

        created_products: list[Product] = []
        created_target_ids: list[int] = []
        for row in normalized_targets:
            qty_created = int(row["quantity_created"])
            share = Decimal(qty_created) / Decimal(total_target_quantity)
            target_allocated_cost = allocated_cost_total * share
            target_allocated_tax = allocated_tax_total * share
            target_allocated_shipping = allocated_shipping_total * share
            target_allocated_handling = allocated_handling_total * share
            resolved_unit_cost = row["unit_cost"]
            if resolved_unit_cost is None:
                resolved_unit_cost = target_allocated_cost / Decimal(qty_created)
            target_unit_tax = target_allocated_tax / Decimal(qty_created)
            target_unit_shipping = target_allocated_shipping / Decimal(qty_created)
            target_unit_handling = target_allocated_handling / Decimal(qty_created)

            target = Product(
                sku=str(row["sku"]).strip(),
                title=str(row["title"]).strip(),
                category=str(row["category"]).strip(),
                inventory_class=str(row["inventory_class"]).strip(),
                description=str(row["description"]).strip(),
                metal_type=str(row["metal_type"]).strip(),
                weight_oz=row["weight_oz"],
                acquisition_cost=resolved_unit_cost,
                acquisition_tax_paid=target_unit_tax,
                acquisition_shipping_paid=target_unit_shipping,
                acquisition_handling_paid=target_unit_handling,
                product_cost=resolved_unit_cost,
                current_quantity=qty_created,
                acquired_at=occurred,
                status="active",
            )
            self.db.add(target)
            self.db.flush()
            created_products.append(target)
            created_target_ids.append(int(target.id))

            if lot_id is not None:
                assignment = ProductLotAssignment(
                    product_id=int(target.id),
                    lot_id=int(lot_id),
                    quantity_acquired=qty_created,
                    unit_cost=resolved_unit_cost,
                    unit_tax_paid=target_unit_tax,
                    unit_shipping_paid=target_unit_shipping,
                    unit_handling_paid=target_unit_handling,
                    allocated_cost=(resolved_unit_cost * qty_created),
                    allocated_tax_paid=target_allocated_tax,
                    allocated_shipping_paid=target_allocated_shipping,
                    allocated_handling_paid=target_allocated_handling,
                    acquired_at=occurred,
                )
                self.db.add(assignment)

            self._record_inventory_movement(
                product_id=int(target.id),
                movement_type="conversion_in",
                quantity_before=0,
                quantity_after=qty_created,
                unit_cost=resolved_unit_cost,
                reference_type="product_conversion",
                reference_id=int(source.id),
                notes=(notes or "").strip()
                or f"Created from source product #{int(source.id)} via bulk conversion.",
                occurred_at=occurred,
            )
            self._record_audit(
                entity_type="product",
                entity_id=int(target.id),
                action="convert_in",
                actor=actor,
                changes={
                    "source_product_id": int(source.id),
                    "target_product_id": int(target.id),
                    "target_quantity_created": qty_created,
                    "target_inventory_class": str(row["inventory_class"]).strip(),
                    "target_unit_cost": self._serialize_audit_value(resolved_unit_cost),
                    "notes": (notes or "").strip(),
                },
            )

        qty_after_source = qty_before_source - int(source_quantity_used)
        source.current_quantity = qty_after_source
        self._record_inventory_movement(
            product_id=int(source.id),
            movement_type="conversion_out",
            quantity_before=qty_before_source,
            quantity_after=qty_after_source,
            unit_cost=source_base_unit_cost,
            reference_type="product_conversion",
            reference_id=(created_target_ids[0] if created_target_ids else None),
            notes=(
                (notes or "").strip()
                or f"Bulk conversion into targets: {', '.join(str(v) for v in created_target_ids)}"
            ),
            occurred_at=occurred,
        )
        self._record_audit(
            entity_type="product",
            entity_id=int(source.id),
            action="convert_out",
            actor=actor,
            changes={
                "source_product_id": int(source.id),
                "target_product_ids": created_target_ids,
                "source_quantity_used": int(source_quantity_used),
                "source_quantity_before": qty_before_source,
                "source_quantity_after": qty_after_source,
                "notes": (notes or "").strip(),
            },
        )
        self.db.commit()
        for target in created_products:
            self.db.refresh(target)
        return created_products

    def update_listing(
        self, listing_id: int, updates: dict[str, Any], actor: str = "system"
    ) -> MarketplaceListing:
        listing = self.db.get(MarketplaceListing, listing_id)
        if listing is None:
            raise ValueError(f"Listing {listing_id} not found.")

        new_marketplace = updates.get("marketplace", listing.marketplace)
        new_title = updates.get("listing_title", listing.listing_title)
        new_price = updates.get("listing_price", listing.listing_price)
        new_quantity = updates.get("quantity_listed", listing.quantity_listed)
        new_external_listing_id = updates.get("external_listing_id", listing.external_listing_id)
        ValidationService.require_non_empty("Marketplace", new_marketplace)
        ValidationService.require_non_empty("Listing title", new_title)
        ValidationService.require_positive_int("Quantity listed", new_quantity)
        ValidationService.require_non_negative_decimal("Listing price", new_price)
        ValidationService.ensure_unique_marketplace_listing(
            self.db,
            new_marketplace,
            new_external_listing_id,
            exclude_listing_id=listing.id,
        )
        requested_status = str(updates.get("listing_status", listing.listing_status) or "").strip().lower()
        requested_review_status = str(
            updates.get("review_status", listing.review_status or "pending")
        ).strip().lower()
        if requested_status == "active" and requested_review_status != "approved":
            raise ValueError("Listing must be approved in review before setting status to `active`.")
        if requested_status == "active":
            review_policy = self.get_runtime_setting(
                environment=settings.app_env,
                key="listing_review_two_person_required",
                active_only=True,
            )
            channels_setting = self.get_runtime_setting(
                environment=settings.app_env,
                key="listing_review_two_person_channels_csv",
                active_only=True,
            )
            required = str(getattr(review_policy, "value", "false") or "false").strip().lower() in {
                "1",
                "true",
                "yes",
                "y",
                "on",
            }
            configured_channels = str(
                getattr(channels_setting, "value", "ebay") or "ebay"
            ).strip().lower()
            channel_tokens = {
                token.strip().lower()
                for token in configured_channels.replace("\n", ",").split(",")
                if token.strip()
            } or {"ebay"}
            target_marketplace = str(updates.get("marketplace", listing.marketplace) or "").strip().lower()
            reviewed_by_value = str(updates.get("reviewed_by", listing.reviewed_by or "") or "").strip().lower()
            actor_value = (actor or "system").strip().lower()
            if required and target_marketplace in channel_tokens and reviewed_by_value == actor_value:
                raise ValueError(
                    "Two-person review policy is enabled for this marketplace. "
                    "A different user must publish than the reviewer."
                )

        changes: dict[str, dict[str, Any]] = {}
        for field, new_value in updates.items():
            if not hasattr(listing, field):
                continue
            old_value = getattr(listing, field)
            if old_value != new_value:
                changes[field] = {
                    "before": self._serialize_audit_value(old_value),
                    "after": self._serialize_audit_value(new_value),
                }
                setattr(listing, field, new_value)

        if changes:
            self._record_audit("listing", listing.id, "update", actor, changes)
            self.db.commit()
            self.db.refresh(listing)
        return listing

    def delete_listing(self, listing_id: int, actor: str = "system") -> bool:
        listing = self.db.get(MarketplaceListing, listing_id)
        if listing is None:
            return False

        linked_sales_count = int(
            self.db.query(func.count(Sale.id)).filter(Sale.listing_id == int(listing_id)).scalar() or 0
        )
        linked_order_items_count = int(
            self.db.query(func.count(OrderItem.id)).filter(OrderItem.listing_id == int(listing_id)).scalar() or 0
        )
        linked_media_count = int(
            self.db.query(func.count(MediaAsset.id)).filter(MediaAsset.listing_id == int(listing_id)).scalar() or 0
        )

        listing_snapshot = {
            "id": int(listing.id),
            "product_id": int(listing.product_id),
            "marketplace": str(listing.marketplace or "").strip(),
            "external_listing_id": str(listing.external_listing_id or "").strip(),
            "listing_status": str(listing.listing_status or "").strip(),
            "review_status": str(listing.review_status or "").strip(),
            "listing_title": str(listing.listing_title or "").strip(),
        }

        # Preserve related operational records while removing the bad/duplicate listing record.
        self.db.query(Sale).filter(Sale.listing_id == int(listing_id)).update(
            {Sale.listing_id: None},
            synchronize_session=False,
        )
        self.db.query(OrderItem).filter(OrderItem.listing_id == int(listing_id)).update(
            {OrderItem.listing_id: None},
            synchronize_session=False,
        )
        self.db.query(MediaAsset).filter(MediaAsset.listing_id == int(listing_id)).update(
            {MediaAsset.listing_id: None},
            synchronize_session=False,
        )

        self.db.delete(listing)
        self._record_audit(
            "listing",
            int(listing_id),
            "delete",
            actor,
            {
                "listing": {
                    "before": listing_snapshot,
                    "after": None,
                },
                "linked_sales_count": {
                    "before": linked_sales_count,
                    "after": 0,
                },
                "linked_order_items_count": {
                    "before": linked_order_items_count,
                    "after": 0,
                },
                "linked_media_count": {
                    "before": linked_media_count,
                    "after": 0,
                },
            },
        )
        self.db.commit()
        return True

    def archive_listing(
        self,
        listing_id: int,
        *,
        actor: str = "system",
        reason: str = "",
    ) -> MarketplaceListing:
        listing = self.db.get(MarketplaceListing, listing_id)
        if listing is None:
            raise ValueError(f"Listing {listing_id} not found.")

        details_obj: dict[str, Any] = {}
        raw = str(listing.marketplace_details or "").strip()
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    details_obj = parsed
                else:
                    details_obj = {"notes": raw}
            except Exception:
                details_obj = {"notes": raw}

        lifecycle = details_obj.get("lifecycle")
        if not isinstance(lifecycle, dict):
            lifecycle = {}
        lifecycle["archived"] = True
        lifecycle["archived_at"] = utcnow_naive().isoformat()
        lifecycle["archived_by"] = (actor or "system").strip() or "system"
        lifecycle["archive_reason"] = str(reason or "").strip()
        details_obj["lifecycle"] = lifecycle

        updates: dict[str, Any] = {
            "marketplace_details": json.dumps(details_obj, indent=2),
        }
        if str(listing.listing_status or "").strip().lower() == "active":
            updates["listing_status"] = "ended"
        return self.update_listing(int(listing_id), updates, actor=actor)

    def restore_listing(
        self,
        listing_id: int,
        *,
        actor: str = "system",
    ) -> MarketplaceListing:
        listing = self.db.get(MarketplaceListing, listing_id)
        if listing is None:
            raise ValueError(f"Listing {listing_id} not found.")

        details_obj: dict[str, Any] = {}
        raw = str(listing.marketplace_details or "").strip()
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    details_obj = parsed
                else:
                    details_obj = {"notes": raw}
            except Exception:
                details_obj = {"notes": raw}

        lifecycle = details_obj.get("lifecycle")
        if not isinstance(lifecycle, dict):
            lifecycle = {}
        lifecycle["archived"] = False
        lifecycle["restored_at"] = utcnow_naive().isoformat()
        lifecycle["restored_by"] = (actor or "system").strip() or "system"
        details_obj["lifecycle"] = lifecycle
        return self.update_listing(
            int(listing_id),
            {"marketplace_details": json.dumps(details_obj, indent=2)},
            actor=actor,
        )

    def review_listing(
        self,
        listing_id: int,
        *,
        decision: str,
        actor: str = "system",
        notes: str = "",
    ) -> MarketplaceListing:
        listing = self.db.get(MarketplaceListing, listing_id)
        if listing is None:
            raise ValueError(f"Listing {listing_id} not found.")
        normalized = (decision or "").strip().lower()
        if normalized not in {"approved", "rejected", "pending"}:
            raise ValueError("Review decision must be one of: approved, rejected, pending.")

        updates: dict[str, Any] = {
            "review_status": normalized,
            "reviewed_at": utcnow_naive(),
            "reviewed_by": (actor or "system").strip() or "system",
        }
        if normalized in {"rejected", "pending"} and listing.listing_status == "active":
            updates["listing_status"] = "draft"

        details_raw = (listing.marketplace_details or "").strip()
        details_obj: dict[str, Any] = {}
        if details_raw:
            try:
                parsed = json.loads(details_raw)
                if isinstance(parsed, dict):
                    details_obj = parsed
                else:
                    details_obj = {"notes": details_raw}
            except Exception:
                details_obj = {"notes": details_raw}
        details_obj["review"] = {
            "decision": normalized,
            "actor": updates["reviewed_by"],
            "reviewed_at": updates["reviewed_at"].isoformat(),
            "notes": (notes or "").strip(),
        }
        review_history = details_obj.get("review_history")
        if not isinstance(review_history, list):
            review_history = []
        review_history.append(
            {
                "decision": normalized,
                "actor": updates["reviewed_by"],
                "reviewed_at": updates["reviewed_at"].isoformat(),
                "notes": (notes or "").strip(),
            }
        )
        # Keep history bounded for row-size sanity while retaining meaningful audit context.
        details_obj["review_history"] = review_history[-100:]
        updates["marketplace_details"] = json.dumps(details_obj, indent=2)
        return self.update_listing(listing_id, updates, actor=actor)

    def update_sale(self, sale_id: int, updates: dict[str, Any], actor: str = "system") -> Sale:
        sale = self.db.get(Sale, sale_id)
        if sale is None:
            raise ValueError(f"Sale {sale_id} not found.")

        new_marketplace = updates.get("marketplace", sale.marketplace)
        new_quantity = updates.get("quantity_sold", sale.quantity_sold)
        new_sold_price = updates.get("sold_price", sale.sold_price)
        new_fees = updates.get("fees", sale.fees)
        new_shipping_cost = updates.get("shipping_cost", sale.shipping_cost)
        new_tracking_number = updates.get("tracking_number", sale.tracking_number)
        new_tracking_status = updates.get("tracking_status", sale.tracking_status)
        new_external_order_id = updates.get("external_order_id", sale.external_order_id)
        new_shipped_at = updates.get("shipped_at", sale.shipped_at)
        new_delivered_at = updates.get("delivered_at", sale.delivered_at)

        ValidationService.require_non_empty("Marketplace", new_marketplace)
        ValidationService.require_positive_int("Quantity sold", new_quantity)
        ValidationService.require_non_negative_decimal("Sold price", new_sold_price)
        ValidationService.require_non_negative_decimal("Fees", new_fees)
        ValidationService.require_non_negative_decimal("Shipping cost", new_shipping_cost)
        ValidationService.validate_sale_tracking_requirements(new_tracking_status, new_tracking_number)
        ValidationService.validate_shipping_dates(new_tracking_status, new_shipped_at, new_delivered_at)
        ValidationService.ensure_tracking_number_not_reused(
            self.db,
            new_tracking_number,
            new_external_order_id,
            exclude_sale_id=sale.id,
        )

        old_product_id = sale.product_id
        old_quantity = sale.quantity_sold
        new_product_id = updates.get("product_id", old_product_id)
        new_quantity = updates.get("quantity_sold", old_quantity)
        old_listing_id = sale.listing_id
        new_listing_id = updates.get("listing_id", old_listing_id)

        changes: dict[str, dict[str, Any]] = {}
        for field, new_value in updates.items():
            if not hasattr(sale, field):
                continue
            old_value = getattr(sale, field)
            if old_value != new_value:
                changes[field] = {
                    "before": self._serialize_audit_value(old_value),
                    "after": self._serialize_audit_value(new_value),
                }
                setattr(sale, field, new_value)

        if changes:
            inventory_fields = {"product_id", "quantity_sold", "listing_id"}
            inventory_changed = any(field in changes for field in inventory_fields)
            if inventory_changed:
                old_bundle_components = self._listing_bundle_sale_components(old_listing_id, old_quantity)
                new_bundle_components = self._listing_bundle_sale_components(new_listing_id, new_quantity)
                if old_bundle_components:
                    for component in old_bundle_components:
                        old_product = self.db.get(Product, int(component["product_id"]))
                        if old_product:
                            before = int(old_product.current_quantity)
                            after = before + max(1, int(component.get("quantity_total") or 1))
                            old_product.current_quantity = after
                            self._record_inventory_movement(
                                product_id=old_product.id,
                                movement_type="sale_bundle_component_adjustment_revert",
                                quantity_before=before,
                                quantity_after=after,
                                unit_cost=old_product.acquisition_cost,
                                reference_type="sale",
                                reference_id=sale.id,
                                notes=f"Reverted prior bundle sale component quantity due to sale update by {actor}.",
                                occurred_at=utcnow_naive(),
                            )
                elif old_product_id is not None:
                    old_product = self.db.get(Product, old_product_id)
                    if old_product:
                        before = int(old_product.current_quantity)
                        after = before + int(old_quantity)
                        old_product.current_quantity = after
                        self._record_inventory_movement(
                            product_id=old_product.id,
                            movement_type="sale_adjustment_revert",
                            quantity_before=before,
                            quantity_after=after,
                            unit_cost=old_product.acquisition_cost,
                            reference_type="sale",
                            reference_id=sale.id,
                            notes=f"Reverted prior sale quantity due to sale update by {actor}.",
                            occurred_at=utcnow_naive(),
                        )

                if new_bundle_components:
                    for component in new_bundle_components:
                        new_product = self.db.get(Product, int(component["product_id"]))
                        if new_product:
                            before = int(new_product.current_quantity)
                            after = max(0, before - max(1, int(component.get("quantity_total") or 1)))
                            new_product.current_quantity = after
                            self._record_inventory_movement(
                                product_id=new_product.id,
                                movement_type="sale_bundle_component_adjustment_apply",
                                quantity_before=before,
                                quantity_after=after,
                                unit_cost=new_product.acquisition_cost,
                                reference_type="sale",
                                reference_id=sale.id,
                                notes=f"Applied updated bundle sale component quantity due to sale update by {actor}.",
                                occurred_at=utcnow_naive(),
                            )
                elif new_product_id is not None:
                    new_product = self.db.get(Product, int(new_product_id))
                    if new_product:
                        before = int(new_product.current_quantity)
                        after = max(0, before - int(new_quantity))
                        new_product.current_quantity = after
                        self._record_inventory_movement(
                            product_id=new_product.id,
                            movement_type="sale_adjustment_apply",
                            quantity_before=before,
                            quantity_after=after,
                            unit_cost=new_product.acquisition_cost,
                            reference_type="sale",
                            reference_id=sale.id,
                            notes=f"Applied updated sale quantity due to sale update by {actor}.",
                            occurred_at=utcnow_naive(),
                        )

            self._record_audit("sale", sale.id, "update", actor, changes)
            self.db.commit()
            self.db.refresh(sale)
        return sale

    def update_media_asset(
        self, media_id: int, updates: dict[str, Any], actor: str = "system"
    ) -> MediaAsset:
        media = self.db.get(MediaAsset, media_id)
        if media is None:
            raise ValueError(f"Media asset {media_id} not found.")

        changes: dict[str, dict[str, Any]] = {}
        for field, new_value in updates.items():
            if not hasattr(media, field):
                continue
            old_value = getattr(media, field)
            if old_value != new_value:
                changes[field] = {
                    "before": self._serialize_audit_value(old_value),
                    "after": self._serialize_audit_value(new_value),
                }
                setattr(media, field, new_value)

        if changes:
            self._record_audit("media_asset", media.id, "update", actor, changes)
            self.db.commit()
            self.db.refresh(media)
        return media

    def bulk_update_media_assets(
        self,
        media_ids: list[int],
        updates: dict[str, Any],
        actor: str = "system",
    ) -> dict[str, list[int]]:
        normalized_ids = sorted({int(v) for v in (media_ids or []) if int(v) > 0})
        if not normalized_ids:
            return {"updated_ids": [], "missing_ids": []}
        rows = self.db.scalars(
            select(MediaAsset).where(MediaAsset.id.in_(normalized_ids))
        ).all()
        row_by_id = {int(row.id): row for row in rows}
        missing_ids = [mid for mid in normalized_ids if mid not in row_by_id]
        updated_ids: list[int] = []
        for media_id in normalized_ids:
            media = row_by_id.get(media_id)
            if media is None:
                continue
            changes: dict[str, dict[str, Any]] = {}
            for field, new_value in updates.items():
                if not hasattr(media, field):
                    continue
                old_value = getattr(media, field)
                if old_value != new_value:
                    changes[field] = {
                        "before": self._serialize_audit_value(old_value),
                        "after": self._serialize_audit_value(new_value),
                    }
                    setattr(media, field, new_value)
            if changes:
                self._record_audit("media_asset", media.id, "update", actor, changes)
                updated_ids.append(int(media.id))
        if updated_ids:
            self.db.commit()
        return {"updated_ids": updated_ids, "missing_ids": missing_ids}

    def delete_media_asset(self, media_id: int, actor: str = "system") -> bool:
        media = self.db.get(MediaAsset, int(media_id))
        if media is None:
            return False
        snapshot = {
            "id": int(media.id),
            "product_id": media.product_id,
            "listing_id": media.listing_id,
            "media_type": str(media.media_type or "").strip(),
            "filename": str(media.original_filename or "").strip(),
            "s3_bucket": str(media.s3_bucket or "").strip(),
            "s3_key": str(media.s3_key or "").strip(),
            "s3_url": str(media.s3_url or "").strip(),
        }
        self.db.delete(media)
        self._record_audit(
            "media_asset",
            int(snapshot["id"]),
            "delete",
            actor,
            {"before": snapshot},
        )
        self.db.commit()
        return True

    def get_media_asset_archive_blockers(self, media_id: int) -> dict[str, int]:
        media = self.db.get(MediaAsset, int(media_id))
        if media is None:
            raise ValueError(f"Media asset {media_id} not found.")
        linked_listing_active = 0
        if media.listing_id is not None:
            linked_listing_active = int(
                self.db.scalar(
                    select(func.count())
                    .select_from(MarketplaceListing)
                    .where(
                        MarketplaceListing.id == int(media.listing_id),
                        func.lower(func.trim(func.coalesce(cast(MarketplaceListing.listing_status, String), "")))
                        == "active",
                    )
                )
                or 0
            )
        linked_product_active_listings = 0
        if media.product_id is not None:
            linked_product_active_listings = int(
                self.db.scalar(
                    select(func.count())
                    .select_from(MarketplaceListing)
                    .where(
                        MarketplaceListing.product_id == int(media.product_id),
                        func.lower(func.trim(func.coalesce(cast(MarketplaceListing.listing_status, String), "")))
                        == "active",
                    )
                )
                or 0
            )
        return {
            "linked_listing_active": int(linked_listing_active),
            "linked_product_active_listings": int(linked_product_active_listings),
        }

    def archive_media_asset(self, media_id: int, *, actor: str = "system", force: bool = False) -> MediaAsset:
        media = self.db.get(MediaAsset, int(media_id))
        if media is None:
            raise ValueError(f"Media asset {media_id} not found.")
        if media.is_archived:
            return media
        blockers = self.get_media_asset_archive_blockers(int(media.id))
        has_blockers = any(int(v or 0) > 0 for v in blockers.values())
        if has_blockers and not bool(force):
            raise ValueError(
                "Cannot archive media linked to active listing context. "
                "Use force=True to confirm archive despite active links."
            )
        media.is_archived = True
        self._record_audit(
            "media_asset",
            media.id,
            "archive",
            actor,
            {
                "is_archived": {
                    "before": False,
                    "after": True,
                },
                "force": {"before": False, "after": bool(force)},
                "blockers": {"before": None, "after": blockers},
            },
        )
        self.db.commit()
        self.db.refresh(media)
        return media

    def restore_media_asset(self, media_id: int, *, actor: str = "system") -> MediaAsset:
        media = self.db.get(MediaAsset, int(media_id))
        if media is None:
            raise ValueError(f"Media asset {media_id} not found.")
        if not media.is_archived:
            return media
        media.is_archived = False
        self._record_audit(
            "media_asset",
            media.id,
            "restore",
            actor,
            {
                "is_archived": {
                    "before": True,
                    "after": False,
                }
            },
        )
        self.db.commit()
        self.db.refresh(media)
        return media

    def list_audit_logs(self, limit: int = 200) -> list[AuditLog]:
        return self.db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)).all()

    def record_audit_event(
        self,
        *,
        entity_type: str,
        entity_id: int | None,
        action: str,
        actor: str = "system",
        changes: dict[str, Any] | None = None,
    ) -> AuditLog:
        payload = changes or {}
        self._record_audit(
            entity_type=(entity_type or "").strip().lower(),
            entity_id=entity_id,
            action=(action or "").strip() or "note",
            actor=actor,
            changes=payload,
        )
        self.db.commit()
        row = self.db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(1)).first()
        if row is None:
            raise RuntimeError("Failed to persist audit event.")
        return row

    def list_audit_logs_for_entity(
        self,
        *,
        entity_type: str,
        entity_id: int | str,
        limit: int = 200,
    ) -> list[AuditLog]:
        query = (
            select(AuditLog)
            .where(
                AuditLog.entity_type == (entity_type or "").strip().lower(),
                AuditLog.entity_id == int(entity_id),
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(max(1, int(limit)))
        )
        return self.db.scalars(query).all()

    def list_app_users(self, active_only: bool = False) -> list[AppUser]:
        query = select(AppUser)
        if active_only:
            query = query.where(AppUser.is_active.is_(True))
        return self.db.scalars(query.order_by(AppUser.username.asc())).all()

    def upsert_app_user(
        self,
        *,
        username: str,
        role: str,
        display_name: str = "",
        email: str = "",
        password: str = "",
        is_active: bool = True,
        actor: str = "system",
    ) -> AppUser:
        resolved_username = (username or "").strip()
        if not resolved_username:
            raise ValueError("Username is required.")
        existing = self.db.scalar(select(AppUser).where(AppUser.username == resolved_username))
        if existing is None:
            resolved_password = (password or "").strip()
            if not resolved_password:
                raise ValueError("Password is required when creating a new user.")
            password_hash = ""
            password_salt = ""
            password_updated_at = None
            if resolved_password:
                password_hash, password_salt = hash_password(resolved_password)
                password_updated_at = utcnow_naive()
            row = AppUser(
                username=resolved_username,
                role=(role or "viewer").strip().lower(),
                display_name=display_name.strip(),
                email=email.strip(),
                password_hash=password_hash,
                password_salt=password_salt,
                password_updated_at=password_updated_at,
                is_active=bool(is_active),
            )
            self.db.add(row)
            self.db.flush()
            self._record_audit(
                "app_user",
                row.id,
                "create",
                actor,
                {
                    "after": {
                        "username": row.username,
                        "role": row.role,
                        "is_active": row.is_active,
                        "password_set": bool(row.password_hash),
                    }
                },
            )
            self.db.commit()
            self.db.refresh(row)
            return row

        changes: dict[str, dict[str, Any]] = {}
        updates = {
            "role": (role or "viewer").strip().lower(),
            "display_name": display_name.strip(),
            "email": email.strip(),
            "is_active": bool(is_active),
        }
        for field, new_value in updates.items():
            old_value = getattr(existing, field)
            if old_value != new_value:
                changes[field] = {
                    "before": self._serialize_audit_value(old_value),
                    "after": self._serialize_audit_value(new_value),
                }
                setattr(existing, field, new_value)
        if (password or "").strip():
            new_hash, new_salt = hash_password(password)
            existing.password_hash = new_hash
            existing.password_salt = new_salt
            existing.password_updated_at = utcnow_naive()
            changes["password"] = {"before": "<redacted>", "after": "<updated>"}
        if changes:
            self._record_audit("app_user", existing.id, "update", actor, changes)
            self.db.commit()
            self.db.refresh(existing)
        return existing

    def update_app_user(self, user_id: int, updates: dict[str, Any], actor: str = "system") -> AppUser:
        row = self.db.get(AppUser, user_id)
        if row is None:
            raise ValueError(f"App user {user_id} not found.")
        changes: dict[str, dict[str, Any]] = {}
        for field, new_value in updates.items():
            if not hasattr(row, field):
                continue
            old_value = getattr(row, field)
            if old_value != new_value:
                changes[field] = {
                    "before": self._serialize_audit_value(old_value),
                    "after": self._serialize_audit_value(new_value),
                }
                setattr(row, field, new_value)
        if changes:
            self._record_audit("app_user", row.id, "update", actor, changes)
            self.db.commit()
            self.db.refresh(row)
        return row

    def set_app_user_password(self, user_id: int, password: str, actor: str = "system") -> AppUser:
        row = self.db.get(AppUser, user_id)
        if row is None:
            raise ValueError(f"App user {user_id} not found.")
        new_hash, new_salt = hash_password(password)
        row.password_hash = new_hash
        row.password_salt = new_salt
        row.password_updated_at = utcnow_naive()
        self._record_audit(
            "app_user",
            row.id,
            "update",
            actor,
            {"password": {"before": "<redacted>", "after": "<updated>"}},
        )
        self.db.commit()
        self.db.refresh(row)
        return row

    def authenticate_app_user(self, username: str, password: str) -> AppUser | None:
        resolved_username = (username or "").strip()
        if not resolved_username:
            return None
        row = self.db.scalar(
            select(AppUser).where(
                AppUser.username == resolved_username,
                AppUser.is_active.is_(True),
            )
        )
        if row is None:
            return None
        if not verify_password(password, row.password_hash, row.password_salt):
            return None
        return row

    def list_role_permissions(self) -> dict[str, set[str]]:
        rows = self.db.scalars(select(RolePermission).order_by(RolePermission.role.asc())).all()
        result: dict[str, set[str]] = {}
        for row in rows:
            result.setdefault(row.role, set()).add(row.permission)
        return result

    def set_role_permissions(self, role: str, permissions: set[str], actor: str = "system") -> None:
        resolved_role = (role or "").strip().lower()
        if not resolved_role:
            raise ValueError("Role is required.")
        desired = {p.strip() for p in permissions if p and p.strip()}
        current_rows = self.db.scalars(select(RolePermission).where(RolePermission.role == resolved_role)).all()
        current = {row.permission for row in current_rows}
        if current == desired:
            return

        self.db.execute(delete(RolePermission).where(RolePermission.role == resolved_role))
        for perm in sorted(desired):
            self.db.add(
                RolePermission(
                    role=resolved_role,
                    permission=perm,
                    created_at=utcnow_naive(),
                    updated_at=utcnow_naive(),
                )
            )

        self._record_audit(
            "role_permission",
            None,
            "update",
            actor,
            {
                "role": resolved_role,
                "before": sorted(current),
                "after": sorted(desired),
            },
        )
        self.db.commit()

    def create_sync_run(
        self,
        *,
        provider: str,
        job_name: str,
        direction: str = "pull",
        status: str = "queued",
        retry_of_run_id: int | None = None,
        retry_count: int = 0,
        line_items_with_listing_link: int = 0,
        line_items_unmapped_sku: int = 0,
        auto_listings_created: int = 0,
        notes: str = "",
        actor: str = "system",
    ) -> SyncRun:
        run = SyncRun(
            retry_of_run_id=retry_of_run_id,
            retry_count=max(0, int(retry_count)),
            provider=(provider or "").strip().lower(),
            job_name=(job_name or "").strip(),
            direction=(direction or "pull").strip().lower(),
            status=(status or "queued").strip().lower(),
            line_items_with_listing_link=max(0, int(line_items_with_listing_link)),
            line_items_unmapped_sku=max(0, int(line_items_unmapped_sku)),
            auto_listings_created=max(0, int(auto_listings_created)),
            notes=(notes or "").strip(),
            started_at=utcnow_naive(),
        )
        self.db.add(run)
        self.db.flush()
        self._record_audit(
            "sync_run",
            run.id,
            "create",
            actor,
            {
                "after": {
                    "provider": run.provider,
                    "job_name": run.job_name,
                    "status": run.status,
                    "retry_of_run_id": run.retry_of_run_id,
                    "retry_count": run.retry_count,
                    "line_items_with_listing_link": run.line_items_with_listing_link,
                    "line_items_unmapped_sku": run.line_items_unmapped_sku,
                    "auto_listings_created": run.auto_listings_created,
                }
            },
        )
        self.db.commit()
        self.db.refresh(run)
        return run

    def update_sync_run(
        self,
        sync_run_id: int,
        updates: dict[str, Any],
        actor: str = "system",
    ) -> SyncRun:
        row = self.db.get(SyncRun, sync_run_id)
        if row is None:
            raise ValueError(f"Sync run {sync_run_id} not found.")

        changes: dict[str, dict[str, Any]] = {}
        for field, new_value in updates.items():
            if not hasattr(row, field):
                continue
            old_value = getattr(row, field)
            if old_value != new_value:
                changes[field] = {
                    "before": self._serialize_audit_value(old_value),
                    "after": self._serialize_audit_value(new_value),
                }
                setattr(row, field, new_value)

        if changes:
            self._record_audit("sync_run", row.id, "update", actor, changes)
            self.db.commit()
            self.db.refresh(row)
        return row

    def list_sync_runs(self, provider: str | None = None, limit: int = 200) -> list[SyncRun]:
        query = select(SyncRun)
        if provider:
            query = query.where(SyncRun.provider == provider.strip().lower())
        query = query.order_by(SyncRun.started_at.desc(), SyncRun.id.desc()).limit(max(1, int(limit)))
        return self.db.scalars(query).all()

    def retry_sync_run(self, source_run_id: int, actor: str = "system") -> SyncRun:
        source = self.db.get(SyncRun, source_run_id)
        if source is None:
            raise ValueError(f"Sync run {source_run_id} not found.")
        if source.status not in {"failed", "partial"}:
            raise ValueError("Only failed/partial sync runs can be retried.")

        retry_run = self.create_sync_run(
            provider=source.provider,
            job_name=source.job_name,
            direction=source.direction,
            status="queued",
            retry_of_run_id=source.id,
            retry_count=int(source.retry_count or 0) + 1,
            notes=f"Retry of sync run #{source.id}.",
            actor=actor,
        )
        return retry_run

    def create_integration_automation_rule(
        self,
        *,
        environment: str,
        integration: str,
        action: str,
        name: str,
        trigger_status: str,
        conditions_json: str,
        effect_json: str,
        requires_approval: bool = True,
        is_active: bool = True,
        actor: str = "system",
    ) -> IntegrationAutomationRule:
        resolved_env = (environment or settings.app_env or "local").strip().lower()
        resolved_integration = (integration or "").strip().lower()
        resolved_action = (action or "").strip().lower()
        resolved_name = (name or "").strip()
        resolved_trigger_status = (trigger_status or "queued").strip().lower()
        if not resolved_integration:
            raise ValueError("Integration is required.")
        if not resolved_action:
            raise ValueError("Action is required.")
        if not resolved_name:
            raise ValueError("Rule name is required.")
        try:
            json.loads((conditions_json or "{}").strip() or "{}")
            json.loads((effect_json or "{}").strip() or "{}")
        except Exception as exc:
            raise ValueError(f"Rule JSON must be valid JSON: {exc}") from exc

        row = IntegrationAutomationRule(
            environment=resolved_env,
            integration=resolved_integration,
            action=resolved_action,
            name=resolved_name,
            trigger_status=resolved_trigger_status,
            conditions_json=(conditions_json or "{}").strip() or "{}",
            effect_json=(effect_json or "{}").strip() or "{}",
            requires_approval=bool(requires_approval),
            is_active=bool(is_active),
            created_by=(actor or "system").strip() or "system",
            updated_by=(actor or "system").strip() or "system",
        )
        self.db.add(row)
        self.db.flush()
        self._record_audit(
            "integration_automation_rule",
            row.id,
            "create",
            actor,
            {
                "after": {
                    "environment": row.environment,
                    "integration": row.integration,
                    "action": row.action,
                    "name": row.name,
                    "trigger_status": row.trigger_status,
                    "requires_approval": row.requires_approval,
                    "is_active": row.is_active,
                }
            },
        )
        self.db.commit()
        self.db.refresh(row)
        return row

    def list_integration_automation_rules(
        self,
        *,
        environment: str,
        integration: str | None = None,
        action: str | None = None,
        active_only: bool = False,
        limit: int = 500,
    ) -> list[IntegrationAutomationRule]:
        query = select(IntegrationAutomationRule).where(
            IntegrationAutomationRule.environment == (environment or settings.app_env or "local").strip().lower()
        )
        if integration and integration.strip():
            query = query.where(IntegrationAutomationRule.integration == integration.strip().lower())
        if action and action.strip():
            query = query.where(IntegrationAutomationRule.action == action.strip().lower())
        if active_only:
            query = query.where(IntegrationAutomationRule.is_active.is_(True))
        query = query.order_by(
            IntegrationAutomationRule.integration.asc(),
            IntegrationAutomationRule.action.asc(),
            IntegrationAutomationRule.name.asc(),
        ).limit(max(1, int(limit)))
        return self.db.scalars(query).all()

    def update_integration_automation_rule(
        self,
        rule_id: int,
        updates: dict[str, Any],
        *,
        actor: str = "system",
    ) -> IntegrationAutomationRule:
        row = self.db.get(IntegrationAutomationRule, int(rule_id))
        if row is None:
            raise ValueError(f"Integration automation rule {rule_id} not found.")
        if "conditions_json" in updates:
            try:
                json.loads((updates.get("conditions_json") or "{}").strip() or "{}")
            except Exception as exc:
                raise ValueError(f"conditions_json must be valid JSON: {exc}") from exc
        if "effect_json" in updates:
            try:
                json.loads((updates.get("effect_json") or "{}").strip() or "{}")
            except Exception as exc:
                raise ValueError(f"effect_json must be valid JSON: {exc}") from exc

        changes: dict[str, dict[str, Any]] = {}
        for field, new_value in updates.items():
            if not hasattr(row, field):
                continue
            old_value = getattr(row, field)
            if old_value != new_value:
                changes[field] = {
                    "before": self._serialize_audit_value(old_value),
                    "after": self._serialize_audit_value(new_value),
                }
                setattr(row, field, new_value)
        if changes:
            row.updated_by = (actor or "system").strip() or "system"
            self._record_audit("integration_automation_rule", row.id, "update", actor, changes)
            self.db.commit()
            self.db.refresh(row)
        return row

    def delete_integration_automation_rule(
        self,
        *,
        rule_id: int,
        actor: str = "system",
    ) -> bool:
        row = self.db.get(IntegrationAutomationRule, int(rule_id))
        if row is None:
            return False
        self.db.delete(row)
        self._record_audit(
            "integration_automation_rule",
            int(rule_id),
            "delete",
            actor,
            {
                "integration": row.integration,
                "action": row.action,
                "name": row.name,
            },
        )
        self.db.commit()
        return True

    def create_integration_automation_approval(
        self,
        *,
        environment: str,
        rule_id: int,
        queue_job_id: int | None = None,
        notes: str = "",
        approved_by: str = "system",
        approved_at: datetime | None = None,
        expires_at: datetime | None = None,
        actor: str = "system",
    ) -> IntegrationAutomationApproval:
        rule = self.db.get(IntegrationAutomationRule, int(rule_id))
        if rule is None:
            raise ValueError(f"Integration automation rule {rule_id} not found.")
        if queue_job_id is not None:
            queue_row = self.db.get(IntegrationQueueJob, int(queue_job_id))
            if queue_row is None:
                raise ValueError(f"Integration queue job {queue_job_id} not found.")
        row = IntegrationAutomationApproval(
            environment=(environment or settings.app_env or "local").strip().lower(),
            rule_id=int(rule_id),
            queue_job_id=int(queue_job_id) if queue_job_id is not None else None,
            status="approved",
            notes=(notes or "").strip(),
            approved_by=(approved_by or actor or "system").strip() or "system",
            approved_at=approved_at or utcnow_naive(),
            expires_at=expires_at,
            is_active=True,
        )
        self.db.add(row)
        self.db.flush()
        self._record_audit(
            "integration_automation_approval",
            row.id,
            "create",
            actor,
            {
                "after": {
                    "environment": row.environment,
                    "rule_id": row.rule_id,
                    "queue_job_id": row.queue_job_id,
                    "status": row.status,
                    "approved_by": row.approved_by,
                    "approved_at": self._serialize_audit_value(row.approved_at),
                    "expires_at": self._serialize_audit_value(row.expires_at),
                    "is_active": row.is_active,
                }
            },
        )
        self.db.commit()
        self.db.refresh(row)
        return row

    def list_integration_automation_approvals(
        self,
        *,
        environment: str,
        rule_id: int | None = None,
        queue_job_id: int | None = None,
        active_only: bool = False,
        limit: int = 500,
    ) -> list[IntegrationAutomationApproval]:
        query = select(IntegrationAutomationApproval).where(
            IntegrationAutomationApproval.environment == (environment or settings.app_env or "local").strip().lower()
        )
        if rule_id is not None:
            query = query.where(IntegrationAutomationApproval.rule_id == int(rule_id))
        if queue_job_id is not None:
            query = query.where(IntegrationAutomationApproval.queue_job_id == int(queue_job_id))
        if active_only:
            query = query.where(
                IntegrationAutomationApproval.is_active.is_(True),
                IntegrationAutomationApproval.status == "approved",
            )
        query = query.order_by(
            IntegrationAutomationApproval.approved_at.desc(),
            IntegrationAutomationApproval.id.desc(),
        ).limit(max(1, int(limit)))
        return self.db.scalars(query).all()

    def has_active_integration_automation_approval(
        self,
        *,
        environment: str,
        rule_id: int,
        queue_job_id: int | None = None,
        as_of: datetime | None = None,
    ) -> bool:
        now = as_of or utcnow_naive()
        query = select(IntegrationAutomationApproval).where(
            IntegrationAutomationApproval.environment == (environment or settings.app_env or "local").strip().lower(),
            IntegrationAutomationApproval.rule_id == int(rule_id),
            IntegrationAutomationApproval.is_active.is_(True),
            IntegrationAutomationApproval.status == "approved",
            IntegrationAutomationApproval.approved_at <= now,
            or_(
                IntegrationAutomationApproval.expires_at.is_(None),
                IntegrationAutomationApproval.expires_at > now,
            ),
        )
        if queue_job_id is not None:
            query = query.where(
                or_(
                    IntegrationAutomationApproval.queue_job_id.is_(None),
                    IntegrationAutomationApproval.queue_job_id == int(queue_job_id),
                )
            )
        row = self.db.scalar(query.limit(1))
        return row is not None

    def revoke_integration_automation_approval(
        self,
        *,
        approval_id: int,
        actor: str = "system",
    ) -> IntegrationAutomationApproval:
        row = self.db.get(IntegrationAutomationApproval, int(approval_id))
        if row is None:
            raise ValueError(f"Integration automation approval {approval_id} not found.")
        updates = {
            "status": "revoked",
            "is_active": False,
        }
        changes: dict[str, dict[str, Any]] = {}
        for field, value in updates.items():
            old_value = getattr(row, field)
            if old_value != value:
                changes[field] = {
                    "before": self._serialize_audit_value(old_value),
                    "after": self._serialize_audit_value(value),
                }
                setattr(row, field, value)
        if changes:
            self._record_audit("integration_automation_approval", row.id, "revoke", actor, changes)
            self.db.commit()
            self.db.refresh(row)
        return row

    def create_integration_queue_job(
        self,
        *,
        environment: str,
        integration: str,
        action: str,
        payload_json: str,
        requested_by: str,
        max_retries: int = 5,
        next_attempt_at: datetime | None = None,
        actor: str = "system",
    ) -> IntegrationQueueJob:
        row = IntegrationQueueJob(
            environment=(environment or settings.app_env or "local").strip().lower(),
            integration=(integration or "google").strip().lower(),
            action=(action or "").strip(),
            status="queued",
            payload_json=(payload_json or "{}").strip() or "{}",
            retry_count=0,
            max_retries=max(0, int(max_retries)),
            next_attempt_at=next_attempt_at or utcnow_naive(),
            requested_by=(requested_by or actor or "system").strip() or "system",
            updated_by=(actor or "system").strip() or "system",
            last_error="",
        )
        self.db.add(row)
        self.db.flush()
        self._record_audit(
            "integration_queue_job",
            row.id,
            "create",
            actor,
            {
                "after": {
                    "environment": row.environment,
                    "integration": row.integration,
                    "action": row.action,
                    "status": row.status,
                    "max_retries": row.max_retries,
                    "next_attempt_at": self._serialize_audit_value(row.next_attempt_at),
                }
            },
        )
        self.db.commit()
        self.db.refresh(row)
        return row

    def list_integration_queue_jobs(
        self,
        *,
        environment: str,
        integration: str | None = None,
        statuses: set[str] | None = None,
        limit: int = 200,
    ) -> list[IntegrationQueueJob]:
        query = select(IntegrationQueueJob).where(
            IntegrationQueueJob.environment == (environment or settings.app_env or "local").strip().lower()
        )
        if integration and integration.strip():
            query = query.where(IntegrationQueueJob.integration == integration.strip().lower())
        if statuses:
            normalized = {str(s).strip().lower() for s in statuses if str(s).strip()}
            if normalized:
                query = query.where(IntegrationQueueJob.status.in_(sorted(normalized)))
        query = query.order_by(
            IntegrationQueueJob.next_attempt_at.asc(),
            IntegrationQueueJob.id.desc(),
        ).limit(max(1, int(limit)))
        return self.db.scalars(query).all()

    def update_integration_queue_job(
        self,
        job_id: int,
        updates: dict[str, Any],
        actor: str = "system",
    ) -> IntegrationQueueJob:
        row = self.db.get(IntegrationQueueJob, int(job_id))
        if row is None:
            raise ValueError(f"Integration queue job {job_id} not found.")

        changes: dict[str, dict[str, Any]] = {}
        for field, new_value in updates.items():
            if not hasattr(row, field):
                continue
            old_value = getattr(row, field)
            if old_value != new_value:
                changes[field] = {
                    "before": self._serialize_audit_value(old_value),
                    "after": self._serialize_audit_value(new_value),
                }
                setattr(row, field, new_value)
        if changes:
            row.updated_by = (actor or "system").strip() or "system"
            self._record_audit("integration_queue_job", row.id, "update", actor, changes)
            self.db.commit()
            self.db.refresh(row)
        return row

    def enqueue_notification_outbox(
        self,
        *,
        environment: str,
        channel: str,
        event_type: str,
        payload_json: str,
        requested_by: str,
        entity_type: str = "",
        entity_id: str = "",
        dedupe_key: str = "",
        next_attempt_at: datetime | None = None,
        max_attempts: int = 6,
        actor: str = "system",
    ) -> NotificationOutbox:
        normalized_env = (environment or settings.app_env or "local").strip().lower()
        normalized_channel = (channel or "slack").strip().lower()
        normalized_event_type = (event_type or "").strip().lower()
        normalized_entity_type = (entity_type or "").strip().lower()
        normalized_entity_id = (entity_id or "").strip()
        normalized_dedupe_key = (dedupe_key or "").strip()
        if normalized_dedupe_key:
            existing = self.db.scalar(
                select(NotificationOutbox).where(
                    NotificationOutbox.environment == normalized_env,
                    NotificationOutbox.channel == normalized_channel,
                    NotificationOutbox.dedupe_key == normalized_dedupe_key,
                    NotificationOutbox.status.in_(["queued", "retrying", "processing", "sent"]),
                )
            )
            if existing is not None:
                return existing
        row = NotificationOutbox(
            environment=normalized_env,
            channel=normalized_channel,
            event_type=normalized_event_type,
            entity_type=normalized_entity_type,
            entity_id=normalized_entity_id,
            dedupe_key=normalized_dedupe_key,
            status="queued",
            payload_json=(payload_json or "{}").strip() or "{}",
            attempt_count=0,
            max_attempts=max(1, int(max_attempts)),
            next_attempt_at=next_attempt_at or utcnow_naive(),
            requested_by=(requested_by or actor or "system").strip() or "system",
            updated_by=(actor or "system").strip() or "system",
            last_error="",
            locked_by="",
            locked_at=None,
            dispatched_at=None,
            last_attempt_at=None,
        )
        self.db.add(row)
        self.db.flush()
        self._record_audit(
            "notification_outbox",
            row.id,
            "create",
            actor,
            {
                "after": {
                    "environment": row.environment,
                    "channel": row.channel,
                    "event_type": row.event_type,
                    "entity_type": row.entity_type,
                    "entity_id": row.entity_id,
                    "status": row.status,
                    "next_attempt_at": self._serialize_audit_value(row.next_attempt_at),
                    "max_attempts": row.max_attempts,
                }
            },
        )
        self.db.commit()
        self.db.refresh(row)
        return row

    def get_notification_outbox(
        self,
        outbox_id: int,
        *,
        environment: str | None = None,
    ) -> NotificationOutbox | None:
        row = self.db.get(NotificationOutbox, int(outbox_id))
        if row is None:
            return None
        if environment and str(getattr(row, "environment", "") or "").strip().lower() != environment.strip().lower():
            return None
        return row

    def list_notification_outbox(
        self,
        *,
        environment: str,
        channel: str | None = None,
        statuses: set[str] | None = None,
        due_before: datetime | None = None,
        limit: int = 200,
    ) -> list[NotificationOutbox]:
        query = select(NotificationOutbox).where(
            NotificationOutbox.environment == (environment or settings.app_env or "local").strip().lower()
        )
        if channel and channel.strip():
            query = query.where(NotificationOutbox.channel == channel.strip().lower())
        if statuses:
            normalized = {str(s).strip().lower() for s in statuses if str(s).strip()}
            if normalized:
                query = query.where(NotificationOutbox.status.in_(sorted(normalized)))
        if due_before is not None:
            query = query.where(
                or_(
                    NotificationOutbox.next_attempt_at.is_(None),
                    NotificationOutbox.next_attempt_at <= due_before,
                )
            )
        query = query.order_by(
            NotificationOutbox.next_attempt_at.asc(),
            NotificationOutbox.id.asc(),
        ).limit(max(1, int(limit)))
        return self.db.scalars(query).all()

    def update_notification_outbox(
        self,
        outbox_id: int,
        updates: dict[str, Any],
        actor: str = "system",
    ) -> NotificationOutbox:
        row = self.db.get(NotificationOutbox, int(outbox_id))
        if row is None:
            raise ValueError(f"Notification outbox row {outbox_id} not found.")

        changes: dict[str, dict[str, Any]] = {}
        for field, new_value in updates.items():
            if not hasattr(row, field):
                continue
            old_value = getattr(row, field)
            if old_value != new_value:
                changes[field] = {
                    "before": self._serialize_audit_value(old_value),
                    "after": self._serialize_audit_value(new_value),
                }
                setattr(row, field, new_value)
        if changes:
            row.updated_by = (actor or "system").strip() or "system"
            self._record_audit("notification_outbox", row.id, "update", actor, changes)
            self.db.commit()
            self.db.refresh(row)
        return row

    def cleanup_notification_outbox(
        self,
        *,
        environment: str,
        retain_sent_days: int = 14,
        retain_failed_days: int = 30,
        actor: str = "system",
    ) -> dict[str, int]:
        env = (environment or settings.app_env or "local").strip().lower()
        sent_cutoff = utcnow_naive() - timedelta(days=max(1, int(retain_sent_days)))
        failed_cutoff = utcnow_naive() - timedelta(days=max(1, int(retain_failed_days)))

        sent_rows = self.db.scalars(
            select(NotificationOutbox).where(
                NotificationOutbox.environment == env,
                NotificationOutbox.status == "sent",
                NotificationOutbox.created_at < sent_cutoff,
            )
        ).all()
        failed_rows = self.db.scalars(
            select(NotificationOutbox).where(
                NotificationOutbox.environment == env,
                NotificationOutbox.status == "failed",
                NotificationOutbox.created_at < failed_cutoff,
            )
        ).all()
        sent_ids = [int(r.id) for r in sent_rows]
        failed_ids = [int(r.id) for r in failed_rows]

        if sent_ids:
            self.db.execute(delete(NotificationOutbox).where(NotificationOutbox.id.in_(sent_ids)))
        if failed_ids:
            self.db.execute(delete(NotificationOutbox).where(NotificationOutbox.id.in_(failed_ids)))

        result = {
            "deleted_sent": len(sent_ids),
            "deleted_failed": len(failed_ids),
            "deleted_total": len(sent_ids) + len(failed_ids),
        }
        self._record_audit(
            "notification_outbox",
            None,
            "cleanup",
            actor,
            {
                "after": {
                    "environment": env,
                    "retain_sent_days": int(retain_sent_days),
                    "retain_failed_days": int(retain_failed_days),
                    **result,
                }
            },
        )
        self.db.commit()
        return result

    def cleanup_archived_media_assets(
        self,
        *,
        retain_days: int = 180,
        actor: str = "system",
    ) -> dict[str, int]:
        cutoff = utcnow_naive() - timedelta(days=max(1, int(retain_days)))
        archived_rows = self.db.scalars(
            select(MediaAsset).where(
                MediaAsset.is_archived.is_(True),
                MediaAsset.updated_at <= cutoff,
            )
        ).all()
        archived_ids = [int(r.id) for r in archived_rows]
        if archived_ids:
            self.db.execute(delete(MediaAsset).where(MediaAsset.id.in_(archived_ids)))
        result = {
            "deleted_archived_media": len(archived_ids),
        }
        self._record_audit(
            "media_asset",
            None,
            "retention_cleanup",
            actor,
            {
                "after": {
                    "retain_days": int(retain_days),
                    "cutoff": cutoff.isoformat(timespec="seconds"),
                    **result,
                }
            },
        )
        self.db.commit()
        return result

    def cleanup_archived_listings(
        self,
        *,
        retain_days: int = 365,
        actor: str = "system",
    ) -> dict[str, int]:
        cutoff = utcnow_naive() - timedelta(days=max(1, int(retain_days)))
        candidates = self.db.scalars(
            select(MarketplaceListing).where(
                MarketplaceListing.updated_at <= cutoff,
            )
        ).all()
        deleted = 0
        skipped_with_dependencies = 0
        for row in candidates:
            if not self._listing_is_archived(row):
                continue
            listing_id = int(row.id)
            has_dependencies = bool(
                self.db.scalar(
                    select(func.count()).select_from(Sale).where(Sale.listing_id == listing_id)
                )
                or 0
            ) or bool(
                self.db.scalar(
                    select(func.count()).select_from(OrderItem).where(OrderItem.listing_id == listing_id)
                )
                or 0
            ) or bool(
                self.db.scalar(
                    select(func.count()).select_from(MediaAsset).where(MediaAsset.listing_id == listing_id)
                )
                or 0
            )
            if has_dependencies:
                skipped_with_dependencies += 1
                continue
            self.db.delete(row)
            deleted += 1

        result = {
            "deleted_archived_listings": int(deleted),
            "skipped_listings_with_dependencies": int(skipped_with_dependencies),
        }
        self._record_audit(
            "listing",
            None,
            "retention_cleanup",
            actor,
            {
                "after": {
                    "retain_days": int(retain_days),
                    "cutoff": cutoff.isoformat(timespec="seconds"),
                    **result,
                }
            },
        )
        self.db.commit()
        return result

    def cleanup_archived_purchase_lots(
        self,
        *,
        retain_days: int = 365,
        actor: str = "system",
    ) -> dict[str, int]:
        cutoff = utcnow_naive() - timedelta(days=max(1, int(retain_days)))
        candidates = self.db.scalars(
            select(PurchaseLot).where(
                PurchaseLot.updated_at <= cutoff,
            )
        ).all()
        deleted = 0
        skipped_with_dependencies = 0
        for row in candidates:
            if not self._lot_is_archived(row):
                continue
            lot_id = int(row.id)
            has_dependencies = bool(
                self.db.scalar(
                    select(func.count()).select_from(ProductLotAssignment).where(ProductLotAssignment.lot_id == lot_id)
                )
                or 0
            ) or bool(
                self.db.scalar(
                    select(func.count()).select_from(PurchaseDocument).where(PurchaseDocument.lot_id == lot_id)
                )
                or 0
            )
            if has_dependencies:
                skipped_with_dependencies += 1
                continue
            self.db.delete(row)
            deleted += 1

        result = {
            "deleted_archived_lots": int(deleted),
            "skipped_lots_with_dependencies": int(skipped_with_dependencies),
        }
        self._record_audit(
            "purchase_lot",
            None,
            "retention_cleanup",
            actor,
            {
                "after": {
                    "retain_days": int(retain_days),
                    "cutoff": cutoff.isoformat(timespec="seconds"),
                    **result,
                }
            },
        )
        self.db.commit()
        return result

    def cleanup_archived_products(
        self,
        *,
        retain_days: int = 365,
        actor: str = "system",
    ) -> dict[str, int]:
        cutoff = utcnow_naive() - timedelta(days=max(1, int(retain_days)))
        candidates = self.db.scalars(
            select(Product).where(
                Product.status == "archived",
                Product.updated_at <= cutoff,
            )
        ).all()
        deleted = 0
        skipped_with_dependencies = 0
        for row in candidates:
            product_id = int(row.id)
            has_dependencies = bool(
                self.db.scalar(
                    select(func.count()).select_from(MarketplaceListing).where(MarketplaceListing.product_id == product_id)
                )
                or 0
            ) or bool(
                self.db.scalar(
                    select(func.count()).select_from(Sale).where(Sale.product_id == product_id)
                )
                or 0
            ) or bool(
                self.db.scalar(
                    select(func.count()).select_from(OrderItem).where(OrderItem.product_id == product_id)
                )
                or 0
            ) or bool(
                self.db.scalar(
                    select(func.count()).select_from(ReturnRecord).where(ReturnRecord.product_id == product_id)
                )
                or 0
            ) or bool(
                self.db.scalar(
                    select(func.count()).select_from(ProductLotAssignment).where(ProductLotAssignment.product_id == product_id)
                )
                or 0
            ) or bool(
                self.db.scalar(
                    select(func.count()).select_from(MediaAsset).where(MediaAsset.product_id == product_id)
                )
                or 0
            ) or bool(
                self.db.scalar(
                    select(func.count()).select_from(PurchaseDocument).where(PurchaseDocument.product_id == product_id)
                )
                or 0
            ) or bool(
                self.db.scalar(
                    select(func.count()).select_from(CoinAIRun).where(CoinAIRun.product_id == product_id)
                )
                or 0
            )
            if has_dependencies:
                skipped_with_dependencies += 1
                continue
            self.db.delete(row)
            deleted += 1

        result = {
            "deleted_archived_products": int(deleted),
            "skipped_products_with_dependencies": int(skipped_with_dependencies),
        }
        self._record_audit(
            "product",
            None,
            "retention_cleanup",
            actor,
            {
                "after": {
                    "retain_days": int(retain_days),
                    "cutoff": cutoff.isoformat(timespec="seconds"),
                    **result,
                }
            },
        )
        self.db.commit()
        return result

    def add_sync_event(
        self,
        *,
        sync_run_id: int,
        entity_type: str,
        entity_id: str = "",
        action: str = "",
        status: str = "ok",
        message: str = "",
        payload_json: str = "{}",
    ) -> SyncEvent:
        row = SyncEvent(
            sync_run_id=sync_run_id,
            entity_type=(entity_type or "").strip(),
            entity_id=(entity_id or "").strip(),
            action=(action or "").strip(),
            status=(status or "ok").strip().lower(),
            message=(message or "").strip(),
            payload_json=(payload_json or "{}").strip() or "{}",
            created_at=utcnow_naive(),
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def add_sync_error(
        self,
        *,
        sync_run_id: int,
        code: str,
        message: str,
        severity: str = "error",
        context_json: str = "{}",
    ) -> SyncError:
        row = SyncError(
            sync_run_id=sync_run_id,
            severity=(severity or "error").strip().lower(),
            code=(code or "").strip(),
            message=(message or "").strip(),
            context_json=(context_json or "{}").strip() or "{}",
            occurred_at=utcnow_naive(),
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def list_sync_events(self, sync_run_id: int, limit: int = 500) -> list[SyncEvent]:
        query = (
            select(SyncEvent)
            .where(SyncEvent.sync_run_id == sync_run_id)
            .order_by(SyncEvent.created_at.asc(), SyncEvent.id.asc())
            .limit(max(1, int(limit)))
        )
        return self.db.scalars(query).all()

    def list_sync_errors(self, sync_run_id: int, limit: int = 500) -> list[SyncError]:
        query = (
            select(SyncError)
            .where(SyncError.sync_run_id == sync_run_id)
            .order_by(SyncError.occurred_at.asc(), SyncError.id.asc())
            .limit(max(1, int(limit)))
        )
        return self.db.scalars(query).all()

    def list_sync_error_queue(
        self,
        *,
        provider: str | None = None,
        unresolved_only: bool = True,
        limit: int = 500,
    ) -> list[tuple[SyncError, SyncRun]]:
        query = (
            select(SyncError, SyncRun)
            .join(SyncRun, SyncRun.id == SyncError.sync_run_id)
            .order_by(SyncError.occurred_at.desc(), SyncError.id.desc())
            .limit(max(1, int(limit)))
        )
        if unresolved_only:
            query = query.where(SyncError.resolved_at.is_(None))
        if provider:
            query = query.where(SyncRun.provider == provider.strip().lower())
        return list(self.db.execute(query).all())

    def resolve_sync_error(
        self,
        sync_error_id: int,
        *,
        actor: str = "system",
        resolved_at: datetime | None = None,
    ) -> SyncError:
        row = self.db.get(SyncError, sync_error_id)
        if row is None:
            raise ValueError(f"Sync error {sync_error_id} not found.")
        target_resolved_at = resolved_at or utcnow_naive()
        if row.resolved_at is None:
            row.resolved_at = target_resolved_at
            self._record_audit(
                "sync_error",
                row.id,
                "resolve",
                actor,
                {"resolved_at": {"before": None, "after": target_resolved_at.isoformat()}},
            )
            self.db.commit()
            self.db.refresh(row)
        return row

    def list_sync_events_for_entity(
        self,
        *,
        entity_type: str,
        entity_id: int | str,
        limit: int = 500,
    ) -> list[SyncEvent]:
        query = (
            select(SyncEvent)
            .where(
                SyncEvent.entity_type == (entity_type or "").strip().lower(),
                SyncEvent.entity_id == str(entity_id).strip(),
            )
            .order_by(SyncEvent.created_at.desc(), SyncEvent.id.desc())
            .limit(max(1, int(limit)))
        )
        return self.db.scalars(query).all()

    def dashboard_metrics(self) -> dict:
        landed_unit_cost_expr = (
            func.coalesce(Product.acquisition_cost, 0)
            + func.coalesce(Product.acquisition_tax_paid, 0)
            + func.coalesce(Product.acquisition_shipping_paid, 0)
            + func.coalesce(Product.acquisition_handling_paid, 0)
        )
        product_count = self.db.scalar(select(func.count()).select_from(Product)) or 0
        listing_count = (
            self.db.scalar(
                select(func.count())
                .select_from(MarketplaceListing)
                .where(MarketplaceListing.listing_status == "active")
            )
            or 0
        )
        sale_count = self.db.scalar(select(func.count()).select_from(Sale)) or 0

        product_cost_rows = self.db.execute(
            select(
                Product.id,
                Product.current_quantity,
                Product.acquisition_cost,
                Product.acquisition_tax_paid,
                Product.acquisition_shipping_paid,
                Product.acquisition_handling_paid,
                Product.product_cost,
            )
        ).all()
        default_unit_cost_by_product = {
            int(row.id): self._product_default_landed_unit_cost(row)
            for row in product_cost_rows
            if row.id is not None
        }
        lot_cost_maps = self.report_sale_unit_cost_maps(
            end_dt=utcnow_naive(),
            default_unit_cost_by_product=default_unit_cost_by_product,
        )
        fifo_remaining_unit_cost_by_product = dict(
            lot_cost_maps.get("fifo_remaining_unit_cost_by_product") or {}
        )
        lot_weighted_unit_cost_by_product = dict(lot_cost_maps.get("lot_weighted_unit_cost_by_product") or {})
        inventory_cost = sum(
            max(0, int(row.current_quantity or 0))
            * self._safe_float(
                fifo_remaining_unit_cost_by_product.get(
                    int(row.id),
                    lot_weighted_unit_cost_by_product.get(
                        int(row.id),
                        default_unit_cost_by_product.get(int(row.id), 0.0),
                    ),
                )
            )
            for row in product_cost_rows
            if row.id is not None
        )

        gross_sales = self.db.scalar(select(func.coalesce(func.sum(Sale.sold_price), 0))) or 0
        raw_net_sales = (
            self.db.scalar(
                select(
                    func.coalesce(
                        func.sum(
                            Sale.sold_price
                            + Sale.shipping_cost
                            - Sale.fees
                            - func.coalesce(Sale.shipping_label_cost, 0)
                        ),
                        0,
                    )
                )
            )
            or 0
        )
        net_sales = raw_net_sales
        if int(sale_count or 0) > 0:
            sale_window = self.db.execute(select(func.min(Sale.sold_at), func.max(Sale.sold_at))).one()
            if sale_window[0] is not None and sale_window[1] is not None:
                actual_rows = self.report_sales_actual_econ_rows(
                    start_dt=sale_window[0] - timedelta(seconds=1),
                    end_dt=sale_window[1] + timedelta(seconds=1),
                )
                if actual_rows:
                    net_sales = sum(
                        self._safe_float(row.get("net_before_cogs_actual"))
                        for row in actual_rows
                    )

        return {
            "product_count": int(product_count),
            "listing_count": int(listing_count),
            "sale_count": int(sale_count),
            "inventory_cost": float(inventory_cost),
            "gross_sales": float(gross_sales),
            "net_sales": float(net_sales),
        }

    def dashboard_live_metrics(
        self,
        *,
        now: datetime | None = None,
        include_fee_type_breakdown: bool = True,
    ) -> dict:
        snapshot = now or utcnow_naive()
        window_7d = snapshot - timedelta(days=7)
        window_30d = snapshot - timedelta(days=30)
        landed_unit_cost_expr = (
            func.coalesce(Product.acquisition_cost, 0)
            + func.coalesce(Product.acquisition_tax_paid, 0)
            + func.coalesce(Product.acquisition_shipping_paid, 0)
            + func.coalesce(Product.acquisition_handling_paid, 0)
        )

        sales_windows = self.db.execute(
            select(
                func.coalesce(
                    func.sum(
                        case(
                            (and_(Sale.sold_at >= window_7d, Sale.sold_at <= snapshot), 1),
                            else_=0,
                        )
                    ),
                    0,
                ).label("sales_7d_count"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                and_(Sale.sold_at >= window_7d, Sale.sold_at <= snapshot),
                                func.coalesce(Sale.sold_price, 0),
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("sales_7d_gross"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                and_(Sale.sold_at >= window_7d, Sale.sold_at <= snapshot),
                                func.coalesce(Sale.fees, 0),
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("sales_7d_fees"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                and_(Sale.sold_at >= window_7d, Sale.sold_at <= snapshot),
                                func.coalesce(Sale.shipping_cost, 0),
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("sales_7d_shipping"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                and_(Sale.sold_at >= window_7d, Sale.sold_at <= snapshot),
                                func.coalesce(Sale.shipping_label_cost, 0),
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("sales_7d_label_spend"),
                func.coalesce(func.sum(func.coalesce(Sale.sold_price, 0)), 0).label("sales_30d_gross"),
                func.coalesce(func.sum(func.coalesce(Sale.fees, 0)), 0).label("sales_30d_fees"),
                func.coalesce(func.sum(func.coalesce(Sale.shipping_cost, 0)), 0).label("sales_30d_shipping"),
                func.coalesce(func.sum(func.coalesce(Sale.shipping_label_cost, 0)), 0).label("sales_30d_label_spend"),
                func.coalesce(func.count(Sale.id), 0).label("sales_30d_count"),
                func.coalesce(
                    func.sum(landed_unit_cost_expr * func.coalesce(Sale.quantity_sold, 0)),
                    0,
                ).label("sales_30d_est_cogs"),
            )
            .select_from(Sale)
            .outerjoin(Product, Product.id == Sale.product_id)
            .where(Sale.sold_at >= window_30d, Sale.sold_at <= snapshot)
        ).one()

        order_metrics_30d = self.db.execute(
            select(
                func.count(Order.id),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                func.lower(func.coalesce(cast(Order.order_status, String), "")).in_(
                                    ["shipped", "delivered"]
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                ~func.lower(func.coalesce(cast(Order.order_status, String), "")).in_(
                                    ["shipped", "delivered", "cancelled", "refunded"]
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.coalesce(func.sum(func.coalesce(Order.shipping_cost, 0)), 0),
                func.coalesce(func.sum(func.coalesce(Order.shipping_label_cost, 0)), 0),
            ).where(Order.sold_at >= window_30d, Order.sold_at <= snapshot)
        ).one()
        orders_7d_count = self.db.scalar(
            select(func.count(Order.id)).where(Order.sold_at >= window_7d, Order.sold_at <= snapshot)
        ) or 0

        sales_7d_count = int(sales_windows[0] or 0)
        sales_7d_gross = float(sales_windows[1] or 0)
        sales_7d_fees = float(sales_windows[2] or 0)
        sales_7d_shipping = float(sales_windows[3] or 0)
        sales_7d_label_spend = float(sales_windows[4] or 0)
        sales_7d_net = sales_7d_gross + sales_7d_shipping - sales_7d_fees - sales_7d_label_spend

        sales_30d_count = int(sales_windows[9] or 0)
        sales_30d_gross = float(sales_windows[5] or 0)
        sales_30d_fees = float(sales_windows[6] or 0)
        sales_30d_shipping = float(sales_windows[7] or 0)
        sales_30d_label_spend = float(sales_windows[8] or 0)
        sales_30d_est_cogs = float(sales_windows[10] or 0)
        sales_30d_cogs_source_counts: dict[str, int] = {}
        return_rows_30d = self.db.execute(
            select(
                ReturnRecord.id,
                ReturnRecord.sale_id,
                ReturnRecord.product_id,
                ReturnRecord.quantity,
                ReturnRecord.refund_amount,
                ReturnRecord.refund_fees,
                ReturnRecord.refund_shipping,
            )
            .where(ReturnRecord.returned_at >= window_30d, ReturnRecord.returned_at <= snapshot)
        ).all()
        returns_30d_count = len(return_rows_30d)
        returns_30d_refund_total = round(
            sum(
                self._safe_float(row.refund_amount)
                + self._safe_float(row.refund_fees)
                + self._safe_float(row.refund_shipping)
                for row in return_rows_30d
            ),
            2,
        )
        returns_30d_cogs_reversal = 0.0
        returns_30d_profit_impact = -returns_30d_refund_total
        if sales_30d_count > 0 or returns_30d_count > 0:
            product_default_rows = self.db.execute(
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
                int(row.id): self._product_default_landed_unit_cost(row)
                for row in product_default_rows
                if row.id is not None
            }
            cost_maps = self.report_sale_unit_cost_maps(
                end_dt=snapshot,
                default_unit_cost_by_product=default_unit_cost_by_product,
            )
            fifo_unit_cost_by_sale = dict(cost_maps.get("fifo_unit_cost_by_sale") or {})
            fifo_unit_cost_source_by_sale = dict(cost_maps.get("fifo_unit_cost_source_by_sale") or {})
            if return_rows_30d:
                for row in return_rows_30d:
                    return_qty = max(1, int(row.quantity or 1))
                    if row.sale_id is not None and int(row.sale_id) in fifo_unit_cost_by_sale:
                        returns_30d_cogs_reversal += (
                            self._safe_float(fifo_unit_cost_by_sale.get(int(row.sale_id))) * return_qty
                        )
                    elif row.product_id is not None:
                        returns_30d_cogs_reversal += (
                            self._safe_float(default_unit_cost_by_product.get(int(row.product_id))) * return_qty
                        )
                returns_30d_cogs_reversal = round(float(returns_30d_cogs_reversal), 2)
                returns_30d_profit_impact = round(
                    -returns_30d_refund_total + returns_30d_cogs_reversal,
                    2,
                )
            if sales_30d_count > 0:
                sale_qty_rows = self.db.execute(
                    select(
                        Sale.id,
                        Sale.quantity_sold,
                        MarketplaceListing.marketplace_details.label("listing_marketplace_details"),
                    )
                    .select_from(Sale)
                    .outerjoin(MarketplaceListing, MarketplaceListing.id == Sale.listing_id)
                    .where(Sale.sold_at >= window_30d, Sale.sold_at <= snapshot)
                ).all()
                sales_30d_est_cogs = sum(
                    self._safe_float(fifo_unit_cost_by_sale.get(int(row.id)))
                    * max(1, int(row.quantity_sold or 1))
                    for row in sale_qty_rows
                    if row.id is not None
                )
                sales_30d_bundle_sale_count = 0
                sales_30d_bundle_inventory_units_sold = 0
                for row in sale_qty_rows:
                    if row.id is None:
                        continue
                    bundle_components = self._bundle_components_from_payload(
                        self._listing_bundle_payload_from_raw(row.listing_marketplace_details),
                        int(row.quantity_sold or 0),
                    )
                    if bundle_components:
                        sales_30d_bundle_sale_count += 1
                        sales_30d_bundle_inventory_units_sold += sum(
                            max(1, int(component.get("quantity_total") or 1))
                            for component in bundle_components
                        )
                    source = str(fifo_unit_cost_source_by_sale.get(int(row.id)) or "missing_cost_basis")
                    sales_30d_cogs_source_counts[source] = sales_30d_cogs_source_counts.get(source, 0) + 1
            else:
                sales_30d_bundle_sale_count = 0
                sales_30d_bundle_inventory_units_sold = 0
        else:
            sales_30d_bundle_sale_count = 0
            sales_30d_bundle_inventory_units_sold = 0
        cogs_review_sources = {
            "lot_equal_quantity_fallback",
            "missing_cost_basis",
            "mixed_fifo_cost",
        }
        sales_30d_cogs_review_count = sum(
            int(count or 0)
            for source, count in sales_30d_cogs_source_counts.items()
            if str(source or "") in cogs_review_sources
        )
        if sales_30d_cogs_review_count > 0:
            sales_30d_profit_basis_status = "review_needed"
        elif int(sales_30d_cogs_source_counts.get("lot_expected_quantity_fallback", 0) or 0) > 0:
            sales_30d_profit_basis_status = "partial_lot_estimate"
        else:
            sales_30d_profit_basis_status = "ok"

        orders_30d_count = int(order_metrics_30d[0] or 0)
        orders_30d_shipped = int(order_metrics_30d[1] or 0)
        orders_30d_not_shipped = int(order_metrics_30d[2] or 0)
        orders_30d_shipping_charged = float(order_metrics_30d[3] or 0)
        orders_30d_label_spend = float(order_metrics_30d[4] or 0)

        actual_econ_rows = (
            self.report_sales_actual_econ_rows(start_dt=window_30d, end_dt=snapshot)
            if sales_30d_count > 0
            else []
        )
        if actual_econ_rows:
            actual_7d_rows = []
            for row in actual_econ_rows:
                sold_at_raw = row.get("sold_at")
                sold_at_value = sold_at_raw
                if isinstance(sold_at_raw, str):
                    try:
                        sold_at_value = datetime.fromisoformat(sold_at_raw)
                    except ValueError:
                        sold_at_value = None
                if isinstance(sold_at_value, datetime) and sold_at_value >= window_7d:
                    actual_7d_rows.append(row)
            if actual_7d_rows:
                sales_7d_net = round(
                    sum(float(row.get("net_before_cogs_actual") or 0.0) for row in actual_7d_rows),
                    2,
                )
        actual_fee_30d = round(
            sum(float(row.get("allocated_fee_actual") or 0.0) for row in actual_econ_rows),
            2,
        )
        normalized_fee_type_totals: dict[str, float] = {}
        linked_order_ids = sorted(
            {
                int(row.get("order_id") or 0)
                for row in actual_econ_rows
                if int(row.get("order_id") or 0) > 0
            }
        )
        if include_fee_type_breakdown and linked_order_ids:
            for fee_type, fee_total in self.db.execute(
                select(
                    OrderFinanceEntry.fee_type,
                    func.coalesce(func.sum(func.coalesce(OrderFinanceEntry.amount, 0)), 0),
                )
                .where(
                    OrderFinanceEntry.order_id.in_(linked_order_ids),
                    OrderFinanceEntry.entry_kind == "marketplace_fee",
                )
                .group_by(OrderFinanceEntry.fee_type)
            ).all():
                key = str(fee_type or "").strip().upper()
                if not key:
                    key = "UNKNOWN"
                normalized_fee_type_totals[key] = round(float(fee_total or 0), 2)
        normalized_fee_total_30d = round(actual_fee_30d if actual_econ_rows else float(sales_30d_fees or 0), 2)
        if actual_econ_rows:
            shipping_charged_30d = round(
                sum(float(row.get("allocated_shipping_charged") or 0.0) for row in actual_econ_rows),
                2,
            )
            shipping_label_spend_30d = round(
                sum(float(row.get("allocated_shipping_actual") or 0.0) for row in actual_econ_rows),
                2,
            )
        elif orders_30d_shipping_charged > 0 or orders_30d_label_spend > 0:
            shipping_charged_30d = orders_30d_shipping_charged
            shipping_label_spend_30d = orders_30d_label_spend
        else:
            shipping_charged_30d = sales_30d_shipping
            shipping_label_spend_30d = sales_30d_label_spend
        sales_30d_net = (
            sales_30d_gross
            + shipping_charged_30d
            - normalized_fee_total_30d
            - shipping_label_spend_30d
        )
        sales_30d_profit_before_returns = round(sales_30d_net - sales_30d_est_cogs, 2)
        sales_30d_net_after_returns = round(sales_30d_net - returns_30d_refund_total, 2)
        sales_30d_est_profit = round(sales_30d_profit_before_returns + returns_30d_profit_impact, 2)

        return {
            "orders_7d_count": int(orders_7d_count),
            "orders_30d_count": orders_30d_count,
            "orders_30d_shipped": orders_30d_shipped,
            "orders_30d_not_shipped": orders_30d_not_shipped,
            "sales_7d_count": sales_7d_count,
            "sales_30d_count": sales_30d_count,
            "sales_7d_gross": sales_7d_gross,
            "sales_7d_net": sales_7d_net,
            "sales_30d_gross": sales_30d_gross,
            "sales_30d_net": sales_30d_net,
            "sales_30d_shipping_charged": shipping_charged_30d,
            "sales_30d_shipping_label_spend": shipping_label_spend_30d,
            "sales_30d_shipping_delta": shipping_charged_30d - shipping_label_spend_30d,
            "sales_30d_est_cogs": sales_30d_est_cogs,
            "sales_30d_profit_before_returns": sales_30d_profit_before_returns,
            "returns_30d_count": returns_30d_count,
            "returns_30d_refund_total": returns_30d_refund_total,
            "returns_30d_cogs_reversal": returns_30d_cogs_reversal,
            "returns_30d_profit_impact": returns_30d_profit_impact,
            "sales_30d_net_after_returns": sales_30d_net_after_returns,
            "sales_30d_cogs_source_counts": sales_30d_cogs_source_counts,
            "sales_30d_cogs_review_count": sales_30d_cogs_review_count,
            "sales_30d_profit_basis_status": sales_30d_profit_basis_status,
            "sales_30d_est_profit": sales_30d_est_profit,
            "sales_30d_bundle_sale_count": sales_30d_bundle_sale_count,
            "sales_30d_bundle_inventory_units_sold": sales_30d_bundle_inventory_units_sold,
            "ebay_fees_30d_total": normalized_fee_total_30d,
            "ebay_fee_type_breakdown_30d": normalized_fee_type_totals,
        }

    def dashboard_profit_basis_rows(
        self,
        *,
        now: datetime | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        snapshot = now or utcnow_naive()
        window_30d = snapshot - timedelta(days=30)
        sale_rows = self.db.execute(
            select(
                Sale.id.label("sale_id"),
                Sale.sold_at.label("sold_at"),
                Sale.marketplace.label("marketplace"),
                Sale.product_id.label("product_id"),
                Sale.listing_id.label("listing_id"),
                Sale.quantity_sold.label("quantity_sold"),
                Sale.sold_price.label("sold_price"),
                Sale.fees.label("fees"),
                Sale.shipping_cost.label("shipping_cost"),
                Sale.shipping_label_cost.label("shipping_label_cost"),
                Product.sku.label("sku"),
                Product.title.label("product_title"),
                MarketplaceListing.marketplace_details.label("listing_marketplace_details"),
            )
            .select_from(Sale)
            .outerjoin(Product, Product.id == Sale.product_id)
            .outerjoin(MarketplaceListing, MarketplaceListing.id == Sale.listing_id)
            .where(Sale.sold_at >= window_30d, Sale.sold_at <= snapshot)
            .order_by(Sale.sold_at.desc(), Sale.id.desc())
            .limit(max(1, int(limit or 50)))
        ).all()
        if not sale_rows:
            return []

        product_default_rows = self.db.execute(
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
            int(row.id): self._product_default_landed_unit_cost(row)
            for row in product_default_rows
            if row.id is not None
        }
        cost_maps = self.report_sale_unit_cost_maps(
            end_dt=snapshot,
            default_unit_cost_by_product=default_unit_cost_by_product,
        )
        fifo_unit_cost_by_sale = dict(cost_maps.get("fifo_unit_cost_by_sale") or {})
        fifo_total_cost_by_sale = dict(cost_maps.get("fifo_total_cost_by_sale") or {})
        fifo_unit_cost_source_by_sale = dict(cost_maps.get("fifo_unit_cost_source_by_sale") or {})
        fifo_cogs_evidence_by_sale = dict(cost_maps.get("fifo_cogs_evidence_by_sale") or {})
        actual_econ_by_sale_id = {
            int(row.get("sale_id") or 0): row
            for row in self.report_sales_actual_econ_rows(start_dt=window_30d, end_dt=snapshot)
            if int(row.get("sale_id") or 0) > 0
        }

        output: list[dict[str, Any]] = []
        for row in sale_rows:
            sale_id = int(row.sale_id or 0)
            qty = max(1, int(row.quantity_sold or 1))
            actual = actual_econ_by_sale_id.get(sale_id) or {}
            gross = self._safe_float(row.sold_price)
            fees = self._safe_float(actual.get("allocated_fee_actual", row.fees))
            shipping_charged = self._safe_float(
                actual.get("allocated_shipping_charged", row.shipping_cost)
            )
            label_spend = self._safe_float(
                actual.get("allocated_shipping_actual", row.shipping_label_cost)
            )
            net_before_cogs = self._safe_float(
                actual.get(
                    "net_before_cogs_actual",
                    gross + shipping_charged - fees - label_spend,
                )
            )
            fifo_total = self._safe_float(fifo_total_cost_by_sale.get(sale_id))
            if fifo_total <= 0:
                fifo_total = self._safe_float(fifo_unit_cost_by_sale.get(sale_id)) * qty
            unit_cogs = (fifo_total / float(qty)) if qty > 0 else 0.0
            profit_before_returns = net_before_cogs - fifo_total
            evidence_rows = list(fifo_cogs_evidence_by_sale.get(sale_id) or [])
            bundle_components = self._bundle_components_from_payload(
                self._listing_bundle_payload_from_raw(row.listing_marketplace_details),
                qty,
            )
            output.append(
                {
                    "sale_id": sale_id,
                    "sold_at": row.sold_at.isoformat() if row.sold_at else None,
                    "marketplace": str(row.marketplace or "").strip().lower(),
                    "product_id": int(row.product_id or 0) or None,
                    "sku": str(row.sku or "").strip() or None,
                    "product_title": str(row.product_title or "").strip() or None,
                    "quantity_sold": int(qty),
                    "gross_sales": round(float(gross), 2),
                    "shipping_charged": round(float(shipping_charged), 2),
                    "fees": round(float(fees), 2),
                    "label_spend": round(float(label_spend), 2),
                    "net_before_cogs": round(float(net_before_cogs), 2),
                    "fifo_unit_cogs": round(float(unit_cogs), 4),
                    "fifo_cogs": round(float(fifo_total), 2),
                    "profit_before_returns": round(float(profit_before_returns), 2),
                    "fifo_cost_source": str(
                        fifo_unit_cost_source_by_sale.get(sale_id) or "missing_cost_basis"
                    ),
                    "fifo_cogs_evidence_rows": int(len(evidence_rows)),
                    "listing_is_bundle": bool(bundle_components),
                    "listing_bundle_component_count": int(len(bundle_components)),
                }
            )
        return output

    def report_shipping_economics_rows(
        self,
        *,
        start_dt: datetime,
        end_dt: datetime,
        marketplaces: set[str] | None = None,
    ) -> list[dict]:
        ListingProduct = aliased(Product)
        marketplace_filter = {str(v).strip().lower() for v in (marketplaces or set()) if str(v).strip()}
        marketplace_expr = func.lower(func.coalesce(Sale.marketplace, ""))
        order_sale_count_expr = case(
            (
                Sale.order_id.is_not(None),
                func.count(Sale.id).over(partition_by=Sale.order_id),
            ),
            else_=1,
        )
        order_shipping_alloc_expr = func.coalesce(Order.shipping_cost, 0) / func.nullif(order_sale_count_expr, 0)
        order_label_alloc_expr = func.coalesce(Order.shipping_label_cost, 0) / func.nullif(order_sale_count_expr, 0)
        effective_shipping_charged_expr = case(
            (func.coalesce(Sale.shipping_cost, 0) > 0, func.coalesce(Sale.shipping_cost, 0)),
            else_=func.coalesce(order_shipping_alloc_expr, 0),
        )
        effective_label_spend_expr = case(
            (func.coalesce(Sale.shipping_label_cost, 0) > 0, func.coalesce(Sale.shipping_label_cost, 0)),
            else_=func.coalesce(order_label_alloc_expr, 0),
        )
        query = (
            select(
                Sale.id.label("sale_id"),
                Sale.sold_at.label("sold_at"),
                marketplace_expr.label("marketplace"),
                Sale.external_order_id.label("external_order_id"),
                Sale.order_id.label("order_id"),
                func.coalesce(Product.sku, ListingProduct.sku).label("sku"),
                func.coalesce(Product.title, ListingProduct.title).label("product_title"),
                Sale.quantity_sold.label("qty"),
                effective_shipping_charged_expr.label("shipping_charged_to_buyer"),
                effective_label_spend_expr.label("shipping_label_spend"),
                (effective_shipping_charged_expr - effective_label_spend_expr).label(
                    "shipping_delta_charged_minus_spend"
                ),
                func.coalesce(Sale.shipping_label_currency, func.coalesce(Order.shipping_label_currency, "")).label("shipping_label_currency"),
                func.coalesce(Sale.shipping_label_id, "").label("shipping_label_id"),
                Sale.shipping_label_purchased_at.label("shipping_label_purchased_at"),
                func.coalesce(Sale.shipping_provider, "").label("shipping_provider"),
                func.coalesce(Sale.shipping_service, "").label("shipping_service"),
                func.coalesce(Sale.tracking_number, "").label("tracking_number"),
            )
            .select_from(Sale)
            .outerjoin(Product, Product.id == Sale.product_id)
            .outerjoin(MarketplaceListing, MarketplaceListing.id == Sale.listing_id)
            .outerjoin(ListingProduct, ListingProduct.id == MarketplaceListing.product_id)
            .outerjoin(Order, Order.id == Sale.order_id)
            .where(Sale.sold_at.is_not(None), Sale.sold_at >= start_dt, Sale.sold_at <= end_dt)
            .order_by(Sale.sold_at.desc(), Sale.id.desc())
        )
        if marketplace_filter:
            query = query.where(marketplace_expr.in_(sorted(marketplace_filter)))
        rows = self.db.execute(query).all()
        actual_econ_by_sale_id = {
            int(actual_row.get("sale_id") or 0): actual_row
            for actual_row in self.report_sales_actual_econ_rows(start_dt=start_dt, end_dt=end_dt)
            if int(actual_row.get("sale_id") or 0) > 0
        }
        result: list[dict] = []
        for row in rows:
            order_id = int(row.order_id) if row.order_id is not None else None
            actual = actual_econ_by_sale_id.get(int(row.sale_id or 0)) or {}
            shipping_charged = self._safe_float(
                actual.get("allocated_shipping_charged", row.shipping_charged_to_buyer)
            )
            label_spend = self._safe_float(actual.get("allocated_shipping_actual", row.shipping_label_spend))
            label_source = str(actual.get("actual_shipping_source") or "order_or_sale_shipping_label_field")
            result.append(
                {
                    "sale_id": int(row.sale_id),
                    "sold_at": row.sold_at,
                    "marketplace": str(row.marketplace or "").strip().lower(),
                    "external_order_id": str(row.external_order_id or "").strip(),
                    "order_id": order_id,
                    "sku": str(row.sku or "").strip() or None,
                    "product_title": str(row.product_title or "").strip() or None,
                    "qty": int(row.qty or 0),
                    "shipping_charged_to_buyer": round(shipping_charged, 2),
                    "shipping_label_spend": round(label_spend, 2),
                    "shipping_delta_charged_minus_spend": round(
                        shipping_charged - label_spend,
                        2,
                    ),
                    "shipping_label_spend_source": label_source,
                    "shipping_label_currency": str(row.shipping_label_currency or "").strip(),
                    "shipping_label_id": str(row.shipping_label_id or "").strip(),
                    "shipping_label_purchased_at": row.shipping_label_purchased_at,
                    "shipping_provider": str(row.shipping_provider or "").strip(),
                    "shipping_service": str(row.shipping_service or "").strip(),
                    "tracking_number": str(row.tracking_number or "").strip(),
                }
            )
        return result

    def report_shipping_economics_summary(
        self,
        *,
        start_dt: datetime,
        end_dt: datetime,
        marketplaces: set[str] | None = None,
    ) -> list[dict]:
        rows = self.report_shipping_economics_rows(
            start_dt=start_dt,
            end_dt=end_dt,
            marketplaces=marketplaces,
        )
        grouped: dict[str, dict[str, float | int | str]] = {}
        for row in rows:
            marketplace = str(row.get("marketplace") or "").strip().lower()
            if marketplace not in grouped:
                grouped[marketplace] = {
                    "marketplace": marketplace,
                    "sales_count": 0,
                    "total_shipping_charged": 0.0,
                    "total_label_spend": 0.0,
                    "label_spend_covered_count": 0,
                }
            bucket = grouped[marketplace]
            bucket["sales_count"] = int(bucket["sales_count"] or 0) + 1
            charged = float(row.get("shipping_charged_to_buyer") or 0.0)
            label = float(row.get("shipping_label_spend") or 0.0)
            bucket["total_shipping_charged"] = float(bucket["total_shipping_charged"] or 0.0) + charged
            bucket["total_label_spend"] = float(bucket["total_label_spend"] or 0.0) + label
            if label > 0:
                bucket["label_spend_covered_count"] = int(bucket["label_spend_covered_count"] or 0) + 1

        result: list[dict] = []
        for marketplace, bucket in sorted(
            grouped.items(),
            key=lambda item: (-int(item[1]["sales_count"] or 0), str(item[0])),
        ):
            sales_count = int(bucket["sales_count"] or 0)
            total_shipping_charged = float(bucket["total_shipping_charged"] or 0.0)
            total_label_spend = float(bucket["total_label_spend"] or 0.0)
            covered_count = int(bucket["label_spend_covered_count"] or 0)
            coverage_pct = (float(covered_count) / float(sales_count) * 100.0) if sales_count > 0 else 0.0
            result.append(
                {
                    "marketplace": marketplace,
                    "sales_count": sales_count,
                    "total_shipping_charged": round(total_shipping_charged, 2),
                    "total_label_spend": round(total_label_spend, 2),
                    "shipping_delta_charged_minus_spend": round(total_shipping_charged - total_label_spend, 2),
                    "label_spend_covered_count": covered_count,
                    "label_spend_coverage_percent": round(coverage_pct, 2),
                }
            )
        return result

    def report_tax_estimate_detail_rows(
        self,
        *,
        start_dt: datetime,
        end_dt: datetime,
        tax_rate_percent: float,
        shipping_taxable: bool,
        tax_exempt_categories: set[str] | None = None,
        marketplaces: set[str] | None = None,
    ) -> list[dict]:
        exempt_categories = {
            str(v).strip().lower() for v in (tax_exempt_categories or set()) if str(v).strip()
        }
        marketplace_filter = {str(v).strip().lower() for v in (marketplaces or set()) if str(v).strip()}
        ListingProduct = aliased(Product)
        category_expr = func.lower(func.coalesce(Product.category, ListingProduct.category, ""))
        marketplace_expr = func.lower(func.coalesce(Sale.marketplace, ""))
        is_exempt_expr = (
            category_expr.in_(sorted(exempt_categories)) if exempt_categories else literal(False)
        )
        taxable_item_expr = case(
            (is_exempt_expr, 0),
            else_=func.coalesce(Sale.sold_price, 0),
        )
        taxable_shipping_expr = (
            func.coalesce(Sale.shipping_cost, 0) if bool(shipping_taxable) else literal(0)
        )
        taxable_subtotal_expr = taxable_item_expr + taxable_shipping_expr
        tax_rate_multiplier = float(max(0.0, float(tax_rate_percent or 0.0))) / 100.0
        estimated_tax_expr = taxable_subtotal_expr * tax_rate_multiplier

        query = (
            select(
                Sale.id.label("sale_id"),
                Sale.sold_at.label("sold_at"),
                marketplace_expr.label("marketplace"),
                func.coalesce(Product.sku, ListingProduct.sku).label("sku"),
                func.coalesce(Product.title, ListingProduct.title).label("product_title"),
                category_expr.label("category"),
                func.coalesce(Sale.sold_price, 0).label("gross_sales"),
                func.coalesce(Sale.shipping_cost, 0).label("shipping_cost"),
                is_exempt_expr.label("is_tax_exempt_category"),
                taxable_item_expr.label("taxable_item_subtotal"),
                taxable_shipping_expr.label("taxable_shipping_subtotal"),
                taxable_subtotal_expr.label("taxable_subtotal"),
                estimated_tax_expr.label("estimated_tax_collected"),
            )
            .select_from(Sale)
            .outerjoin(Product, Product.id == Sale.product_id)
            .outerjoin(MarketplaceListing, MarketplaceListing.id == Sale.listing_id)
            .outerjoin(ListingProduct, ListingProduct.id == MarketplaceListing.product_id)
            .where(Sale.sold_at.is_not(None), Sale.sold_at >= start_dt, Sale.sold_at <= end_dt)
            .order_by(Sale.sold_at.desc(), Sale.id.desc())
        )
        if marketplace_filter:
            query = query.where(marketplace_expr.in_(sorted(marketplace_filter)))
        rows = self.db.execute(query).all()
        actual_econ_by_sale_id = {
            int(actual_row.get("sale_id") or 0): actual_row
            for actual_row in self.report_sales_actual_econ_rows(start_dt=start_dt, end_dt=end_dt)
            if int(actual_row.get("sale_id") or 0) > 0
        }
        result: list[dict] = []
        for row in rows:
            actual = actual_econ_by_sale_id.get(int(row.sale_id or 0)) or {}
            field_shipping_cost = float(row.shipping_cost or 0)
            shipping_charged = self._safe_float(
                actual.get("allocated_shipping_charged", field_shipping_cost)
            )
            taxable_shipping = shipping_charged if bool(shipping_taxable) else 0.0
            taxable_subtotal = float(row.taxable_item_subtotal or 0) + taxable_shipping
            estimated_tax = taxable_subtotal * tax_rate_multiplier
            result.append(
                {
                    "sale_id": int(row.sale_id),
                    "sold_at": row.sold_at,
                    "marketplace": str(row.marketplace or "").strip().lower(),
                    "sku": str(row.sku or "").strip() or None,
                    "product_title": str(row.product_title or "").strip() or None,
                    "category": str(row.category or "").strip().lower(),
                    "gross_sales": round(float(row.gross_sales or 0), 2),
                    "shipping_cost": round(shipping_charged, 2),
                    "field_shipping_cost": round(field_shipping_cost, 2),
                    "shipping_cost_source": (
                        "actual_economics_allocated_shipping_charged"
                        if actual
                        else "sale_shipping_cost_field"
                    ),
                    "is_tax_exempt_category": bool(row.is_tax_exempt_category),
                    "taxable_item_subtotal": round(float(row.taxable_item_subtotal or 0), 2),
                    "taxable_shipping_subtotal": round(taxable_shipping, 2),
                    "taxable_subtotal": round(taxable_subtotal, 2),
                    "estimated_tax_collected": round(estimated_tax, 2),
                }
            )
        return result

    def report_accounting_exception_rows(
        self,
        *,
        start_dt: datetime,
        end_dt: datetime,
    ) -> list[dict]:
        product_cost_rows = self.db.execute(
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
            int(row.id): self._product_default_landed_unit_cost(row)
            for row in product_cost_rows
            if row.id is not None
        }
        cost_maps = self.report_sale_unit_cost_maps(
            end_dt=end_dt,
            default_unit_cost_by_product=default_unit_cost_by_product,
        )
        fifo_unit_cost_by_sale = {
            int(k): self._safe_float(v)
            for k, v in dict(cost_maps.get("fifo_unit_cost_by_sale") or {}).items()
            if k is not None
        }
        fifo_unit_cost_source_by_sale = {
            int(k): str(v or "missing_cost_basis")
            for k, v in dict(cost_maps.get("fifo_unit_cost_source_by_sale") or {}).items()
            if k is not None
        }
        fifo_cogs_evidence_by_sale = {
            int(k): list(v or [])
            for k, v in dict(cost_maps.get("fifo_cogs_evidence_by_sale") or {}).items()
            if k is not None
        }
        shipping_rows = self.report_shipping_economics_rows(start_dt=start_dt, end_dt=end_dt)
        shipping_by_sale_id = {
            int(row.get("sale_id") or 0): row
            for row in shipping_rows
            if int(row.get("sale_id") or 0) > 0
        }
        actual_econ_by_sale_id = {
            int(row.get("sale_id") or 0): row
            for row in self.report_sales_actual_econ_rows(start_dt=start_dt, end_dt=end_dt)
            if int(row.get("sale_id") or 0) > 0
        }

        rows: list[dict] = []

        def add_exception(
            *,
            severity: str,
            exception_type: str,
            entity_type: str,
            entity_id: int,
            sku: str | None = None,
            marketplace: str = "",
            reference: str = "",
            amount: float | None = None,
            details: str = "",
            occurred_at: datetime | str | None = None,
        ) -> None:
            rows.append(
                {
                    "severity": severity,
                    "exception_type": exception_type,
                    "entity_type": entity_type,
                    "entity_id": int(entity_id or 0),
                    "sku": str(sku or "").strip() or None,
                    "marketplace": str(marketplace or "").strip().lower(),
                    "reference": str(reference or "").strip(),
                    "amount": round(float(amount), 2) if amount is not None else None,
                    "details": str(details or "").strip(),
                    "occurred_at": occurred_at.isoformat() if isinstance(occurred_at, datetime) else occurred_at,
                }
            )

        def _cogs_evidence_summary(sale_id: int) -> str:
            evidence = fifo_cogs_evidence_by_sale.get(int(sale_id or 0)) or []
            if not evidence:
                return "No FIFO COGS evidence rows were available for this sale."
            parts: list[str] = []
            for item in evidence[:4]:
                qty = self._safe_float(item.get("quantity"))
                unit_cost = self._safe_float(item.get("unit_cost"))
                total_cost = self._safe_float(item.get("total_cost"))
                source = str(item.get("cost_source") or "missing_cost_basis")
                product_id = int(item.get("product_id") or 0)
                lot_id = int(item.get("lot_id") or 0)
                assignment_id = int(item.get("assignment_id") or 0)
                origin = f"product#{product_id}" if product_id > 0 else "product#?"
                if lot_id > 0:
                    origin += f"/lot#{lot_id}"
                if assignment_id > 0:
                    origin += f"/assignment#{assignment_id}"
                parts.append(f"{qty:g} unit(s) from {source} at {unit_cost:.2f} = {total_cost:.2f} ({origin})")
            if len(evidence) > 4:
                parts.append(f"plus {len(evidence) - 4} more COGS allocation row(s)")
            return "; ".join(parts)

        ListingProduct = aliased(Product)
        sale_rows = self.db.execute(
            select(
                Sale.id.label("sale_id"),
                Sale.sold_at.label("sold_at"),
                Sale.marketplace.label("marketplace"),
                Sale.external_order_id.label("external_order_id"),
                Sale.order_id.label("order_id"),
                Sale.product_id.label("product_id"),
                Sale.listing_id.label("listing_id"),
                MarketplaceListing.marketplace_details.label("listing_marketplace_details"),
                func.coalesce(Product.sku, ListingProduct.sku).label("sku"),
                func.coalesce(Product.title, ListingProduct.title).label("product_title"),
                Sale.quantity_sold.label("quantity_sold"),
                Sale.sold_price.label("sold_price"),
                Sale.fees.label("fees"),
                Sale.shipping_cost.label("shipping_cost"),
                Sale.shipping_label_cost.label("shipping_label_cost"),
            )
            .select_from(Sale)
            .outerjoin(Product, Product.id == Sale.product_id)
            .outerjoin(MarketplaceListing, MarketplaceListing.id == Sale.listing_id)
            .outerjoin(ListingProduct, ListingProduct.id == MarketplaceListing.product_id)
            .where(Sale.sold_at.is_not(None), Sale.sold_at >= start_dt, Sale.sold_at <= end_dt)
            .order_by(Sale.sold_at.desc(), Sale.id.desc())
        ).all()

        shippable_marketplaces = {"ebay", "facebook", "craigslist", "local", "in_person", "pos"}
        for sale in sale_rows:
            sale_id = int(sale.sale_id or 0)
            marketplace = str(sale.marketplace or "").strip().lower()
            qty = max(1, int(sale.quantity_sold or 1))
            sold_price = self._safe_float(sale.sold_price)
            actual_detail = actual_econ_by_sale_id.get(sale_id) or {}
            fees = self._safe_float(actual_detail.get("allocated_fee_actual", sale.fees))
            fee_source = str(actual_detail.get("actual_fee_source") or "sale_fees_field")
            shipping_detail = shipping_by_sale_id.get(sale_id) or {}
            shipping_charged = self._safe_float(
                shipping_detail.get("shipping_charged_to_buyer", sale.shipping_cost)
            )
            label_spend = self._safe_float(shipping_detail.get("shipping_label_spend", sale.shipping_label_cost))
            unit_cogs = self._safe_float(fifo_unit_cost_by_sale.get(sale_id))
            cogs = unit_cogs * qty
            cost_source = str(fifo_unit_cost_source_by_sale.get(sale_id) or "missing_cost_basis")
            cogs_evidence = _cogs_evidence_summary(sale_id)
            net_before_cogs = sold_price + shipping_charged - fees - label_spend
            margin = net_before_cogs - cogs
            bundle_components = self._bundle_components_from_payload(
                self._listing_bundle_payload_from_raw(sale.listing_marketplace_details),
                qty,
            )
            has_bundle_cost_basis = bool(bundle_components)

            if int(sale.product_id or 0) <= 0 and not has_bundle_cost_basis:
                add_exception(
                    severity="P0",
                    exception_type="missing_product_link",
                    entity_type="sale",
                    entity_id=sale_id,
                    marketplace=marketplace,
                    reference=sale.external_order_id,
                    amount=sold_price,
                    details="Sale has no product link, so COGS cannot be proven.",
                    occurred_at=sale.sold_at,
                )
            if unit_cogs <= 0:
                add_exception(
                    severity="P0",
                    exception_type="missing_cost_basis",
                    entity_type="sale",
                    entity_id=sale_id,
                    sku=sale.sku,
                    marketplace=marketplace,
                    reference=sale.external_order_id,
                    amount=cogs,
                    details=(
                        "FIFO COGS resolved to zero from lot assignments, product landed cost, and product_cost "
                        f"fallback. Source={cost_source}. Evidence: {cogs_evidence}"
                    ),
                    occurred_at=sale.sold_at,
                )
            if marketplace in shippable_marketplaces and shipping_charged > 0 and label_spend <= 0:
                add_exception(
                    severity="P1",
                    exception_type="missing_shipping_label_spend",
                    entity_type="sale",
                    entity_id=sale_id,
                    sku=sale.sku,
                    marketplace=marketplace,
                    reference=sale.external_order_id,
                    amount=shipping_charged,
                    details="Buyer shipping was recorded, but no sale/order label spend was available.",
                    occurred_at=sale.sold_at,
                )
            if marketplace == "ebay" and fees <= 0:
                add_exception(
                    severity="P1",
                    exception_type="missing_fee_evidence",
                    entity_type="sale",
                    entity_id=sale_id,
                    sku=sale.sku,
                    marketplace=marketplace,
                    reference=sale.external_order_id,
                    amount=fees,
                    details=f"eBay sale has no marketplace fee evidence from actual-economics source `{fee_source}`.",
                    occurred_at=sale.sold_at,
                )
            if margin <= 0:
                add_exception(
                    severity="P1",
                    exception_type="nonpositive_margin",
                    entity_type="sale",
                    entity_id=sale_id,
                    sku=sale.sku,
                    marketplace=marketplace,
                    reference=sale.external_order_id,
                    amount=margin,
                    details=(
                        f"Net before COGS {net_before_cogs:.2f} minus COGS {cogs:.2f} "
                        f"produced margin {margin:.2f}. COGS source={cost_source}. Evidence: {cogs_evidence}"
                    ),
                    occurred_at=sale.sold_at,
                )

        for fee_row in self.report_ebay_fee_reconciliation_rows(start_dt=start_dt, end_dt=end_dt):
            if str(fee_row.get("actual_fee_source") or "").strip() != "sale_fees_field":
                continue
            add_exception(
                severity="P2",
                exception_type="fee_source_fallback",
                entity_type="sale",
                entity_id=int(fee_row.get("sale_id") or 0),
                sku=fee_row.get("sku"),
                marketplace="ebay",
                reference=fee_row.get("external_order_id"),
                amount=self._safe_float(fee_row.get("actual_fee")),
                details="Actual fee is using the sale.fees fallback instead of normalized finance entries or order fee breakdown.",
                occurred_at=fee_row.get("sold_at"),
            )

        active_bundle_listing_rows = self.db.execute(
            select(
                MarketplaceListing.id.label("listing_id"),
                MarketplaceListing.marketplace.label("marketplace"),
                MarketplaceListing.external_listing_id.label("external_listing_id"),
                MarketplaceListing.listing_title.label("listing_title"),
                MarketplaceListing.quantity_listed.label("quantity_listed"),
                MarketplaceListing.marketplace_details.label("marketplace_details"),
                MarketplaceListing.updated_at.label("updated_at"),
            )
            .where(
                func.lower(func.coalesce(cast(MarketplaceListing.listing_status, String), "")) == "active",
                MarketplaceListing.marketplace_details.is_not(None),
                func.length(func.trim(func.coalesce(MarketplaceListing.marketplace_details, ""))) > 0,
            )
            .order_by(MarketplaceListing.updated_at.desc(), MarketplaceListing.id.desc())
        ).all()
        bundle_component_product_ids: set[int] = set()
        parsed_bundle_listings: list[dict[str, Any]] = []
        for listing_row in active_bundle_listing_rows:
            bundle = self._listing_bundle_payload_from_raw(listing_row.marketplace_details)
            components = self._bundle_components_from_payload(bundle, 1)
            if not components:
                continue
            parsed_bundle_listings.append({"row": listing_row, "components": components})
            for component in components:
                product_id = int(component.get("product_id") or 0)
                if product_id > 0:
                    bundle_component_product_ids.add(product_id)
        bundle_component_stock: dict[int, int] = {}
        if bundle_component_product_ids:
            stock_rows = self.db.execute(
                select(Product.id, Product.current_quantity).where(Product.id.in_(sorted(bundle_component_product_ids)))
            ).all()
            bundle_component_stock = {
                int(row.id): max(0, int(row.current_quantity or 0))
                for row in stock_rows
                if row.id is not None
            }
        active_bundle_listing_ids = [
            int(parsed["row"].listing_id)
            for parsed in parsed_bundle_listings
            if int(parsed["row"].listing_id or 0) > 0
        ]
        sold_qty_by_bundle_listing: dict[int, int] = {}
        if active_bundle_listing_ids:
            sold_qty_rows = self.db.execute(
                select(
                    Sale.listing_id.label("listing_id"),
                    func.coalesce(func.sum(Sale.quantity_sold), 0).label("sold_qty"),
                )
                .where(Sale.listing_id.in_(active_bundle_listing_ids))
                .group_by(Sale.listing_id)
            ).all()
            sold_qty_by_bundle_listing = {
                int(row.listing_id): max(0, int(row.sold_qty or 0))
                for row in sold_qty_rows
                if row.listing_id is not None
            }
        for parsed in parsed_bundle_listings:
            listing_row = parsed["row"]
            listing_qty = max(
                0,
                int(listing_row.quantity_listed or 0)
                - int(sold_qty_by_bundle_listing.get(int(listing_row.listing_id or 0), 0)),
            )
            if listing_qty <= 0:
                continue
            shortages: list[str] = []
            available_lots_by_component: list[int] = []
            for component in list(parsed.get("components") or []):
                product_id = int(component.get("product_id") or 0)
                qty_per_listing = max(1, int(component.get("quantity_per_listing") or 1))
                stock_qty = max(0, int(bundle_component_stock.get(product_id, 0)))
                required_qty = qty_per_listing * listing_qty
                available_lots_by_component.append(stock_qty // qty_per_listing)
                if stock_qty < required_qty:
                    shortages.append(
                        f"{component.get('sku') or product_id}: needs {required_qty}, stock {stock_qty}"
                    )
            if shortages:
                max_available_lots = min(available_lots_by_component) if available_lots_by_component else 0
                add_exception(
                    severity="P1",
                    exception_type="active_bundle_listing_stock_shortage",
                    entity_type="listing",
                    entity_id=int(listing_row.listing_id or 0),
                    marketplace=str(listing_row.marketplace or "").strip().lower(),
                    reference=str(listing_row.external_listing_id or "").strip(),
                    amount=float(listing_qty - max_available_lots),
                    details=(
                        f"Active bundle listing remaining quantity {listing_qty} exceeds component stock. "
                        f"Available complete bundle listings from current stock: {max_available_lots}. "
                        + " | ".join(shortages)
                    ),
                    occurred_at=listing_row.updated_at,
                )

        committed_by_component_product: dict[int, dict[str, Any]] = {}
        for parsed in parsed_bundle_listings:
            listing_row = parsed["row"]
            listing_qty = max(
                0,
                int(listing_row.quantity_listed or 0)
                - int(sold_qty_by_bundle_listing.get(int(listing_row.listing_id or 0), 0)),
            )
            if listing_qty <= 0:
                continue
            listing_ref = str(listing_row.external_listing_id or "").strip() or f"listing#{listing_row.listing_id}"
            for component in list(parsed.get("components") or []):
                product_id = int(component.get("product_id") or 0)
                if product_id <= 0:
                    continue
                qty_per_listing = max(1, int(component.get("quantity_per_listing") or 1))
                required_qty = qty_per_listing * listing_qty
                bucket = committed_by_component_product.setdefault(
                    product_id,
                    {
                        "sku": str(component.get("sku") or product_id).strip(),
                        "required_qty": 0,
                        "stock_qty": max(0, int(bundle_component_stock.get(product_id, 0))),
                        "listing_count": 0,
                        "references": [],
                        "latest_updated_at": listing_row.updated_at,
                    },
                )
                bucket["required_qty"] = int(bucket.get("required_qty") or 0) + required_qty
                bucket["listing_count"] = int(bucket.get("listing_count") or 0) + 1
                references = bucket.get("references")
                if isinstance(references, list) and listing_ref not in references:
                    references.append(listing_ref)
                latest_updated_at = bucket.get("latest_updated_at")
                if latest_updated_at is None or (
                    listing_row.updated_at is not None and listing_row.updated_at > latest_updated_at
                ):
                    bucket["latest_updated_at"] = listing_row.updated_at
        for product_id, bucket in committed_by_component_product.items():
            required_qty = int(bucket.get("required_qty") or 0)
            stock_qty = int(bucket.get("stock_qty") or 0)
            if required_qty <= stock_qty:
                continue
            references = [str(value) for value in list(bucket.get("references") or []) if str(value).strip()]
            add_exception(
                severity="P1",
                exception_type="active_bundle_component_overcommitted",
                entity_type="product",
                entity_id=int(product_id),
                sku=str(bucket.get("sku") or product_id).strip(),
                reference=", ".join(references[:5]),
                amount=float(required_qty - stock_qty),
                details=(
                    f"Active bundle listings collectively require {required_qty} unit(s), "
                    f"but current stock is {stock_qty}. "
                    f"Active bundle listing references: {', '.join(references[:5])}."
                ),
                occurred_at=bucket.get("latest_updated_at"),
            )

        matched_sale_order_ids = {
            int(row.order_id)
            for row in sale_rows
            if row.order_id is not None and int(row.order_id or 0) > 0
        }
        finance_label_rows = self.db.execute(
            select(
                OrderFinanceEntry.id.label("finance_entry_id"),
                OrderFinanceEntry.order_id.label("order_id"),
                OrderFinanceEntry.external_order_id.label("external_order_id"),
                OrderFinanceEntry.amount.label("amount"),
                OrderFinanceEntry.transaction_date.label("transaction_date"),
                OrderFinanceEntry.created_at.label("created_at"),
            )
            .where(
                OrderFinanceEntry.entry_kind == "shipping_label",
                or_(
                    and_(
                        OrderFinanceEntry.transaction_date.is_not(None),
                        OrderFinanceEntry.transaction_date >= start_dt,
                        OrderFinanceEntry.transaction_date <= end_dt,
                    ),
                    and_(
                        OrderFinanceEntry.transaction_date.is_(None),
                        OrderFinanceEntry.created_at >= start_dt,
                        OrderFinanceEntry.created_at <= end_dt,
                    ),
                ),
            )
            .order_by(OrderFinanceEntry.transaction_date.desc(), OrderFinanceEntry.id.desc())
        ).all()
        for finance_row in finance_label_rows:
            order_id = int(finance_row.order_id or 0)
            if order_id > 0 and order_id in matched_sale_order_ids:
                continue
            occurred = finance_row.transaction_date or finance_row.created_at
            add_exception(
                severity="P2",
                exception_type="unmatched_shipping_label_finance_entry",
                entity_type="order_finance_entry",
                entity_id=int(finance_row.finance_entry_id or 0),
                marketplace="ebay",
                reference=finance_row.external_order_id,
                amount=self._safe_float(finance_row.amount),
                details=(
                    "Shipping-label finance entry is in the reporting window but is not linked to any sale row "
                    "in the same window, so dashboard/Reports shipping delta ignores it until sale/order linkage is reconciled."
                ),
                occurred_at=occurred,
            )

        lot_rows = self.db.execute(
            select(
                PurchaseLot.id.label("lot_id"),
                PurchaseLot.lot_code.label("lot_code"),
                PurchaseLot.purchase_date.label("purchase_date"),
                PurchaseLot.total_cost.label("lot_total_cost"),
                PurchaseLot.total_tax_paid.label("lot_total_tax_paid"),
                PurchaseLot.total_shipping_paid.label("lot_total_shipping_paid"),
                PurchaseLot.total_handling_paid.label("lot_total_handling_paid"),
                PurchaseLot.expected_total_quantity.label("lot_expected_total_quantity"),
                ProductLotAssignment.id.label("assignment_id"),
                ProductLotAssignment.product_id.label("product_id"),
                ProductLotAssignment.quantity_acquired.label("quantity_acquired"),
                ProductLotAssignment.unit_cost.label("unit_cost"),
                ProductLotAssignment.unit_tax_paid.label("unit_tax_paid"),
                ProductLotAssignment.unit_shipping_paid.label("unit_shipping_paid"),
                ProductLotAssignment.unit_handling_paid.label("unit_handling_paid"),
                ProductLotAssignment.allocated_cost.label("allocated_cost"),
                ProductLotAssignment.allocated_tax_paid.label("allocated_tax_paid"),
                ProductLotAssignment.allocated_shipping_paid.label("allocated_shipping_paid"),
                ProductLotAssignment.allocated_handling_paid.label("allocated_handling_paid"),
                ProductLotAssignment.allocation_weight.label("allocation_weight"),
            )
            .select_from(PurchaseLot)
            .outerjoin(ProductLotAssignment, ProductLotAssignment.lot_id == PurchaseLot.id)
            .where(PurchaseLot.purchase_date <= end_dt)
            .order_by(PurchaseLot.purchase_date.desc(), PurchaseLot.id.desc())
        ).all()
        lot_buckets: dict[int, dict[str, Any]] = {}
        for row in lot_rows:
            lot_id = int(row.lot_id or 0)
            if lot_id <= 0:
                continue
            bucket = lot_buckets.setdefault(
                lot_id,
                {
                    "lot_code": str(row.lot_code or "").strip(),
                    "purchase_date": row.purchase_date,
                    "lot_total": self._lot_landed_total_from_assignment_row(row),
                    "expected_total_quantity": int(row.lot_expected_total_quantity or 0),
                    "explicit_total": 0.0,
                    "blank_qty": 0,
                    "blank_assignment_count": 0,
                    "blank_product_ids": set(),
                    "weighted_blank_assignment_count": 0,
                    "assigned_qty": 0,
                    "assignment_count": 0,
                },
            )
            if int(row.assignment_id or 0) <= 0:
                continue
            bucket["assignment_count"] = int(bucket["assignment_count"] or 0) + 1
            qty = max(0, int(row.quantity_acquired or 0))
            bucket["assigned_qty"] = int(bucket["assigned_qty"] or 0) + qty
            explicit_unit = self._explicit_landed_unit_cost_from_assignment_row(row)
            if explicit_unit > 0:
                bucket["explicit_total"] = self._safe_float(bucket["explicit_total"]) + (explicit_unit * qty)
            else:
                bucket["blank_qty"] = int(bucket["blank_qty"] or 0) + qty
                bucket["blank_assignment_count"] = int(bucket["blank_assignment_count"] or 0) + 1
                blank_product_ids = bucket.get("blank_product_ids")
                if isinstance(blank_product_ids, set) and int(row.product_id or 0) > 0:
                    blank_product_ids.add(int(row.product_id or 0))
                if self._safe_float(row.allocation_weight) > 0:
                    bucket["weighted_blank_assignment_count"] = int(bucket["weighted_blank_assignment_count"] or 0) + 1

        for lot_id, bucket in lot_buckets.items():
            lot_total = self._safe_float(bucket.get("lot_total"))
            explicit_total = self._safe_float(bucket.get("explicit_total"))
            blank_qty = int(bucket.get("blank_qty") or 0)
            assigned_qty = int(bucket.get("assigned_qty") or 0)
            expected_total_quantity = int(bucket.get("expected_total_quantity") or 0)
            assignment_count = int(bucket.get("assignment_count") or 0)
            blank_assignment_count = int(bucket.get("blank_assignment_count") or 0)
            blank_product_count = len(bucket.get("blank_product_ids") or set())
            weighted_blank_assignment_count = int(bucket.get("weighted_blank_assignment_count") or 0)
            if expected_total_quantity > assigned_qty and lot_total > 0:
                add_exception(
                    severity="P2",
                    exception_type="lot_allocation_pending_check_in",
                    entity_type="purchase_lot",
                    entity_id=lot_id,
                    reference=bucket.get("lot_code"),
                    amount=lot_total,
                    details=(
                        f"Lot expects {expected_total_quantity} total units/items but only {assigned_qty} are assigned. "
                        "Whole-lot fallback costs are using the expected quantity until check-in is complete."
                    ),
                    occurred_at=bucket.get("purchase_date"),
                )
            if (
                lot_total > 0
                and blank_assignment_count > 1
                and blank_product_count > 1
                and weighted_blank_assignment_count <= 0
                and expected_total_quantity <= 0
            ):
                add_exception(
                    severity="P2",
                    exception_type="lot_equal_fallback_review_needed",
                    entity_type="purchase_lot",
                    entity_id=lot_id,
                    reference=bucket.get("lot_code"),
                    amount=lot_total,
                    details=(
                        f"Lot has {blank_assignment_count} blank-cost assignments across {blank_product_count} products "
                        "with no allocation weights or expected quantity. COGS is using equal quantity fallback; review mixed-lot cost allocation."
                    ),
                    occurred_at=bucket.get("purchase_date"),
                )
            if lot_total <= 0 and assignment_count > 0 and blank_qty > 0:
                add_exception(
                    severity="P1",
                    exception_type="blank_lot_assignment_without_lot_total",
                    entity_type="purchase_lot",
                    entity_id=lot_id,
                    reference=bucket.get("lot_code"),
                    amount=lot_total,
                    details="Lot has blank-cost assignments but no positive landed lot total to allocate.",
                    occurred_at=bucket.get("purchase_date"),
                )
            if lot_total > 0 and explicit_total - lot_total > 0.01:
                add_exception(
                    severity="P0",
                    exception_type="lot_overallocated",
                    entity_type="purchase_lot",
                    entity_id=lot_id,
                    reference=bucket.get("lot_code"),
                    amount=explicit_total - lot_total,
                    details=f"Explicit assignment landed total {explicit_total:.2f} exceeds lot landed total {lot_total:.2f}.",
                    occurred_at=bucket.get("purchase_date"),
                )
            if lot_total > 0 and blank_qty <= 0 and assignment_count > 0 and lot_total - explicit_total > 0.01:
                add_exception(
                    severity="P1",
                    exception_type="lot_underallocated",
                    entity_type="purchase_lot",
                    entity_id=lot_id,
                    reference=bucket.get("lot_code"),
                    amount=lot_total - explicit_total,
                    details=f"Lot landed total {lot_total:.2f} exceeds explicit assignment landed total {explicit_total:.2f}.",
                    occurred_at=bucket.get("purchase_date"),
                )

        severity_rank = {"P0": 0, "P1": 1, "P2": 2}
        return sorted(
            rows,
            key=lambda row: (
                severity_rank.get(str(row.get("severity") or ""), 99),
                str(row.get("exception_type") or ""),
                str(row.get("occurred_at") or ""),
                int(row.get("entity_id") or 0),
            ),
        )

    def report_sales_actual_econ_rows(
        self,
        *,
        start_dt: datetime,
        end_dt: datetime,
    ) -> list[dict]:
        ListingProduct = aliased(Product)
        sold_price_expr = func.coalesce(Sale.sold_price, 0)
        sale_fee_expr = func.coalesce(Sale.fees, 0)
        sale_shipping_charged_expr = func.coalesce(Sale.shipping_cost, 0)
        sale_shipping_label_expr = func.coalesce(Sale.shipping_label_cost, 0)
        sibling_gross_total_expr = func.sum(sold_price_expr).over(partition_by=Sale.order_id)
        sibling_fee_total_expr = func.sum(sale_fee_expr).over(partition_by=Sale.order_id)
        sibling_shipping_charged_total_expr = func.sum(sale_shipping_charged_expr).over(partition_by=Sale.order_id)
        sibling_shipping_label_total_expr = func.sum(sale_shipping_label_expr).over(partition_by=Sale.order_id)
        sibling_count_expr = func.count(Sale.id).over(partition_by=Sale.order_id)
        weight_expr = case(
            (Sale.order_id.is_(None), literal(1.0)),
            (sibling_gross_total_expr > 0, sold_price_expr / func.nullif(sibling_gross_total_expr, 0)),
            (sibling_count_expr > 0, literal(1.0) / func.nullif(sibling_count_expr, 0)),
            else_=literal(1.0),
        )
        order_fee_total_expr = case(
            (and_(Sale.order_id.is_not(None), func.coalesce(Order.fees, 0) > 0), func.coalesce(Order.fees, 0)),
            (and_(Sale.order_id.is_not(None), sibling_fee_total_expr > 0), sibling_fee_total_expr),
            else_=sale_fee_expr,
        )
        order_shipping_charged_total_expr = case(
            (
                and_(Sale.order_id.is_not(None), func.coalesce(Order.shipping_cost, 0) > 0),
                func.coalesce(Order.shipping_cost, 0),
            ),
            (and_(Sale.order_id.is_not(None), sibling_shipping_charged_total_expr > 0), sibling_shipping_charged_total_expr),
            else_=sale_shipping_charged_expr,
        )
        order_shipping_actual_total_expr = case(
            (
                and_(Sale.order_id.is_not(None), func.coalesce(Order.shipping_label_cost, 0) > 0),
                func.coalesce(Order.shipping_label_cost, 0),
            ),
            (and_(Sale.order_id.is_not(None), sibling_shipping_label_total_expr > 0), sibling_shipping_label_total_expr),
            else_=sale_shipping_label_expr,
        )
        allocated_fee_expr = order_fee_total_expr * weight_expr
        allocated_shipping_charged_expr = order_shipping_charged_total_expr * weight_expr
        allocated_shipping_actual_expr = order_shipping_actual_total_expr * weight_expr
        net_before_cogs_actual_expr = (
            sold_price_expr
            + allocated_shipping_charged_expr
            - allocated_fee_expr
            - allocated_shipping_actual_expr
        )

        rows = self.db.execute(
            select(
                Sale.id.label("sale_id"),
                Sale.sold_at.label("sold_at"),
                Sale.order_id.label("order_id"),
                func.lower(func.coalesce(cast(Sale.marketplace, String), "")).label("marketplace"),
                Sale.external_order_id.label("external_order_id"),
                func.coalesce(Product.sku, ListingProduct.sku).label("sku"),
                func.coalesce(Product.title, ListingProduct.title).label("product_title"),
                func.coalesce(Sale.quantity_sold, 0).label("qty"),
                sold_price_expr.label("sold_price"),
                weight_expr.label("allocation_weight"),
                order_fee_total_expr.label("order_fee_total_actual"),
                order_shipping_charged_total_expr.label("order_shipping_charged_total"),
                order_shipping_actual_total_expr.label("order_shipping_actual_total"),
                allocated_fee_expr.label("allocated_fee_actual"),
                allocated_shipping_charged_expr.label("allocated_shipping_charged"),
                allocated_shipping_actual_expr.label("allocated_shipping_actual"),
                (allocated_shipping_charged_expr - allocated_shipping_actual_expr).label(
                    "shipping_delta_charged_minus_actual"
                ),
                net_before_cogs_actual_expr.label("net_before_cogs_actual"),
            )
            .select_from(Sale)
            .outerjoin(Order, Order.id == Sale.order_id)
            .outerjoin(Product, Product.id == Sale.product_id)
            .outerjoin(MarketplaceListing, MarketplaceListing.id == Sale.listing_id)
            .outerjoin(ListingProduct, ListingProduct.id == MarketplaceListing.product_id)
            .where(
                Sale.sold_at.is_not(None),
                Sale.sold_at >= start_dt,
                Sale.sold_at <= end_dt,
            )
            .order_by(Sale.sold_at.desc(), Sale.id.desc())
        ).all()
        order_ids = sorted({int(row.order_id) for row in rows if row.order_id is not None})
        normalized_fee_by_order: dict[int, float] = {}
        normalized_label_by_order: dict[int, float] = {}
        if order_ids:
            normalized_fee_by_order = {
                int(order_id): float(total or 0)
                for order_id, total in self.db.execute(
                    select(
                        OrderFinanceEntry.order_id,
                        func.coalesce(func.sum(func.coalesce(OrderFinanceEntry.amount, 0)), 0),
                    )
                    .where(
                        OrderFinanceEntry.order_id.in_(order_ids),
                        OrderFinanceEntry.entry_kind == "marketplace_fee",
                    )
                    .group_by(OrderFinanceEntry.order_id)
                ).all()
            }
            normalized_label_by_order = {
                int(order_id): float(total or 0)
                for order_id, total in self.db.execute(
                    select(
                        OrderFinanceEntry.order_id,
                        func.coalesce(func.sum(func.coalesce(OrderFinanceEntry.amount, 0)), 0),
                    )
                    .where(
                        OrderFinanceEntry.order_id.in_(order_ids),
                        OrderFinanceEntry.entry_kind == "shipping_label",
                    )
                    .group_by(OrderFinanceEntry.order_id)
                ).all()
            }
        result: list[dict] = []
        for row in rows:
            order_id = int(row.order_id) if row.order_id is not None else None
            weight = float(row.allocation_weight or 0)
            sold_price = float(row.sold_price or 0)
            fee_total_actual = (
                normalized_fee_by_order[order_id]
                if order_id is not None and order_id in normalized_fee_by_order
                else float(row.order_fee_total_actual or 0)
            )
            shipping_actual_total = (
                normalized_label_by_order[order_id]
                if order_id is not None and order_id in normalized_label_by_order
                else float(row.order_shipping_actual_total or 0)
            )
            shipping_charged_total = float(row.order_shipping_charged_total or 0)
            allocated_fee = fee_total_actual * weight
            allocated_shipping_charged = shipping_charged_total * weight
            allocated_shipping_actual = shipping_actual_total * weight
            net_before_cogs_actual = sold_price + allocated_shipping_charged - allocated_fee - allocated_shipping_actual
            result.append(
                {
                    "sale_id": int(row.sale_id or 0),
                    "sold_at": row.sold_at.isoformat() if row.sold_at else None,
                    "order_id": order_id,
                    "marketplace": str(row.marketplace or "").strip().lower(),
                    "external_order_id": str(row.external_order_id or "").strip(),
                    "sku": str(row.sku or "").strip() or None,
                    "product_title": str(row.product_title or "").strip() or None,
                    "qty": int(row.qty or 0),
                    "sold_price": round(sold_price, 2),
                    "allocation_weight": round(weight, 6),
                    "order_fee_total_actual": round(fee_total_actual, 2),
                    "order_shipping_charged_total": round(shipping_charged_total, 2),
                    "order_shipping_actual_total": round(shipping_actual_total, 2),
                    "actual_fee_source": (
                        "normalized_order_finance_entries_marketplace_fee_sum"
                        if order_id is not None and order_id in normalized_fee_by_order
                        else "order_or_sale_fee_field"
                    ),
                    "actual_shipping_source": (
                        "normalized_order_finance_entries_shipping_label_sum"
                        if order_id is not None and order_id in normalized_label_by_order
                        else "order_or_sale_shipping_label_field"
                    ),
                    "allocated_fee_actual": round(allocated_fee, 2),
                    "allocated_shipping_charged": round(allocated_shipping_charged, 2),
                    "allocated_shipping_actual": round(allocated_shipping_actual, 2),
                    "shipping_delta_charged_minus_actual": round(allocated_shipping_charged - allocated_shipping_actual, 2),
                    "net_before_cogs_actual": round(net_before_cogs_actual, 2),
                }
            )
        return result

    def report_economics_intelligence_fact_rows(
        self,
        *,
        start_dt: datetime,
        end_dt: datetime,
        marketplaces: set[str] | None = None,
    ) -> list[dict]:
        ListingProduct = aliased(Product)
        def _safe_float(value: Any) -> float:
            try:
                return float(value or 0)
            except (TypeError, ValueError):
                return 0.0

        def _extract_listing_fee_estimate(details_raw: str | None) -> dict:
            payload_raw = str(details_raw or "").strip()
            if not payload_raw:
                return {}
            try:
                payload = json.loads(payload_raw)
            except Exception:
                return {}
            if not isinstance(payload, dict):
                return {}
            ebay_publish = payload.get("ebay_publish")
            if not isinstance(ebay_publish, dict):
                return {}
            fee_estimate = ebay_publish.get("fee_estimate")
            return fee_estimate if isinstance(fee_estimate, dict) else {}

        marketplace_filter = {str(v).strip().lower() for v in (marketplaces or set()) if str(v).strip()}

        sold_price_expr = func.coalesce(Sale.sold_price, 0)
        sale_fee_expr = func.coalesce(Sale.fees, 0)
        sale_shipping_charged_expr = func.coalesce(Sale.shipping_cost, 0)
        sale_shipping_label_expr = func.coalesce(Sale.shipping_label_cost, 0)
        sibling_gross_total_expr = func.sum(sold_price_expr).over(partition_by=Sale.order_id)
        sibling_fee_total_expr = func.sum(sale_fee_expr).over(partition_by=Sale.order_id)
        sibling_shipping_charged_total_expr = func.sum(sale_shipping_charged_expr).over(partition_by=Sale.order_id)
        sibling_shipping_label_total_expr = func.sum(sale_shipping_label_expr).over(partition_by=Sale.order_id)
        sibling_count_expr = func.count(Sale.id).over(partition_by=Sale.order_id)
        weight_expr = case(
            (Sale.order_id.is_(None), literal(1.0)),
            (sibling_gross_total_expr > 0, sold_price_expr / func.nullif(sibling_gross_total_expr, 0)),
            (sibling_count_expr > 0, literal(1.0) / func.nullif(sibling_count_expr, 0)),
            else_=literal(1.0),
        )
        order_fee_total_expr = case(
            (and_(Sale.order_id.is_not(None), func.coalesce(Order.fees, 0) > 0), func.coalesce(Order.fees, 0)),
            (and_(Sale.order_id.is_not(None), sibling_fee_total_expr > 0), sibling_fee_total_expr),
            else_=sale_fee_expr,
        )
        order_shipping_charged_total_expr = case(
            (
                and_(Sale.order_id.is_not(None), func.coalesce(Order.shipping_cost, 0) > 0),
                func.coalesce(Order.shipping_cost, 0),
            ),
            (and_(Sale.order_id.is_not(None), sibling_shipping_charged_total_expr > 0), sibling_shipping_charged_total_expr),
            else_=sale_shipping_charged_expr,
        )
        order_shipping_actual_total_expr = case(
            (
                and_(Sale.order_id.is_not(None), func.coalesce(Order.shipping_label_cost, 0) > 0),
                func.coalesce(Order.shipping_label_cost, 0),
            ),
            (and_(Sale.order_id.is_not(None), sibling_shipping_label_total_expr > 0), sibling_shipping_label_total_expr),
            else_=sale_shipping_label_expr,
        )
        allocated_fee_expr = order_fee_total_expr * weight_expr
        allocated_shipping_charged_expr = order_shipping_charged_total_expr * weight_expr
        allocated_shipping_actual_expr = order_shipping_actual_total_expr * weight_expr
        net_before_cogs_actual_expr = (
            sold_price_expr
            + allocated_shipping_charged_expr
            - allocated_fee_expr
            - allocated_shipping_actual_expr
        )

        query = (
            select(
                Sale.id.label("sale_id"),
                Sale.sold_at.label("sold_at"),
                Sale.order_id.label("order_id"),
                Sale.listing_id.label("listing_id"),
                func.lower(func.coalesce(cast(Sale.marketplace, String), "")).label("marketplace"),
                Sale.external_order_id.label("external_order_id"),
                func.coalesce(Product.id, ListingProduct.id).label("product_id"),
                func.coalesce(Product.sku, ListingProduct.sku).label("sku"),
                func.coalesce(Product.title, ListingProduct.title).label("product_title"),
                func.coalesce(Sale.quantity_sold, 0).label("qty"),
                sold_price_expr.label("sold_price"),
                weight_expr.label("allocation_weight"),
                order_fee_total_expr.label("order_fee_total_actual"),
                order_shipping_charged_total_expr.label("order_shipping_charged_total"),
                order_shipping_actual_total_expr.label("order_shipping_actual_total"),
                allocated_fee_expr.label("allocated_fee_actual"),
                allocated_shipping_charged_expr.label("allocated_shipping_charged"),
                allocated_shipping_actual_expr.label("allocated_shipping_actual"),
                net_before_cogs_actual_expr.label("net_before_cogs_actual"),
                MarketplaceListing.marketplace_details.label("listing_marketplace_details"),
                MarketplaceListing.quantity_listed.label("listing_quantity"),
            )
            .select_from(Sale)
            .outerjoin(Order, Order.id == Sale.order_id)
            .outerjoin(Product, Product.id == Sale.product_id)
            .outerjoin(MarketplaceListing, MarketplaceListing.id == Sale.listing_id)
            .outerjoin(ListingProduct, ListingProduct.id == MarketplaceListing.product_id)
            .where(
                Sale.sold_at.is_not(None),
                Sale.sold_at >= start_dt,
                Sale.sold_at <= end_dt,
            )
            .order_by(Sale.sold_at.desc(), Sale.id.desc())
        )
        if marketplace_filter:
            query = query.where(func.lower(func.coalesce(cast(Sale.marketplace, String), "")).in_(sorted(marketplace_filter)))
        rows = self.db.execute(query).all()
        order_ids = sorted({int(row.order_id) for row in rows if row.order_id is not None})
        normalized_fee_by_order: dict[int, float] = {}
        normalized_label_by_order: dict[int, float] = {}
        if order_ids:
            normalized_fee_by_order = {
                int(order_id): float(total or 0)
                for order_id, total in self.db.execute(
                    select(
                        OrderFinanceEntry.order_id,
                        func.coalesce(func.sum(func.coalesce(OrderFinanceEntry.amount, 0)), 0),
                    )
                    .where(
                        OrderFinanceEntry.order_id.in_(order_ids),
                        OrderFinanceEntry.entry_kind == "marketplace_fee",
                    )
                    .group_by(OrderFinanceEntry.order_id)
                ).all()
            }
            normalized_label_by_order = {
                int(order_id): float(total or 0)
                for order_id, total in self.db.execute(
                    select(
                        OrderFinanceEntry.order_id,
                        func.coalesce(func.sum(func.coalesce(OrderFinanceEntry.amount, 0)), 0),
                    )
                    .where(
                        OrderFinanceEntry.order_id.in_(order_ids),
                        OrderFinanceEntry.entry_kind == "shipping_label",
                    )
                    .group_by(OrderFinanceEntry.order_id)
                ).all()
            }

        result: list[dict] = []
        for row in rows:
            qty = max(1, int(row.qty or 0))
            order_id = int(row.order_id) if row.order_id is not None else None
            weight = float(row.allocation_weight or 0)
            sold_price = round(float(row.sold_price or 0), 2)
            actual_fee_total = (
                normalized_fee_by_order[order_id]
                if order_id is not None and order_id in normalized_fee_by_order
                else float(row.order_fee_total_actual or 0)
            )
            actual_shipping_total = (
                normalized_label_by_order[order_id]
                if order_id is not None and order_id in normalized_label_by_order
                else float(row.order_shipping_actual_total or 0)
            )
            actual_fee_alloc = round(actual_fee_total * weight, 2)
            expected_shipping_alloc = round(float(row.order_shipping_charged_total or 0) * weight, 2)
            actual_shipping_alloc = round(actual_shipping_total * weight, 2)
            actual_net_before_cogs = round(sold_price + expected_shipping_alloc - actual_fee_alloc - actual_shipping_alloc, 2)

            fee_estimate = _extract_listing_fee_estimate(getattr(row, "listing_marketplace_details", None))
            fee_estimate_total = _safe_float(fee_estimate.get("estimated_total_fees"))
            fee_estimate_qty = int(_safe_float(fee_estimate.get("quantity") or 0))
            if fee_estimate_qty <= 0:
                fee_estimate_qty = int(row.listing_quantity or 0)
            estimate_available = bool(fee_estimate_total > 0 and fee_estimate_qty > 0)
            estimated_fee_alloc: float | None = None
            estimated_net_before_cogs: float | None = None
            fee_variance_actual_minus_estimated: float | None = None
            net_variance_actual_minus_estimated: float | None = None
            if estimate_available:
                estimated_fee_alloc = round((fee_estimate_total / max(1, fee_estimate_qty)) * qty, 2)
                estimated_net_before_cogs = round(
                    sold_price + expected_shipping_alloc - estimated_fee_alloc - actual_shipping_alloc,
                    2,
                )
                fee_variance_actual_minus_estimated = round(actual_fee_alloc - estimated_fee_alloc, 2)
                net_variance_actual_minus_estimated = round(actual_net_before_cogs - estimated_net_before_cogs, 2)

            result.append(
                {
                    "sale_id": int(row.sale_id or 0),
                    "sold_at": row.sold_at,
                    "marketplace": str(row.marketplace or "").strip().lower(),
                    "order_id": order_id,
                    "listing_id": int(row.listing_id or 0) if row.listing_id is not None else None,
                    "external_order_id": str(row.external_order_id or "").strip(),
                    "product_id": int(row.product_id or 0) if row.product_id is not None else None,
                    "sku": str(row.sku or "").strip() or None,
                    "product_title": str(row.product_title or "").strip() or None,
                    "qty": qty,
                    "sold_price": sold_price,
                    "allocation_weight": round(weight, 6),
                    "estimated_fee_alloc": estimated_fee_alloc,
                    "expected_shipping_alloc": expected_shipping_alloc,
                    "estimated_net_before_cogs": estimated_net_before_cogs,
                    "actual_fee_alloc": actual_fee_alloc,
                    "actual_fee_source": (
                        "normalized_order_finance_entries_marketplace_fee_sum"
                        if order_id is not None and order_id in normalized_fee_by_order
                        else "order_or_sale_fee_field"
                    ),
                    "actual_shipping_alloc": actual_shipping_alloc,
                    "actual_shipping_source": (
                        "normalized_order_finance_entries_shipping_label_sum"
                        if order_id is not None and order_id in normalized_label_by_order
                        else "order_or_sale_shipping_label_field"
                    ),
                    "actual_net_before_cogs": actual_net_before_cogs,
                    "fee_variance_actual_minus_estimated": fee_variance_actual_minus_estimated,
                    "shipping_delta_expected_minus_actual": round(expected_shipping_alloc - actual_shipping_alloc, 2),
                    "net_variance_actual_minus_estimated": net_variance_actual_minus_estimated,
                    "estimate_available": estimate_available,
                }
            )
        return result

    def report_listing_review_activity_rows(
        self,
        *,
        start_dt: datetime,
        end_dt: datetime,
    ) -> list[dict]:
        rows = self.db.execute(
            select(
                MarketplaceListing.id.label("listing_id"),
                func.lower(func.coalesce(cast(MarketplaceListing.marketplace, String), "")).label("marketplace"),
                Product.sku.label("sku"),
                MarketplaceListing.listing_title.label("listing_title"),
                MarketplaceListing.marketplace_details.label("marketplace_details"),
            )
            .select_from(MarketplaceListing)
            .outerjoin(Product, Product.id == MarketplaceListing.product_id)
            .where(
                MarketplaceListing.marketplace_details.is_not(None),
                func.length(func.trim(func.coalesce(MarketplaceListing.marketplace_details, ""))) > 0,
            )
            .order_by(MarketplaceListing.id.desc())
        ).all()

        result: list[dict] = []
        for row in rows:
            payload_raw = str(row.marketplace_details or "").strip()
            if not payload_raw:
                continue
            try:
                payload = json.loads(payload_raw)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            history = payload.get("review_history")
            if not isinstance(history, list):
                continue
            for event in history:
                if not isinstance(event, dict):
                    continue
                reviewed_at_raw = str(event.get("reviewed_at") or "").strip()
                if not reviewed_at_raw:
                    continue
                try:
                    reviewed_at_dt = datetime.fromisoformat(reviewed_at_raw.replace("Z", "+00:00")).replace(tzinfo=None)
                except Exception:
                    continue
                if not (start_dt <= reviewed_at_dt <= end_dt):
                    continue
                result.append(
                    {
                        "listing_id": int(row.listing_id or 0),
                        "marketplace": str(row.marketplace or "").strip().lower(),
                        "sku": str(row.sku or "").strip() or None,
                        "listing_title": str(row.listing_title or "").strip(),
                        "review_decision": str(event.get("decision") or "").strip().lower(),
                        "reviewed_by": str(event.get("actor") or "").strip(),
                        "reviewed_at": reviewed_at_dt.isoformat(),
                        "review_date": reviewed_at_dt.date().isoformat(),
                        "review_notes": str(event.get("notes") or "").strip(),
                    }
                )
        return sorted(result, key=lambda x: (x.get("reviewed_at") or "", x.get("listing_id") or 0), reverse=True)

    def report_listing_format_outcome_rows(
        self,
        *,
        start_dt: datetime,
        end_dt: datetime,
    ) -> list[dict]:
        rows = self.db.execute(
            select(
                MarketplaceListing.id.label("listing_id"),
                MarketplaceListing.listed_at.label("listed_at"),
                func.lower(func.coalesce(cast(MarketplaceListing.marketplace, String), "")).label("marketplace"),
                Product.sku.label("sku"),
                MarketplaceListing.listing_title.label("listing_title"),
                MarketplaceListing.review_status.label("review_status"),
                MarketplaceListing.listing_status.label("listing_status"),
                MarketplaceListing.external_listing_id.label("external_listing_id"),
                MarketplaceListing.marketplace_details.label("marketplace_details"),
            )
            .select_from(MarketplaceListing)
            .outerjoin(Product, Product.id == MarketplaceListing.product_id)
            .order_by(MarketplaceListing.id.desc())
        ).all()
        # Keep null-listed rows as prior behavior did, while filtering dated rows in python
        result: list[dict] = []
        for row in rows:
            listed_at = row.listed_at
            if listed_at is not None and not (start_dt <= listed_at <= end_dt):
                continue
            meta: dict[str, Any] = {}
            details_raw = str(row.marketplace_details or "").strip()
            if details_raw:
                try:
                    parsed = json.loads(details_raw)
                    if isinstance(parsed, dict):
                        publish_meta = parsed.get("ebay_publish")
                        if isinstance(publish_meta, dict):
                            meta = publish_meta
                except Exception:
                    meta = {}
            intent_format = str(meta.get("format") or meta.get("format_type") or "FIXED_PRICE").strip().upper()
            if intent_format not in {"FIXED_PRICE", "AUCTION"}:
                intent_format = "FIXED_PRICE"
            intent_duration = str(meta.get("listing_duration") or "").strip().upper()
            publish_history = meta.get("history") if isinstance(meta.get("history"), list) else []
            publish_attempt_count = len(publish_history)
            publish_success_count = len(
                [
                    h
                    for h in publish_history
                    if str((h or {}).get("status") or "").strip().lower() in {"published", "success"}
                ]
            )
            publish_error_events = [
                h for h in publish_history if str((h or {}).get("status") or "").strip().lower() in {"error", "failed"}
            ]
            publish_error_count = len(publish_error_events)
            last_error = ""
            if publish_error_events:
                last_error = str((publish_error_events[-1] or {}).get("error") or "").strip()
            published_at = str(meta.get("published_at") or "").strip()
            external_listing_id = str(row.external_listing_id or "").strip()
            listing_state = str(row.listing_status or "").strip().lower()
            if external_listing_id and listing_state in {"active", "ended", "sold"}:
                publish_outcome = "published"
            elif publish_error_count > 0:
                publish_outcome = "publish_error"
            elif publish_attempt_count > 0:
                publish_outcome = "attempted_no_publish"
            else:
                publish_outcome = "not_attempted"
            result.append(
                {
                    "listing_id": int(row.listing_id or 0),
                    "listed_at": listed_at.isoformat() if listed_at is not None else None,
                    "marketplace": str(row.marketplace or "").strip().lower(),
                    "sku": str(row.sku or "").strip() or None,
                    "listing_title": str(row.listing_title or "").strip(),
                    "review_status": str(row.review_status or "").strip().lower(),
                    "listing_status": listing_state,
                    "intent_format": intent_format,
                    "intent_duration": intent_duration,
                    "intent_best_offer_enabled": bool(meta.get("best_offer_enabled")),
                    "intent_auction_start_price": round(float(meta.get("auction_start_price") or 0), 2),
                    "intent_auction_reserve_price": round(float(meta.get("auction_reserve_price") or 0), 2),
                    "intent_auction_buy_now_price": round(float(meta.get("auction_buy_now_price") or 0), 2),
                    "publish_attempt_count": int(publish_attempt_count),
                    "publish_success_count": int(publish_success_count),
                    "publish_error_count": int(publish_error_count),
                    "publish_outcome": publish_outcome,
                    "published_at": published_at or None,
                    "published_listing_id": external_listing_id or None,
                    "last_publish_error": last_error or None,
                }
            )
        return sorted(
            result,
            key=lambda x: (str(x.get("listed_at") or ""), int(x.get("listing_id") or 0)),
            reverse=True,
        )

    def report_rebuy_cost_trend_rows(
        self,
        *,
        end_dt: datetime | None = None,
    ) -> list[dict]:
        cutoff = end_dt or utcnow_naive()

        product_rows = self.db.execute(
            select(
                Product.id.label("product_id"),
                Product.sku.label("sku"),
                Product.title.label("title"),
            )
            .select_from(Product)
        ).all()
        product_by_id = {
            int(row.product_id): {
                "sku": str(row.sku or "").strip() or None,
                "title": str(row.title or "").strip() or None,
            }
            for row in product_rows
            if int(row.product_id or 0) > 0
        }

        assignment_rows = self.db.execute(
            select(
                ProductLotAssignment.id.label("assignment_id"),
                ProductLotAssignment.product_id.label("product_id"),
                ProductLotAssignment.acquired_at.label("acquired_at"),
                ProductLotAssignment.quantity_acquired.label("quantity_acquired"),
                ProductLotAssignment.unit_cost.label("unit_cost"),
            )
            .select_from(ProductLotAssignment)
            .where(
                ProductLotAssignment.product_id.is_not(None),
                ProductLotAssignment.acquired_at <= cutoff,
            )
        ).all()

        movement_rows = self.db.execute(
            select(
                InventoryMovement.id.label("movement_id"),
                InventoryMovement.product_id.label("product_id"),
                InventoryMovement.occurred_at.label("occurred_at"),
                func.lower(func.coalesce(cast(InventoryMovement.movement_type, String), "")).label("movement_type"),
                InventoryMovement.quantity_delta.label("quantity_delta"),
                InventoryMovement.unit_cost.label("unit_cost"),
            )
            .select_from(InventoryMovement)
            .where(
                InventoryMovement.product_id.is_not(None),
                InventoryMovement.occurred_at <= cutoff,
                func.lower(func.coalesce(cast(InventoryMovement.movement_type, String), "")).in_(
                    ["initial_stock", "repurchase_in"]
                ),
            )
        ).all()

        assignment_keys = set()
        acquisition_events: dict[int, list[dict]] = {}

        for row in assignment_rows:
            pid = int(row.product_id or 0)
            qty = max(0, int(row.quantity_acquired or 0))
            unit_cost = float(row.unit_cost or 0)
            if pid <= 0 or qty <= 0 or unit_cost <= 0:
                continue
            ts = row.acquired_at or datetime.min
            key = (pid, ts, qty, round(unit_cost, 6))
            assignment_keys.add(key)
            acquisition_events.setdefault(pid, []).append(
                {
                    "occurred_at": ts,
                    "event_type": "lot_assignment",
                    "qty_in": qty,
                    "unit_cost": unit_cost,
                    "source_ref": f"assignment:{int(row.assignment_id or 0)}",
                }
            )

        for row in movement_rows:
            pid = int(row.product_id or 0)
            qty = max(0, int(row.quantity_delta or 0))
            unit_cost = float(row.unit_cost or 0)
            if pid <= 0 or qty <= 0 or unit_cost <= 0:
                continue
            ts = row.occurred_at or datetime.min
            key = (pid, ts, qty, round(unit_cost, 6))
            if key in assignment_keys:
                continue
            acquisition_events.setdefault(pid, []).append(
                {
                    "occurred_at": ts,
                    "event_type": str(row.movement_type or "").strip().lower() or "repurchase_in",
                    "qty_in": qty,
                    "unit_cost": unit_cost,
                    "source_ref": f"movement:{int(row.movement_id or 0)}",
                }
            )

        rows: list[dict] = []
        for pid, events in acquisition_events.items():
            product_meta = product_by_id.get(pid) or {}
            cumulative_qty = 0.0
            cumulative_cost = 0.0
            for idx, event in enumerate(
                sorted(events, key=lambda x: (x["occurred_at"], x["event_type"], x["source_ref"])),
                start=1,
            ):
                qty = float(event["qty_in"])
                unit_cost = float(event["unit_cost"] or 0)
                cumulative_qty += qty
                cumulative_cost += qty * unit_cost
                weighted_unit_cost = (cumulative_cost / cumulative_qty) if cumulative_qty > 0 else 0.0
                rows.append(
                    {
                        "product_id": int(pid),
                        "sku": product_meta.get("sku"),
                        "product_title": product_meta.get("title"),
                        "event_index": int(idx),
                        "as_of": event["occurred_at"].isoformat() if event["occurred_at"] else None,
                        "event_type": event["event_type"],
                        "qty_in": int(qty),
                        "unit_cost": round(unit_cost, 4),
                        "acquisition_value": round(qty * unit_cost, 2),
                        "cumulative_qty_acquired": round(cumulative_qty, 2),
                        "cumulative_acquisition_cost": round(cumulative_cost, 2),
                        "weighted_unit_cost": round(weighted_unit_cost, 4),
                        "source_ref": event["source_ref"],
                    }
                )

        return sorted(rows, key=lambda x: (x.get("sku") or "", x.get("event_index") or 0))

    def report_inventory_cycle_rows(
        self,
        *,
        end_dt: datetime | None = None,
    ) -> list[dict]:
        cutoff = end_dt or utcnow_naive()

        product_rows = self.db.execute(
            select(
                Product.id.label("product_id"),
                Product.sku.label("sku"),
                Product.title.label("title"),
            ).select_from(Product)
        ).all()
        product_by_id = {
            int(row.product_id): {
                "sku": str(row.sku or "").strip() or None,
                "title": str(row.title or "").strip() or None,
            }
            for row in product_rows
            if int(row.product_id or 0) > 0
        }

        movement_rows = self.db.execute(
            select(
                InventoryMovement.id.label("movement_id"),
                InventoryMovement.product_id.label("product_id"),
                InventoryMovement.occurred_at.label("occurred_at"),
                InventoryMovement.quantity_before.label("quantity_before"),
                InventoryMovement.quantity_after.label("quantity_after"),
                InventoryMovement.quantity_delta.label("quantity_delta"),
                InventoryMovement.unit_cost.label("unit_cost"),
            )
            .select_from(InventoryMovement)
            .where(
                InventoryMovement.product_id.is_not(None),
                InventoryMovement.occurred_at <= cutoff,
            )
            .order_by(InventoryMovement.occurred_at.asc(), InventoryMovement.id.asc())
        ).all()
        sales_rows = self.db.execute(
            select(
                Sale.id.label("sale_id"),
                Sale.product_id.label("product_id"),
                Sale.listing_id.label("listing_id"),
                Sale.sold_at.label("sold_at"),
                Sale.quantity_sold.label("quantity_sold"),
                Sale.sold_price.label("sold_price"),
                Sale.fees.label("fees"),
                Sale.shipping_cost.label("shipping_cost"),
                Sale.shipping_label_cost.label("shipping_label_cost"),
                MarketplaceListing.marketplace_details.label("listing_marketplace_details"),
            )
            .select_from(Sale)
            .outerjoin(MarketplaceListing, MarketplaceListing.id == Sale.listing_id)
            .where(
                Sale.sold_at.is_not(None),
                Sale.sold_at <= cutoff,
            )
            .order_by(Sale.sold_at.asc(), Sale.id.asc())
        ).all()

        movements_by_product: dict[int, list[Any]] = {}
        for row in movement_rows:
            pid = int(row.product_id or 0)
            if pid <= 0:
                continue
            movements_by_product.setdefault(pid, []).append(row)
        actual_econ_by_sale_id = {
            int(row.get("sale_id") or 0): row
            for row in self.report_sales_actual_econ_rows(
                start_dt=datetime.min,
                end_dt=cutoff,
            )
            if int(row.get("sale_id") or 0) > 0
        }
        sales_by_product: dict[int, list[Any]] = {}
        for row in sales_rows:
            sale_id = int(row.sale_id or 0)
            sale_qty = max(1, int(row.quantity_sold or 1))
            bundle_components = self._bundle_components_from_payload(
                self._listing_bundle_payload_from_raw(row.listing_marketplace_details),
                sale_qty,
            )
            component_rows = [
                {
                    "product_id": int(component.get("product_id") or 0),
                    "quantity_total": max(1, int(component.get("quantity_total") or 1)),
                }
                for component in bundle_components
                if int(component.get("product_id") or 0) > 0
            ]
            if component_rows:
                total_component_units = sum(int(component["quantity_total"]) for component in component_rows)
                actual = actual_econ_by_sale_id.get(sale_id) or {}
                gross_total = self._safe_float(row.sold_price)
                fee_total = self._safe_float(actual.get("allocated_fee_actual", row.fees))
                shipping_total = self._safe_float(actual.get("allocated_shipping_charged", row.shipping_cost))
                label_total = self._safe_float(
                    actual.get("allocated_shipping_actual", getattr(row, "shipping_label_cost", None))
                )
                for component in component_rows:
                    pid = int(component["product_id"])
                    component_qty = int(component["quantity_total"])
                    weight = (
                        float(component_qty) / float(total_component_units)
                        if total_component_units > 0
                        else 0.0
                    )
                    sales_by_product.setdefault(pid, []).append(
                        {
                            "sale_id": sale_id,
                            "sold_at": row.sold_at,
                            "quantity_sold": component_qty,
                            "sold_price": gross_total * weight,
                            "fees": fee_total * weight,
                            "shipping_cost": shipping_total * weight,
                            "shipping_label_cost": label_total * weight,
                            "actual_allocated": True,
                        }
                    )
                continue

            pid = int(row.product_id or 0)
            if pid <= 0:
                continue
            sales_by_product.setdefault(pid, []).append(row)

        def _apply_cycle_sale(current_cycle: dict, sale: Any) -> None:
            is_allocated_proxy = isinstance(sale, dict) and bool(sale.get("actual_allocated"))
            sale_id = int((sale.get("sale_id") if isinstance(sale, dict) else sale.sale_id) or 0)
            actual = {} if is_allocated_proxy else actual_econ_by_sale_id.get(sale_id) or {}
            fees_field = sale.get("fees") if isinstance(sale, dict) else sale.fees
            shipping_field = sale.get("shipping_cost") if isinstance(sale, dict) else sale.shipping_cost
            label_field = sale.get("shipping_label_cost") if isinstance(sale, dict) else getattr(sale, "shipping_label_cost", None)
            sold_price = sale.get("sold_price") if isinstance(sale, dict) else sale.sold_price
            quantity_sold = sale.get("quantity_sold") if isinstance(sale, dict) else sale.quantity_sold
            fee = (
                self._safe_float(actual.get("allocated_fee_actual"))
                if actual
                else self._safe_float(fees_field)
            )
            shipping_charged = (
                self._safe_float(actual.get("allocated_shipping_charged"))
                if actual
                else self._safe_float(shipping_field)
            )
            label_spend = (
                self._safe_float(actual.get("allocated_shipping_actual"))
                if actual
                else self._safe_float(label_field)
            )
            net_before_cogs = (
                self._safe_float(actual.get("net_before_cogs_actual"))
                if actual
                else self._safe_float(sold_price) + shipping_charged - fee - label_spend
            )
            current_cycle["sale_count"] += 1
            current_cycle["qty_sold_sales"] += int(quantity_sold or 0)
            current_cycle["gross_sales"] += self._safe_float(sold_price)
            current_cycle["fees"] += fee
            current_cycle["shipping_cost"] += shipping_charged
            current_cycle["shipping_label_cost"] += label_spend
            current_cycle["net_sales"] += net_before_cogs

        rows: list[dict] = []
        for product_id, product_movements in movements_by_product.items():
            product_meta = product_by_id.get(product_id) or {}
            product_sales = sorted(
                sales_by_product.get(product_id, []),
                key=lambda x: (
                    (x.get("sold_at") if isinstance(x, dict) else x.sold_at) or datetime.min,
                    int((x.get("sale_id") if isinstance(x, dict) else x.sale_id) or 0),
                ),
            )
            sales_idx = 0
            sorted_movements = sorted(
                product_movements,
                key=lambda x: (x.occurred_at or datetime.min, int(x.movement_id or 0)),
            )
            current_cycle: dict | None = None
            cycle_number = 0

            for mv in sorted_movements:
                before_qty = int(mv.quantity_before or 0)
                after_qty = int(mv.quantity_after or 0)
                qty_delta = int(mv.quantity_delta or 0)
                started_new_cycle = current_cycle is None and after_qty > 0
                if started_new_cycle:
                    cycle_number += 1
                    current_cycle = {
                        "product_id": int(product_id),
                        "sku": product_meta.get("sku"),
                        "product_title": product_meta.get("title"),
                        "cycle_number": int(cycle_number),
                        "cycle_id": f"{product_meta.get('sku') or product_id}-C{cycle_number}",
                        "cycle_start": mv.occurred_at,
                        "cycle_end": None,
                        "cycle_status": "open",
                        "start_qty_before": int(before_qty),
                        "end_qty_after": int(after_qty),
                        "qty_in": 0,
                        "qty_out_movements": 0,
                        "acquisition_cost_known": 0.0,
                        "movement_count": 0,
                        "sale_count": 0,
                        "qty_sold_sales": 0,
                        "gross_sales": 0.0,
                        "fees": 0.0,
                        "shipping_cost": 0.0,
                        "shipping_label_cost": 0.0,
                        "net_sales": 0.0,
                    }
                if current_cycle is None:
                    continue

                current_cycle["movement_count"] += 1
                current_cycle["end_qty_after"] = after_qty
                if qty_delta > 0:
                    current_cycle["qty_in"] += qty_delta
                    if mv.unit_cost is not None:
                        current_cycle["acquisition_cost_known"] += float(mv.unit_cost) * float(qty_delta)
                elif qty_delta < 0:
                    current_cycle["qty_out_movements"] += abs(qty_delta)

                cycle_start = current_cycle["cycle_start"] or datetime.min
                cycle_end_candidate = mv.occurred_at or datetime.min
                while sales_idx < len(product_sales):
                    sale = product_sales[sales_idx]
                    sold_at = (sale.get("sold_at") if isinstance(sale, dict) else sale.sold_at) or datetime.min
                    if sold_at < cycle_start:
                        sales_idx += 1
                        continue
                    if sold_at > cycle_end_candidate:
                        break
                    _apply_cycle_sale(current_cycle, sale)
                    sales_idx += 1

                if after_qty <= 0:
                    current_cycle["cycle_end"] = mv.occurred_at
                    current_cycle["cycle_status"] = "closed"
                    known_cost = float(current_cycle.get("acquisition_cost_known") or 0.0)
                    current_cycle["estimated_margin_vs_known_cost"] = float(current_cycle.get("net_sales") or 0.0) - known_cost
                    rows.append(current_cycle)
                    current_cycle = None

            if current_cycle is not None:
                while sales_idx < len(product_sales):
                    sale = product_sales[sales_idx]
                    sold_at = (sale.get("sold_at") if isinstance(sale, dict) else sale.sold_at) or datetime.min
                    if sold_at < (current_cycle["cycle_start"] or datetime.min):
                        sales_idx += 1
                        continue
                    _apply_cycle_sale(current_cycle, sale)
                    sales_idx += 1
                current_cycle["cycle_status"] = "open"
                known_cost = float(current_cycle.get("acquisition_cost_known") or 0.0)
                current_cycle["estimated_margin_vs_known_cost"] = float(current_cycle.get("net_sales") or 0.0) - known_cost
                rows.append(current_cycle)

        output = []
        for row in rows:
            output.append(
                {
                    "product_id": int(row["product_id"]),
                    "sku": row["sku"],
                    "product_title": row["product_title"],
                    "cycle_number": int(row["cycle_number"]),
                    "cycle_id": row["cycle_id"],
                    "cycle_status": row["cycle_status"],
                    "cycle_start": row["cycle_start"].isoformat() if row["cycle_start"] is not None else None,
                    "cycle_end": row["cycle_end"].isoformat() if row["cycle_end"] is not None else None,
                    "start_qty_before": int(row["start_qty_before"]),
                    "end_qty_after": int(row["end_qty_after"]),
                    "qty_in": int(row["qty_in"]),
                    "qty_out_movements": int(row["qty_out_movements"]),
                    "qty_sold_sales": int(row["qty_sold_sales"]),
                    "movement_count": int(row["movement_count"]),
                    "sale_count": int(row["sale_count"]),
                    "acquisition_cost_known": round(float(row["acquisition_cost_known"] or 0), 2),
                    "gross_sales": round(float(row["gross_sales"] or 0), 2),
                    "fees": round(float(row["fees"] or 0), 2),
                    "shipping_cost": round(float(row["shipping_cost"] or 0), 2),
                    "shipping_label_cost": round(float(row.get("shipping_label_cost") or 0), 2),
                    "net_sales": round(float(row["net_sales"] or 0), 2),
                    "estimated_margin_vs_known_cost": round(float(row["estimated_margin_vs_known_cost"] or 0), 2),
                }
            )
        return sorted(output, key=lambda x: (x.get("sku") or "", int(x.get("cycle_number") or 0)))

    def report_ebay_fee_reconciliation_rows(
        self,
        *,
        start_dt: datetime,
        end_dt: datetime,
    ) -> list[dict]:
        ListingProduct = aliased(Product)
        def _safe_float(value: Any) -> float:
            try:
                if value is None:
                    return 0.0
                return float(value)
            except Exception:
                return 0.0

        def _extract_order_fee_breakdown_from_notes(notes: str | None) -> dict:
            raw = str(notes or "").strip()
            if not raw:
                return {}
            marker = "fee_breakdown_json="
            idx = raw.find(marker)
            if idx < 0:
                return {}
            json_raw = raw[idx + len(marker):].strip()
            if "; " in json_raw:
                json_raw = json_raw.split("; ", 1)[0].strip()
            if not json_raw:
                return {}
            try:
                payload = json.loads(json_raw)
            except Exception:
                return {}
            return payload if isinstance(payload, dict) else {}

        def _parse_listing_fee_estimate_payload(listing_marketplace_details: str | None) -> dict:
            raw = str(listing_marketplace_details or "").strip()
            if not raw:
                return {}
            try:
                parsed = json.loads(raw)
            except Exception:
                return {}
            if not isinstance(parsed, dict):
                return {}
            publish_meta = parsed.get("ebay_publish")
            if not isinstance(publish_meta, dict):
                return {}
            fee_estimate = publish_meta.get("fee_estimate")
            return fee_estimate if isinstance(fee_estimate, dict) else {}

        query = (
            select(
                Sale.id.label("sale_id"),
                Sale.sold_at.label("sold_at"),
                Sale.external_order_id.label("external_order_id"),
                Sale.order_id.label("order_id"),
                Sale.listing_id.label("listing_id"),
                Sale.quantity_sold.label("quantity_sold"),
                Sale.sold_price.label("sale_gross"),
                Sale.fees.label("sale_fee_field"),
                func.coalesce(Product.sku, ListingProduct.sku).label("sku"),
                func.coalesce(Product.title, ListingProduct.title).label("product_title"),
                MarketplaceListing.external_listing_id.label("external_listing_id"),
                MarketplaceListing.marketplace_details.label("listing_marketplace_details"),
                Order.notes.label("order_notes"),
            )
            .select_from(Sale)
            .outerjoin(Product, Product.id == Sale.product_id)
            .outerjoin(MarketplaceListing, MarketplaceListing.id == Sale.listing_id)
            .outerjoin(ListingProduct, ListingProduct.id == MarketplaceListing.product_id)
            .outerjoin(Order, Order.id == Sale.order_id)
            .where(
                func.lower(func.coalesce(Sale.marketplace, "")) == "ebay",
                Sale.sold_at.is_not(None),
                Sale.sold_at >= start_dt,
                Sale.sold_at <= end_dt,
            )
            .order_by(Sale.sold_at.desc(), Sale.id.desc())
        )
        rows = self.db.execute(query).all()
        order_ids = [int(getattr(row, "order_id", 0) or 0) for row in rows if int(getattr(row, "order_id", 0) or 0) > 0]
        normalized_fee_by_order: dict[int, float] = {}
        if order_ids:
            grouped_fee_rows = self.db.execute(
                select(
                    OrderFinanceEntry.order_id,
                    func.coalesce(func.sum(func.coalesce(OrderFinanceEntry.amount, 0)), 0).label("total_fee"),
                )
                .where(
                    OrderFinanceEntry.order_id.in_(order_ids),
                    OrderFinanceEntry.entry_kind == "marketplace_fee",
                )
                .group_by(OrderFinanceEntry.order_id)
            ).all()
            for fee_row in grouped_fee_rows:
                oid = int(getattr(fee_row, "order_id", 0) or 0)
                if oid <= 0:
                    continue
                normalized_fee_by_order[oid] = _safe_float(getattr(fee_row, "total_fee", 0))
        result: list[dict] = []
        for row in rows:
            fee_estimate = _parse_listing_fee_estimate_payload(row.listing_marketplace_details)
            est_total_raw = _safe_float(fee_estimate.get("estimated_total_fees"))
            est_basis_qty_raw = int(_safe_float(fee_estimate.get("quantity") or 0))
            sale_qty = max(1, int(row.quantity_sold or 1))
            est_basis_qty = max(1, est_basis_qty_raw) if est_total_raw > 0 else 0
            est_scaled = est_total_raw * (float(sale_qty) / float(est_basis_qty)) if est_total_raw > 0 and est_basis_qty > 0 else 0.0

            order_fee_breakdown = _extract_order_fee_breakdown_from_notes(row.order_notes)
            order_marketplace_fee = _safe_float(order_fee_breakdown.get("total_marketplace_fee"))
            sale_fee_field = _safe_float(row.sale_fee_field)
            normalized_order_fee_total = _safe_float(normalized_fee_by_order.get(int(row.order_id or 0), 0))
            if normalized_order_fee_total > 0:
                actual_fee = normalized_order_fee_total
                actual_fee_source = "normalized_order_finance_entries_marketplace_fee_sum"
            elif order_marketplace_fee > 0:
                actual_fee = order_marketplace_fee
                actual_fee_source = "order_fee_breakdown_total_marketplace_fee"
            else:
                actual_fee = sale_fee_field
                actual_fee_source = "sale_fees_field"

            variance = actual_fee - est_scaled
            variance_pct = (variance / est_scaled * 100.0) if est_scaled > 0 else 0.0
            estimate_final_value_rate_percent = _safe_float(fee_estimate.get("final_value_rate_percent"))
            estimate_final_value_fixed_usd = _safe_float(fee_estimate.get("final_value_fixed_usd"))
            estimate_payment_rate_percent = _safe_float(fee_estimate.get("payment_rate_percent"))
            estimate_payment_fixed_usd = _safe_float(fee_estimate.get("payment_fixed_usd"))
            estimate_promoted_rate_percent = _safe_float(fee_estimate.get("promoted_rate_percent"))
            sale_gross = _safe_float(row.sale_gross)
            implied_final_value_rate = 0.0
            if sale_gross > 0:
                non_fv_component = (
                    (sale_gross * estimate_payment_rate_percent / 100.0)
                    + estimate_payment_fixed_usd
                    + (sale_gross * estimate_promoted_rate_percent / 100.0)
                    + estimate_final_value_fixed_usd
                )
                implied_final_value_rate = ((actual_fee - non_fv_component) / sale_gross) * 100.0

            result.append(
                {
                    "sale_id": int(row.sale_id or 0),
                    "sold_at": row.sold_at.isoformat() if row.sold_at is not None else None,
                    "external_order_id": str(row.external_order_id or "").strip(),
                    "listing_id": int(row.listing_id or 0) or None,
                    "external_listing_id": str(row.external_listing_id or "").strip(),
                    "sku": str(row.sku or "").strip() or None,
                    "product_title": str(row.product_title or "").strip() or None,
                    "quantity_sold": sale_qty,
                    "sale_gross": round(sale_gross, 2),
                    "actual_fee": round(actual_fee, 2),
                    "actual_fee_source": actual_fee_source,
                    "sale_fee_field": round(sale_fee_field, 2),
                    "normalized_order_finance_marketplace_fee_total": round(normalized_order_fee_total, 2),
                    "normalized_order_finance_marketplace_fee_present": bool(normalized_order_fee_total > 0),
                    "order_fee_breakdown_total_marketplace_fee": round(order_marketplace_fee, 2),
                    "order_fee_breakdown_present": bool(order_marketplace_fee > 0),
                    "delta_sale_fee_field_vs_order_breakdown": round(sale_fee_field - order_marketplace_fee, 2)
                    if order_marketplace_fee > 0
                    else 0.0,
                    "estimated_fee_scaled": round(est_scaled, 2),
                    "estimated_fee_source_total": round(est_total_raw, 2),
                    "estimated_fee_source_qty": int(est_basis_qty_raw or 0),
                    "variance_actual_minus_estimate": round(variance, 2),
                    "variance_percent_of_estimate": round(variance_pct, 2),
                    "fee_estimate_present": bool(est_total_raw > 0),
                    "estimate_final_value_rate_percent": round(estimate_final_value_rate_percent, 4),
                    "estimate_final_value_fixed_usd": round(estimate_final_value_fixed_usd, 2),
                    "estimate_payment_rate_percent": round(estimate_payment_rate_percent, 4),
                    "estimate_payment_fixed_usd": round(estimate_payment_fixed_usd, 2),
                    "estimate_promoted_rate_percent": round(estimate_promoted_rate_percent, 4),
                    "implied_final_value_rate_percent": round(implied_final_value_rate, 4),
                }
            )
        return result

    def report_ebay_marketplace_fee_rows(
        self,
        *,
        start_dt: datetime,
        end_dt: datetime,
    ) -> list[dict]:
        query = (
            select(
                OrderFinanceEntry.order_id.label("order_id"),
                Order.sold_at.label("sold_at"),
                Order.external_order_id.label("external_order_id"),
                OrderFinanceEntry.line_item_id.label("line_item_id"),
                OrderFinanceEntry.sku.label("sku"),
                OrderFinanceEntry.legacy_item_id.label("legacy_item_id"),
                OrderFinanceEntry.fee_type.label("fee_type"),
                OrderFinanceEntry.amount.label("fee_amount"),
                OrderFinanceEntry.currency.label("fee_currency"),
                OrderFinanceEntry.memo.label("fee_memo"),
                OrderFinanceEntry.transaction_id.label("transaction_id"),
                OrderFinanceEntry.transaction_date.label("transaction_date"),
                OrderFinanceEntry.transaction_type.label("transaction_type"),
                OrderFinanceEntry.transaction_status.label("transaction_status"),
                OrderFinanceEntry.source.label("source"),
            )
            .select_from(OrderFinanceEntry)
            .join(Order, Order.id == OrderFinanceEntry.order_id)
            .where(
                OrderFinanceEntry.entry_kind == "marketplace_fee",
                func.lower(func.coalesce(cast(Order.marketplace, String), "")) == "ebay",
                Order.sold_at.is_not(None),
                Order.sold_at >= start_dt,
                Order.sold_at <= end_dt,
            )
            .order_by(Order.sold_at.desc(), OrderFinanceEntry.order_id.desc(), OrderFinanceEntry.id.desc())
        )
        rows = self.db.execute(query).all()
        result: list[dict] = []
        for row in rows:
            result.append(
                {
                    "order_id": int(row.order_id or 0),
                    "sold_at": row.sold_at.isoformat() if row.sold_at is not None else "",
                    "external_order_id": str(row.external_order_id or "").strip(),
                    "line_item_id": str(row.line_item_id or "").strip(),
                    "sku": str(row.sku or "").strip(),
                    "product_title": "",
                    "legacy_item_id": str(row.legacy_item_id or "").strip(),
                    "fee_type": str(row.fee_type or "").strip(),
                    "fee_amount": round(float(row.fee_amount or 0), 2),
                    "fee_currency": str(row.fee_currency or "").strip(),
                    "fee_memo": str(row.fee_memo or "").strip(),
                    "transaction_id": str(row.transaction_id or "").strip(),
                    "transaction_date": row.transaction_date.isoformat() if row.transaction_date is not None else "",
                    "transaction_type": str(row.transaction_type or "").strip(),
                    "transaction_status": str(row.transaction_status or "").strip(),
                    "source": str(row.source or "").strip() or "normalized_order_finance_entries",
                }
            )
        return result

    def report_marketplace_reconciliation_rows(
        self,
        *,
        start_dt: datetime,
        end_dt: datetime,
    ) -> list[dict]:
        sales_rows = self.db.execute(
            select(
                func.lower(func.coalesce(cast(Sale.marketplace, String), "")).label("marketplace"),
                func.count(Sale.id).label("sales_count"),
                func.coalesce(func.sum(Sale.sold_price), 0).label("sales_gross"),
                func.coalesce(func.sum(Sale.fees), 0).label("sales_fees"),
                func.coalesce(func.sum(Sale.shipping_cost), 0).label("sales_shipping_cost"),
                func.coalesce(func.sum(Sale.shipping_label_cost), 0).label("sales_shipping_label_cost"),
                func.coalesce(
                    func.sum(
                        Sale.sold_price
                        + Sale.shipping_cost
                        - Sale.fees
                        - func.coalesce(Sale.shipping_label_cost, 0)
                    ),
                    0,
                ).label("sales_net_before_returns"),
            )
            .where(
                Sale.sold_at.is_not(None),
                Sale.sold_at >= start_dt,
                Sale.sold_at <= end_dt,
                func.length(func.trim(func.coalesce(cast(Sale.marketplace, String), ""))) > 0,
            )
            .group_by(func.lower(func.coalesce(cast(Sale.marketplace, String), "")))
        ).all()

        order_rows = self.db.execute(
            select(
                func.lower(func.coalesce(cast(Order.marketplace, String), "")).label("marketplace"),
                func.count(Order.id).label("orders_count"),
                func.coalesce(func.sum(Order.total_amount), 0).label("order_total_sum"),
            )
            .where(
                Order.sold_at.is_not(None),
                Order.sold_at >= start_dt,
                Order.sold_at <= end_dt,
                func.length(func.trim(func.coalesce(cast(Order.marketplace, String), ""))) > 0,
            )
            .group_by(func.lower(func.coalesce(cast(Order.marketplace, String), "")))
        ).all()

        return_rows = self.db.execute(
            select(
                func.lower(func.coalesce(cast(ReturnRecord.marketplace, String), "")).label("marketplace"),
                func.count(ReturnRecord.id).label("returns_count"),
                func.coalesce(
                    func.sum(
                        func.coalesce(ReturnRecord.refund_amount, 0)
                        + func.coalesce(ReturnRecord.refund_fees, 0)
                        + func.coalesce(ReturnRecord.refund_shipping, 0)
                    ),
                    0,
                ).label("returns_refund_total"),
            )
            .where(
                ReturnRecord.returned_at.is_not(None),
                ReturnRecord.returned_at >= start_dt,
                ReturnRecord.returned_at <= end_dt,
                func.length(func.trim(func.coalesce(cast(ReturnRecord.marketplace, String), ""))) > 0,
            )
            .group_by(func.lower(func.coalesce(cast(ReturnRecord.marketplace, String), "")))
        ).all()

        by_marketplace: dict[str, dict[str, float | int | str | bool]] = {}

        def _bucket(marketplace: str) -> dict[str, float | int | str | bool]:
            key = str(marketplace or "").strip().lower()
            if key not in by_marketplace:
                by_marketplace[key] = {
                    "marketplace": key,
                    "sales_count": 0,
                    "orders_count": 0,
                    "returns_count": 0,
                    "sales_gross": 0.0,
                    "sales_fees": 0.0,
                    "sales_shipping_cost": 0.0,
                    "sales_shipping_label_cost": 0.0,
                    "sales_net_before_returns": 0.0,
                    "returns_refund_total": 0.0,
                    "net_after_returns": 0.0,
                    "order_total_sum": 0.0,
                    "delta_order_total_vs_sales_gross": 0.0,
                    "reconcile_flag": False,
                }
            return by_marketplace[key]

        actual_sales_rows = self.report_sales_actual_econ_rows(start_dt=start_dt, end_dt=end_dt)
        if actual_sales_rows:
            for row in actual_sales_rows:
                bucket = _bucket(str(row.get("marketplace") or ""))
                bucket["sales_count"] = int(bucket.get("sales_count") or 0) + 1
                bucket["sales_gross"] = float(bucket.get("sales_gross") or 0.0) + self._safe_float(row.get("sold_price"))
                bucket["sales_fees"] = float(bucket.get("sales_fees") or 0.0) + self._safe_float(
                    row.get("allocated_fee_actual")
                )
                bucket["sales_shipping_cost"] = float(bucket.get("sales_shipping_cost") or 0.0) + self._safe_float(
                    row.get("allocated_shipping_charged")
                )
                bucket["sales_shipping_label_cost"] = float(
                    bucket.get("sales_shipping_label_cost") or 0.0
                ) + self._safe_float(row.get("allocated_shipping_actual"))
                bucket["sales_net_before_returns"] = float(
                    bucket.get("sales_net_before_returns") or 0.0
                ) + self._safe_float(row.get("net_before_cogs_actual"))
        else:
            for row in sales_rows:
                bucket = _bucket(str(row.marketplace or ""))
                bucket["sales_count"] = int(row.sales_count or 0)
                bucket["sales_gross"] = float(row.sales_gross or 0.0)
                bucket["sales_fees"] = float(row.sales_fees or 0.0)
                bucket["sales_shipping_cost"] = float(row.sales_shipping_cost or 0.0)
                bucket["sales_shipping_label_cost"] = float(row.sales_shipping_label_cost or 0.0)
                bucket["sales_net_before_returns"] = float(row.sales_net_before_returns or 0.0)

        for row in order_rows:
            bucket = _bucket(str(row.marketplace or ""))
            bucket["orders_count"] = int(row.orders_count or 0)
            bucket["order_total_sum"] = float(row.order_total_sum or 0.0)

        for row in return_rows:
            bucket = _bucket(str(row.marketplace or ""))
            bucket["returns_count"] = int(row.returns_count or 0)
            bucket["returns_refund_total"] = float(row.returns_refund_total or 0.0)

        result: list[dict] = []
        for marketplace in sorted(by_marketplace.keys()):
            bucket = by_marketplace[marketplace]
            sales_gross = float(bucket.get("sales_gross") or 0.0)
            order_total_sum = float(bucket.get("order_total_sum") or 0.0)
            sales_net_before_returns = float(bucket.get("sales_net_before_returns") or 0.0)
            returns_refund_total = float(bucket.get("returns_refund_total") or 0.0)
            delta = order_total_sum - sales_gross
            net_after_returns = sales_net_before_returns - returns_refund_total
            result.append(
                {
                    "marketplace": str(bucket.get("marketplace") or ""),
                    "sales_count": int(bucket.get("sales_count") or 0),
                    "orders_count": int(bucket.get("orders_count") or 0),
                    "returns_count": int(bucket.get("returns_count") or 0),
                    "sales_gross": round(sales_gross, 2),
                    "sales_fees": round(float(bucket.get("sales_fees") or 0.0), 2),
                    "sales_shipping_cost": round(float(bucket.get("sales_shipping_cost") or 0.0), 2),
                    "sales_shipping_label_cost": round(float(bucket.get("sales_shipping_label_cost") or 0.0), 2),
                    "sales_net_before_returns": round(sales_net_before_returns, 2),
                    "returns_refund_total": round(returns_refund_total, 2),
                    "net_after_returns": round(net_after_returns, 2),
                    "order_total_sum": round(order_total_sum, 2),
                    "delta_order_total_vs_sales_gross": round(delta, 2),
                    "reconcile_flag": bool(abs(delta) > 0.01),
                }
            )
        return result

    def collect_rollup_latency_baseline(
        self,
        *,
        start_dt: datetime,
        end_dt: datetime,
        tax_rate_percent: float = 7.5,
        shipping_taxable: bool = True,
        tax_exempt_categories: set[str] | None = None,
        marketplaces: set[str] | None = None,
    ) -> list[dict]:
        baseline_rows: list[dict] = []

        def _run(name: str, fn) -> None:
            started = perf_counter()
            value = fn()
            elapsed_ms = (perf_counter() - started) * 1000.0
            if isinstance(value, dict):
                row_count = len(value.keys())
            elif isinstance(value, list):
                row_count = len(value)
            else:
                try:
                    row_count = len(value)  # type: ignore[arg-type]
                except Exception:
                    row_count = 1 if value is not None else 0
            baseline_rows.append(
                {
                    "rollup_name": name,
                    "elapsed_ms": round(float(elapsed_ms), 3),
                    "result_count": int(row_count),
                    "window_start": start_dt.isoformat(),
                    "window_end": end_dt.isoformat(),
                }
            )

        _run("dashboard_live_metrics", lambda: self.dashboard_live_metrics(now=end_dt))
        _run(
            "report_shipping_economics_rows",
            lambda: self.report_shipping_economics_rows(
                start_dt=start_dt,
                end_dt=end_dt,
                marketplaces=marketplaces,
            ),
        )
        _run(
            "report_shipping_economics_summary",
            lambda: self.report_shipping_economics_summary(
                start_dt=start_dt,
                end_dt=end_dt,
                marketplaces=marketplaces,
            ),
        )
        _run(
            "report_tax_estimate_detail_rows",
            lambda: self.report_tax_estimate_detail_rows(
                start_dt=start_dt,
                end_dt=end_dt,
                tax_rate_percent=float(tax_rate_percent or 0.0),
                shipping_taxable=bool(shipping_taxable),
                tax_exempt_categories=set(tax_exempt_categories or set()),
                marketplaces=marketplaces,
            ),
        )
        _run(
            "report_ebay_fee_reconciliation_rows",
            lambda: self.report_ebay_fee_reconciliation_rows(
                start_dt=start_dt,
                end_dt=end_dt,
            ),
        )

        return baseline_rows

    def collect_rollup_explain_baseline(
        self,
        *,
        start_dt: datetime,
        end_dt: datetime,
        sample_limit: int = 2000,
    ) -> list[dict]:
        limit = max(100, min(10000, int(sample_limit or 2000)))
        window_7d = end_dt - timedelta(days=7)
        window_30d = end_dt - timedelta(days=30)

        explain_rows: list[dict] = []
        inspector = inspect(self.db.get_bind())

        def _table_exists(table_name: str) -> bool:
            try:
                return bool(inspector.has_table(table_name))
            except Exception:
                return False

        def _parse_plan_ms(plan_lines: list[str], key: str) -> float:
            prefix = f"{key}:"
            for line in plan_lines:
                raw = str(line or "").strip()
                if not raw.startswith(prefix):
                    continue
                value_raw = raw[len(prefix) :].strip()
                if value_raw.lower().endswith("ms"):
                    value_raw = value_raw[:-2].strip()
                try:
                    return round(float(value_raw), 3)
                except Exception:
                    return 0.0
            return 0.0

        def _run(name: str, sql: str, params: dict[str, Any]) -> None:
            explain_sql = f"EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) {sql}"
            started = perf_counter()
            try:
                rows = self.db.execute(text(explain_sql), params).all()
                elapsed_ms = (perf_counter() - started) * 1000.0
                plan_lines = [str((row[0] if isinstance(row, (list, tuple)) else row) or "") for row in rows]
                planning_ms = _parse_plan_ms(plan_lines, "Planning Time")
                execution_ms = _parse_plan_ms(plan_lines, "Execution Time")
                explain_rows.append(
                    {
                        "rollup_name": name,
                        "elapsed_ms": round(float(elapsed_ms), 3),
                        "planning_ms": float(planning_ms),
                        "execution_ms": float(execution_ms),
                        "plan_lines": int(len(plan_lines)),
                        "window_start": start_dt.isoformat(),
                        "window_end": end_dt.isoformat(),
                        "sample_limit": int(limit),
                        "plan_text": "\n".join(plan_lines),
                    }
                )
            except Exception as exc:
                try:
                    self.db.rollback()
                except Exception:
                    pass
                elapsed_ms = (perf_counter() - started) * 1000.0
                error_text = str(exc)
                missing_relation = re.search(r'relation "([^"]+)" does not exist', error_text, flags=re.IGNORECASE)
                if missing_relation:
                    explain_rows.append(
                        {
                            "rollup_name": name,
                            "elapsed_ms": round(float(elapsed_ms), 3),
                            "planning_ms": 0.0,
                            "execution_ms": 0.0,
                            "plan_lines": 0,
                            "window_start": start_dt.isoformat(),
                            "window_end": end_dt.isoformat(),
                            "sample_limit": int(limit),
                            "plan_text": "",
                            "skipped": True,
                            "skip_reason": f'table {missing_relation.group(1)} not present',
                        }
                    )
                    return
                explain_rows.append(
                    {
                        "rollup_name": name,
                        "elapsed_ms": round(float(elapsed_ms), 3),
                        "planning_ms": 0.0,
                        "execution_ms": 0.0,
                        "plan_lines": 0,
                        "window_start": start_dt.isoformat(),
                        "window_end": end_dt.isoformat(),
                        "sample_limit": int(limit),
                        "plan_text": "",
                        "error": error_text,
                    }
                )

        _run(
            "dashboard_live_metrics",
            """
            WITH sales_30d AS (
                SELECT
                    s.id,
                    s.sold_at,
                    s.sold_price,
                    s.fees,
                    s.shipping_cost,
                    s.shipping_label_cost,
                    s.order_id,
                    CASE
                        WHEN s.order_id IS NULL THEN 1
                        ELSE COUNT(s.id) OVER (PARTITION BY s.order_id)
                    END AS order_sale_count
                FROM sales s
                WHERE s.sold_at >= :window_30d
                  AND s.sold_at <= :end_dt
            ),
            linked_fee_rollup AS (
                SELECT order_id, COALESCE(SUM(COALESCE(amount, 0)), 0) AS fee_total
                FROM order_finance_entries
                WHERE entry_kind = 'marketplace_fee'
                  AND order_id IN (SELECT DISTINCT order_id FROM sales_30d WHERE order_id IS NOT NULL)
                GROUP BY order_id
            ),
            linked_label_rollup AS (
                SELECT order_id, COALESCE(SUM(COALESCE(amount, 0)), 0) AS label_total
                FROM order_finance_entries
                WHERE entry_kind = 'shipping_label'
                  AND order_id IN (SELECT DISTINCT order_id FROM sales_30d WHERE order_id IS NOT NULL)
                GROUP BY order_id
            ),
            sales_rollup AS (
                SELECT
                    COALESCE(SUM(CASE WHEN s.sold_at >= :window_7d AND s.sold_at <= :end_dt THEN 1 ELSE 0 END), 0) AS sales_7d_count,
                    COALESCE(SUM(CASE WHEN s.sold_at >= :window_7d AND s.sold_at <= :end_dt THEN COALESCE(s.sold_price, 0) ELSE 0 END), 0) AS sales_7d_gross,
                    COUNT(*) AS sales_30d_count,
                    COALESCE(SUM(COALESCE(s.sold_price, 0)), 0) AS sales_30d_gross,
                    COALESCE(SUM(COALESCE(s.shipping_cost, 0)), 0) AS sales_30d_shipping_charged,
                    COALESCE(SUM(COALESCE(f.fee_total / NULLIF(s.order_sale_count, 0), s.fees, 0)), 0) AS sales_30d_fee_actual,
                    COALESCE(SUM(COALESCE(l.label_total / NULLIF(s.order_sale_count, 0), s.shipping_label_cost, 0)), 0) AS sales_30d_label_spend
                FROM sales_30d s
                LEFT JOIN linked_fee_rollup f ON f.order_id = s.order_id
                LEFT JOIN linked_label_rollup l ON l.order_id = s.order_id
            ),
            orders_rollup AS (
                SELECT COUNT(*) AS orders_30d_count
                FROM orders
                WHERE sold_at >= :window_30d
                  AND sold_at <= :end_dt
            )
            SELECT
                sales_rollup.sales_7d_count,
                sales_rollup.sales_7d_gross,
                sales_rollup.sales_30d_count,
                sales_rollup.sales_30d_gross,
                sales_rollup.sales_30d_shipping_charged,
                sales_rollup.sales_30d_fee_actual,
                sales_rollup.sales_30d_label_spend,
                orders_rollup.orders_30d_count,
                sales_rollup.sales_30d_gross + sales_rollup.sales_30d_shipping_charged - sales_rollup.sales_30d_fee_actual - sales_rollup.sales_30d_label_spend AS sales_30d_net
            FROM sales_rollup
            CROSS JOIN orders_rollup
            """,
            {
                "window_7d": window_7d,
                "window_30d": window_30d,
                "end_dt": end_dt,
            },
        )

        _run(
            "report_shipping_economics_rows",
            """
            SELECT
                s.id,
                s.sold_at,
                LOWER(COALESCE(s.marketplace, '')) AS marketplace,
                s.external_order_id,
                s.order_id,
                COALESCE(p.sku, lp.sku) AS sku,
                COALESCE(p.title, lp.title) AS product_title,
                s.quantity_sold,
                COALESCE(s.shipping_cost, 0) AS shipping_charged_to_buyer,
                COALESCE(s.shipping_label_cost, 0) AS shipping_label_spend
            FROM sales s
            LEFT JOIN products p ON p.id = s.product_id
            LEFT JOIN marketplace_listings l ON l.id = s.listing_id
            LEFT JOIN products lp ON lp.id = l.product_id
            WHERE s.sold_at IS NOT NULL
              AND s.sold_at >= :start_dt
              AND s.sold_at <= :end_dt
            ORDER BY s.sold_at DESC, s.id DESC
            LIMIT :sample_limit
            """,
            {
                "start_dt": start_dt,
                "end_dt": end_dt,
                "sample_limit": limit,
            },
        )

        _run(
            "report_shipping_economics_summary",
            """
            SELECT
                LOWER(COALESCE(s.marketplace, '')) AS marketplace,
                COUNT(*) AS sales_count,
                COALESCE(SUM(COALESCE(s.shipping_cost, 0)), 0) AS total_shipping_charged,
                COALESCE(SUM(COALESCE(s.shipping_label_cost, 0)), 0) AS total_label_spend
            FROM sales s
            WHERE s.sold_at IS NOT NULL
              AND s.sold_at >= :start_dt
              AND s.sold_at <= :end_dt
            GROUP BY LOWER(COALESCE(s.marketplace, ''))
            ORDER BY sales_count DESC, marketplace
            """,
            {
                "start_dt": start_dt,
                "end_dt": end_dt,
            },
        )

        _run(
            "report_tax_estimate_detail_rows",
            """
            SELECT
                s.id,
                s.sold_at,
                LOWER(COALESCE(s.marketplace, '')) AS marketplace,
                COALESCE(s.sold_price, 0) AS sold_price,
                COALESCE(s.shipping_cost, 0) AS shipping_cost,
                LOWER(COALESCE(p.category, lp.category, '')) AS product_category
            FROM sales s
            LEFT JOIN products p ON p.id = s.product_id
            LEFT JOIN marketplace_listings l ON l.id = s.listing_id
            LEFT JOIN products lp ON lp.id = l.product_id
            WHERE s.sold_at IS NOT NULL
              AND s.sold_at >= :start_dt
              AND s.sold_at <= :end_dt
            ORDER BY s.sold_at DESC, s.id DESC
            LIMIT :sample_limit
            """,
            {
                "start_dt": start_dt,
                "end_dt": end_dt,
                "sample_limit": limit,
            },
        )

        _run(
            "report_ebay_fee_reconciliation_rows",
            """
            SELECT
                s.id,
                s.sold_at,
                s.external_order_id,
                LOWER(COALESCE(s.marketplace, '')) AS marketplace,
                COALESCE(s.fees, 0) AS sale_fee_field,
                COALESCE(
                    SUM(
                        CASE
                            WHEN LOWER(COALESCE(ofe.entry_kind, '')) = 'marketplace_fee'
                            THEN COALESCE(ofe.amount, 0)
                            ELSE 0
                        END
                    ),
                    0
                ) AS order_marketplace_fee
            FROM sales s
            LEFT JOIN order_finance_entries ofe ON ofe.order_id = s.order_id
            WHERE s.sold_at IS NOT NULL
              AND s.sold_at >= :start_dt
              AND s.sold_at <= :end_dt
            GROUP BY
                s.id,
                s.sold_at,
                s.external_order_id,
                LOWER(COALESCE(s.marketplace, '')),
                COALESCE(s.fees, 0)
            ORDER BY s.sold_at DESC, s.id DESC
            LIMIT :sample_limit
            """,
            {
                "start_dt": start_dt,
                "end_dt": end_dt,
                "sample_limit": limit,
            },
        )

        _run(
            "dashboard_ebay_fee_type_breakdown_30d",
            """
            SELECT
                UPPER(COALESCE(ofe.fee_type, 'UNKNOWN')) AS fee_type,
                COALESCE(SUM(COALESCE(ofe.amount, 0)), 0) AS total_fee_amount
            FROM order_finance_entries ofe
            WHERE ofe.entry_kind = 'marketplace_fee'
              AND (
                ofe.transaction_date >= :window_30d
                OR (ofe.transaction_date IS NULL AND ofe.created_at >= :window_30d)
              )
            GROUP BY UPPER(COALESCE(ofe.fee_type, 'UNKNOWN'))
            ORDER BY total_fee_amount DESC, fee_type
            """,
            {
                "window_30d": window_30d,
            },
        )

        if _table_exists("audit_logs"):
            _run(
                "notification_outbox_runner_activity_14d",
                """
                SELECT
                    created_at,
                    actor,
                    COALESCE(CAST(changes_json AS JSONB) -> 'after' ->> 'action', '') AS action,
                    COALESCE(CAST(changes_json AS JSONB) -> 'after' ->> 'status', '') AS status
                FROM audit_logs
                WHERE entity_type = 'integration_event'
                  AND created_at >= :window_14d
                  AND COALESCE(CAST(changes_json AS JSONB) -> 'after' ->> 'integration', '') = 'notification_outbox'
                  AND COALESCE(CAST(changes_json AS JSONB) -> 'after' ->> 'environment', '') = :app_env
                  AND COALESCE(CAST(changes_json AS JSONB) -> 'after' ->> 'action', '') IN ('process_due', 'cleanup', 'manual_process_due', 'manual_cleanup')
                ORDER BY created_at DESC
                LIMIT :sample_limit
                """,
                {
                    "window_14d": end_dt - timedelta(days=14),
                    "app_env": str(getattr(settings, "app_env", "") or ""),
                    "sample_limit": limit,
                },
            )
        else:
            explain_rows.append(
                {
                    "rollup_name": "notification_outbox_runner_activity_14d",
                    "elapsed_ms": 0.0,
                    "planning_ms": 0.0,
                    "execution_ms": 0.0,
                    "plan_lines": 0,
                    "window_start": start_dt.isoformat(),
                    "window_end": end_dt.isoformat(),
                    "sample_limit": int(limit),
                    "plan_text": "",
                    "skipped": True,
                    "skip_reason": "table audit_logs not present",
                }
            )

        if _table_exists("integration_queue_jobs"):
            _run(
                "slack_ops_queue_health_rows",
                """
                SELECT
                    id,
                    status,
                    requested_by,
                    next_attempt_at,
                    created_at,
                    updated_at
                FROM integration_queue_jobs
                WHERE environment = :app_env
                  AND integration = 'slack_ops'
                  AND status IN ('queued', 'running', 'blocked', 'failed', 'success')
                ORDER BY created_at DESC
                LIMIT :sample_limit
                """,
                {
                    "app_env": str(getattr(settings, "app_env", "") or ""),
                    "sample_limit": limit,
                },
            )
        else:
            explain_rows.append(
                {
                    "rollup_name": "slack_ops_queue_health_rows",
                    "elapsed_ms": 0.0,
                    "planning_ms": 0.0,
                    "execution_ms": 0.0,
                    "plan_lines": 0,
                    "window_start": start_dt.isoformat(),
                    "window_end": end_dt.isoformat(),
                    "sample_limit": int(limit),
                    "plan_text": "",
                    "skipped": True,
                    "skip_reason": "table integration_queue_jobs not present",
                }
            )

        if _table_exists("audit_logs"):
            _run(
                "slack_ops_events_24h",
                """
                SELECT
                    created_at,
                    actor,
                    COALESCE(CAST(changes_json AS JSONB) -> 'after' ->> 'integration', '') AS integration,
                    COALESCE(CAST(changes_json AS JSONB) -> 'after' ->> 'action', '') AS action,
                    COALESCE(CAST(changes_json AS JSONB) -> 'after' ->> 'status', '') AS status
                FROM audit_logs
                WHERE entity_type = 'integration_event'
                  AND created_at >= :window_24h
                  AND COALESCE(CAST(changes_json AS JSONB) -> 'after' ->> 'environment', '') = :app_env
                  AND COALESCE(CAST(changes_json AS JSONB) -> 'after' ->> 'integration', '') = 'slack_ops'
                ORDER BY created_at DESC
                LIMIT :sample_limit
                """,
                {
                    "window_24h": end_dt - timedelta(hours=24),
                    "app_env": str(getattr(settings, "app_env", "") or ""),
                    "sample_limit": limit,
                },
            )
        else:
            explain_rows.append(
                {
                    "rollup_name": "slack_ops_events_24h",
                    "elapsed_ms": 0.0,
                    "planning_ms": 0.0,
                    "execution_ms": 0.0,
                    "plan_lines": 0,
                    "window_start": start_dt.isoformat(),
                    "window_end": end_dt.isoformat(),
                    "sample_limit": int(limit),
                    "plan_text": "",
                    "skipped": True,
                    "skip_reason": "table audit_logs not present",
                }
            )

        _run(
            "report_orders_rows",
            """
            SELECT
                o.id,
                o.sold_at,
                LOWER(COALESCE(o.marketplace, '')) AS marketplace,
                o.external_order_id,
                LOWER(COALESCE(o.order_status, '')) AS order_status,
                COALESCE(o.subtotal_amount, 0) AS subtotal_amount,
                COALESCE(o.fees, 0) AS fees,
                COALESCE(o.shipping_cost, 0) AS shipping_cost,
                COALESCE(o.total_amount, 0) AS total_amount
            FROM orders o
            WHERE o.sold_at >= :start_dt
              AND o.sold_at <= :end_dt
            ORDER BY o.sold_at DESC, o.id DESC
            LIMIT :sample_limit
            """,
            {
                "start_dt": start_dt,
                "end_dt": end_dt,
                "sample_limit": limit,
            },
        )

        _run(
            "report_order_items_rows",
            """
            SELECT
                oi.id,
                oi.order_id,
                o.sold_at,
                LOWER(COALESCE(o.marketplace, '')) AS marketplace,
                o.external_order_id,
                oi.product_id,
                oi.listing_id,
                oi.quantity,
                COALESCE(oi.unit_price, 0) AS unit_price,
                COALESCE(oi.line_fees, 0) AS line_fees,
                COALESCE(oi.line_shipping, 0) AS line_shipping,
                COALESCE(oi.line_total, 0) AS line_total
            FROM order_items oi
            JOIN orders o ON o.id = oi.order_id
            WHERE o.sold_at >= :start_dt
              AND o.sold_at <= :end_dt
            ORDER BY o.sold_at DESC, oi.id DESC
            LIMIT :sample_limit
            """,
            {
                "start_dt": start_dt,
                "end_dt": end_dt,
                "sample_limit": limit,
            },
        )

        _run(
            "report_sales_rows",
            """
            SELECT
                s.id,
                s.order_id,
                s.product_id,
                s.listing_id,
                s.sold_at,
                LOWER(COALESCE(s.marketplace, '')) AS marketplace,
                s.external_order_id,
                s.quantity_sold,
                COALESCE(s.sold_price, 0) AS sold_price,
                COALESCE(s.fees, 0) AS fees,
                COALESCE(s.shipping_cost, 0) AS shipping_cost,
                COALESCE(s.shipping_label_cost, 0) AS shipping_label_cost
            FROM sales s
            WHERE s.sold_at >= :start_dt
              AND s.sold_at <= :end_dt
            ORDER BY s.sold_at DESC, s.id DESC
            LIMIT :sample_limit
            """,
            {
                "start_dt": start_dt,
                "end_dt": end_dt,
                "sample_limit": limit,
            },
        )

        _run(
            "report_returns_rows",
            """
            SELECT
                r.id,
                r.returned_at,
                LOWER(COALESCE(r.marketplace, '')) AS marketplace,
                r.external_return_id,
                LOWER(COALESCE(r.return_status, '')) AS return_status,
                COALESCE(r.refund_amount, 0) AS refund_amount,
                COALESCE(r.refund_fees, 0) AS refund_fees,
                COALESCE(r.refund_shipping, 0) AS refund_shipping
            FROM returns r
            WHERE r.returned_at >= :start_dt
              AND r.returned_at <= :end_dt
            ORDER BY r.returned_at DESC, r.id DESC
            LIMIT :sample_limit
            """,
            {
                "start_dt": start_dt,
                "end_dt": end_dt,
                "sample_limit": limit,
            },
        )

        _run(
            "report_listings_rows",
            """
            SELECT
                l.id,
                l.product_id,
                l.created_at,
                l.updated_at,
                l.listed_at,
                LOWER(COALESCE(l.marketplace, '')) AS marketplace,
                LOWER(COALESCE(CAST(l.listing_status AS TEXT), '')) AS listing_status,
                l.external_listing_id
            FROM marketplace_listings l
            WHERE (l.listed_at >= :start_dt AND l.listed_at <= :end_dt)
               OR (l.created_at >= :start_dt AND l.created_at <= :end_dt)
            ORDER BY l.listed_at DESC NULLS LAST, l.id DESC
            LIMIT :sample_limit
            """,
            {
                "start_dt": start_dt,
                "end_dt": end_dt,
                "sample_limit": limit,
            },
        )

        _run(
            "report_products_rows",
            """
            SELECT
                p.id,
                p.sku,
                p.title,
                p.acquired_at,
                p.created_at,
                LOWER(COALESCE(p.category, '')) AS category,
                LOWER(COALESCE(p.metal_type, '')) AS metal_type,
                COALESCE(p.current_quantity, 0) AS current_quantity,
                COALESCE(p.acquisition_cost, 0) AS acquisition_cost,
                COALESCE(p.product_cost, 0) AS product_cost
            FROM products p
            WHERE (p.acquired_at >= :start_dt AND p.acquired_at <= :end_dt)
               OR (p.acquired_at IS NULL AND p.created_at >= :start_dt AND p.created_at <= :end_dt)
            ORDER BY p.acquired_at DESC NULLS LAST, p.id DESC
            LIMIT :sample_limit
            """,
            {
                "start_dt": start_dt,
                "end_dt": end_dt,
                "sample_limit": limit,
            },
        )

        _run(
            "report_lot_assignment_rows",
            """
            SELECT
                pla.id,
                pla.acquired_at,
                pla.created_at,
                COALESCE(pla.quantity_acquired, 0) AS quantity_acquired,
                COALESCE(pla.unit_cost, 0) AS unit_cost,
                COALESCE(pla.allocated_cost, 0) AS allocated_cost,
                COALESCE(pla.allocation_weight, 0) AS allocation_weight,
                pl.id AS lot_id,
                pl.lot_code,
                p.id AS product_id,
                p.sku
            FROM product_lot_assignments pla
            LEFT JOIN purchase_lots pl ON pl.id = pla.lot_id
            LEFT JOIN products p ON p.id = pla.product_id
            WHERE (pla.acquired_at >= :start_dt AND pla.acquired_at <= :end_dt)
               OR (pla.acquired_at IS NULL AND pla.created_at >= :start_dt AND pla.created_at <= :end_dt)
            ORDER BY pla.acquired_at DESC NULLS LAST, pla.id DESC
            LIMIT :sample_limit
            """,
            {
                "start_dt": start_dt,
                "end_dt": end_dt,
                "sample_limit": limit,
            },
        )

        _run(
            "report_inventory_movement_rows",
            """
            SELECT
                im.id,
                im.occurred_at,
                im.product_id,
                p.sku,
                p.title,
                LOWER(COALESCE(im.movement_type, '')) AS movement_type,
                COALESCE(im.quantity_delta, 0) AS quantity_delta,
                COALESCE(im.quantity_before, 0) AS quantity_before,
                COALESCE(im.quantity_after, 0) AS quantity_after,
                COALESCE(im.unit_cost, 0) AS unit_cost,
                LOWER(COALESCE(im.reference_type, '')) AS reference_type,
                im.reference_id
            FROM inventory_movements im
            LEFT JOIN products p ON p.id = im.product_id
            WHERE im.occurred_at >= :start_dt
              AND im.occurred_at <= :end_dt
            ORDER BY im.occurred_at DESC, im.id DESC
            LIMIT :sample_limit
            """,
            {
                "start_dt": start_dt,
                "end_dt": end_dt,
                "sample_limit": limit,
            },
        )

        _run(
            "report_marketplace_reconciliation_rows",
            """
            WITH sales_rows AS (
                SELECT
                    LOWER(COALESCE(s.marketplace, '')) AS marketplace,
                    COUNT(s.id) AS sales_count,
                    COALESCE(SUM(COALESCE(s.sold_price, 0)), 0) AS gross_sales_amount,
                    COALESCE(SUM(COALESCE(s.fees, 0)), 0) AS sale_fees_amount,
                    COALESCE(SUM(COALESCE(s.shipping_cost, 0)), 0) AS shipping_charged_amount
                FROM sales s
                WHERE s.sold_at >= :start_dt
                  AND s.sold_at <= :end_dt
                GROUP BY LOWER(COALESCE(s.marketplace, ''))
            ),
            order_rows AS (
                SELECT
                    LOWER(COALESCE(o.marketplace, '')) AS marketplace,
                    COUNT(o.id) AS orders_count,
                    COALESCE(SUM(COALESCE(o.total_amount, 0)), 0) AS order_total_amount
                FROM orders o
                WHERE o.sold_at >= :start_dt
                  AND o.sold_at <= :end_dt
                GROUP BY LOWER(COALESCE(o.marketplace, ''))
            ),
            return_rows AS (
                SELECT
                    LOWER(COALESCE(r.marketplace, '')) AS marketplace,
                    COUNT(r.id) AS returns_count,
                    COALESCE(
                        SUM(
                            COALESCE(r.refund_amount, 0)
                            + COALESCE(r.refund_fees, 0)
                            + COALESCE(r.refund_shipping, 0)
                        ),
                        0
                    ) AS refunds_amount
                FROM returns r
                WHERE r.returned_at >= :start_dt
                  AND r.returned_at <= :end_dt
                GROUP BY LOWER(COALESCE(r.marketplace, ''))
            ),
            marketplaces AS (
                SELECT marketplace FROM sales_rows
                UNION
                SELECT marketplace FROM order_rows
                UNION
                SELECT marketplace FROM return_rows
            )
            SELECT
                m.marketplace,
                COALESCE(s.sales_count, 0) AS sales_count,
                COALESCE(s.gross_sales_amount, 0) AS gross_sales_amount,
                COALESCE(s.sale_fees_amount, 0) AS sale_fees_amount,
                COALESCE(s.shipping_charged_amount, 0) AS shipping_charged_amount,
                COALESCE(o.orders_count, 0) AS orders_count,
                COALESCE(o.order_total_amount, 0) AS order_total_amount,
                COALESCE(r.returns_count, 0) AS returns_count,
                COALESCE(r.refunds_amount, 0) AS refunds_amount
            FROM marketplaces m
            LEFT JOIN sales_rows s ON s.marketplace = m.marketplace
            LEFT JOIN order_rows o ON o.marketplace = m.marketplace
            LEFT JOIN return_rows r ON r.marketplace = m.marketplace
            ORDER BY sales_count DESC, m.marketplace
            """,
            {
                "start_dt": start_dt,
                "end_dt": end_dt,
            },
        )

        return explain_rows

    def collect_page_latency_baseline(
        self,
        *,
        start_dt: datetime,
        end_dt: datetime,
        tax_rate_percent: float = 7.5,
        shipping_taxable: bool = True,
        tax_exempt_categories: set[str] | None = None,
        marketplaces: set[str] | None = None,
        include_heavy_list_reads: bool = False,
        include_integrations_reads: bool = False,
    ) -> list[dict]:
        baseline_rows: list[dict] = []

        def _run(name: str, fn) -> None:
            started = perf_counter()
            value = fn()
            elapsed_ms = (perf_counter() - started) * 1000.0
            if isinstance(value, dict):
                row_count = len(value.keys())
            elif isinstance(value, list):
                row_count = len(value)
            else:
                try:
                    row_count = len(value)  # type: ignore[arg-type]
                except Exception:
                    row_count = 1 if value is not None else 0
            baseline_rows.append(
                {
                    "probe_name": name,
                    "elapsed_ms": round(float(elapsed_ms), 3),
                    "result_count": int(row_count),
                    "window_start": start_dt.isoformat(),
                    "window_end": end_dt.isoformat(),
                    "include_heavy_list_reads": bool(include_heavy_list_reads),
                    "include_integrations_reads": bool(include_integrations_reads),
                }
            )

        _run("dashboard_metrics", lambda: self.dashboard_metrics())
        _run("dashboard_live_metrics", lambda: self.dashboard_live_metrics(now=end_dt))
        _run(
            "report_shipping_economics_summary",
            lambda: self.report_shipping_economics_summary(
                start_dt=start_dt,
                end_dt=end_dt,
                marketplaces=marketplaces,
            ),
        )
        _run(
            "report_shipping_economics_rows",
            lambda: self.report_shipping_economics_rows(
                start_dt=start_dt,
                end_dt=end_dt,
                marketplaces=marketplaces,
            ),
        )
        _run(
            "report_tax_estimate_detail_rows",
            lambda: self.report_tax_estimate_detail_rows(
                start_dt=start_dt,
                end_dt=end_dt,
                tax_rate_percent=float(tax_rate_percent or 0.0),
                shipping_taxable=bool(shipping_taxable),
                tax_exempt_categories=set(tax_exempt_categories or set()),
                marketplaces=marketplaces,
            ),
        )

        if bool(include_heavy_list_reads):
            _run(
                "report_ebay_fee_reconciliation_rows_extended",
                lambda: self.report_ebay_fee_reconciliation_rows(
                    start_dt=start_dt,
                    end_dt=end_dt,
                ),
            )
            _run("list_products", lambda: self.list_products())
            _run("list_listings", lambda: self.list_listings())
            _run("list_orders", lambda: self.list_orders())

        if bool(include_integrations_reads):
            _run(
                "list_integration_queue_jobs_slack",
                lambda: self.list_integration_queue_jobs(
                    environment=settings.app_env,
                    integration="slack",
                    statuses={"queued", "running", "failed", "success"},
                    limit=500,
                ),
            )
            _run(
                "list_integration_queue_jobs_google",
                lambda: self.list_integration_queue_jobs(
                    environment=settings.app_env,
                    integration="google",
                    statuses={"queued", "running", "failed", "success"},
                    limit=500,
                ),
            )
            _run(
                "integration_event_rows_shared_14d",
                lambda: self.db.scalars(
                    select(AuditLog)
                    .where(
                        AuditLog.entity_type == "integration_event",
                        AuditLog.created_at >= (end_dt - timedelta(days=14)),
                    )
                    .order_by(AuditLog.created_at.desc())
                    .limit(500)
                ).all(),
            )
            _run(
                "integration_event_rows_shipping_validation_30d",
                lambda: self.db.scalars(
                    select(AuditLog)
                    .where(
                        AuditLog.entity_type == "integration_event",
                        AuditLog.created_at >= (end_dt - timedelta(days=30)),
                    )
                    .order_by(AuditLog.created_at.desc())
                    .limit(1000)
                ).all(),
            )

        return baseline_rows

    def create_coin_ai_run(
        self,
        *,
        environment: str = "local",
        tool_name: str,
        username: str,
        product_id: int | None = None,
        listing_id: int | None = None,
        input_hint: str = "",
        image_filename: str = "",
        image_content_type: str = "",
        result_markdown: str = "",
        result_json: str = "{}",
        web_rows_json: str = "[]",
        actor: str = "system",
    ) -> CoinAIRun:
        row = CoinAIRun(
            environment=(environment or "local").strip() or "local",
            tool_name=(tool_name or "").strip().lower(),
            username=(username or "employee").strip() or "employee",
            product_id=product_id,
            listing_id=listing_id,
            input_hint=(input_hint or "").strip(),
            image_filename=(image_filename or "").strip(),
            image_content_type=(image_content_type or "").strip(),
            result_markdown=(result_markdown or "").strip(),
            result_json=(result_json or "{}").strip() or "{}",
            web_rows_json=(web_rows_json or "[]").strip() or "[]",
            created_at=utcnow_naive(),
        )
        self.db.add(row)
        self.db.flush()
        self._record_audit(
            "coin_ai_run",
            row.id,
            "create",
            actor,
            {
                "after": {
                    "tool_name": row.tool_name,
                    "username": row.username,
                    "product_id": row.product_id,
                    "listing_id": row.listing_id,
                    "image_filename": row.image_filename,
                }
            },
        )
        self.db.commit()
        self.db.refresh(row)
        return row

    def list_coin_ai_runs(
        self,
        *,
        tool_name: str | None = None,
        username: str | None = None,
        limit: int = 100,
    ) -> list[CoinAIRun]:
        query = select(CoinAIRun)
        if tool_name:
            query = query.where(CoinAIRun.tool_name == tool_name.strip().lower())
        if username:
            query = query.where(CoinAIRun.username == username.strip())
        query = query.order_by(CoinAIRun.created_at.desc(), CoinAIRun.id.desc()).limit(max(1, int(limit)))
        return self.db.scalars(query).all()

    def create_coin_reference(
        self,
        *,
        coin_name: str,
        country: str = "",
        issuer: str = "",
        denomination: str = "",
        series: str = "",
        year_start: int | None = None,
        year_end: int | None = None,
        mint_mark: str = "",
        composition: str = "",
        metal_type: str = "",
        weight_grams: Decimal | None = None,
        asw_oz: Decimal | None = None,
        diameter_mm: Decimal | None = None,
        thickness_mm: Decimal | None = None,
        km_number: str = "",
        pcgs_no: str = "",
        ngc_id: str = "",
        mintage: str = "",
        estimated_value_low: Decimal | None = None,
        estimated_value_high: Decimal | None = None,
        price_source: str = "",
        source_url: str = "",
        tags: str = "",
        obverse_description: str = "",
        reverse_description: str = "",
        notes: str = "",
        is_active: bool = True,
        actor: str = "system",
    ) -> CoinReferenceCatalog:
        ValidationService.require_non_empty("Coin name", coin_name)
        ValidationService.require_non_negative_decimal("Weight (grams)", weight_grams)
        ValidationService.require_non_negative_decimal("ASW (oz)", asw_oz)
        ValidationService.require_non_negative_decimal("Diameter (mm)", diameter_mm)
        ValidationService.require_non_negative_decimal("Thickness (mm)", thickness_mm)
        ValidationService.require_non_negative_decimal("Estimated value low", estimated_value_low)
        ValidationService.require_non_negative_decimal("Estimated value high", estimated_value_high)
        if year_start is not None and year_end is not None and int(year_end) < int(year_start):
            raise ValueError("Year end must be greater than or equal to year start.")

        row = CoinReferenceCatalog(
            coin_name=coin_name.strip(),
            country=(country or "").strip(),
            issuer=(issuer or "").strip(),
            denomination=(denomination or "").strip(),
            series=(series or "").strip(),
            year_start=year_start,
            year_end=year_end,
            mint_mark=(mint_mark or "").strip(),
            composition=(composition or "").strip(),
            metal_type=(metal_type or "").strip(),
            weight_grams=weight_grams,
            asw_oz=asw_oz,
            diameter_mm=diameter_mm,
            thickness_mm=thickness_mm,
            km_number=(km_number or "").strip(),
            pcgs_no=(pcgs_no or "").strip(),
            ngc_id=(ngc_id or "").strip(),
            mintage=(mintage or "").strip(),
            estimated_value_low=estimated_value_low,
            estimated_value_high=estimated_value_high,
            price_source=(price_source or "").strip(),
            source_url=(source_url or "").strip(),
            tags=(tags or "").strip(),
            obverse_description=(obverse_description or "").strip(),
            reverse_description=(reverse_description or "").strip(),
            notes=(notes or "").strip(),
            is_active=bool(is_active),
        )
        self.db.add(row)
        self.db.flush()
        self._record_audit(
            "coin_reference",
            row.id,
            "create",
            actor,
            {"after": {"coin_name": row.coin_name, "country": row.country, "series": row.series}},
        )
        self.db.commit()
        self.db.refresh(row)
        return row

    def update_coin_reference(
        self,
        coin_reference_id: int,
        payload: dict,
        *,
        actor: str = "system",
    ) -> CoinReferenceCatalog:
        row = self.db.get(CoinReferenceCatalog, coin_reference_id)
        if row is None:
            raise ValueError(f"Coin reference {coin_reference_id} not found.")
        changes: dict[str, Any] = {}
        allowed_fields = {
            "coin_name",
            "country",
            "issuer",
            "denomination",
            "series",
            "year_start",
            "year_end",
            "mint_mark",
            "composition",
            "metal_type",
            "weight_grams",
            "asw_oz",
            "diameter_mm",
            "thickness_mm",
            "km_number",
            "pcgs_no",
            "ngc_id",
            "mintage",
            "estimated_value_low",
            "estimated_value_high",
            "price_source",
            "source_url",
            "tags",
            "obverse_description",
            "reverse_description",
            "notes",
            "is_active",
        }
        for key, value in payload.items():
            if key not in allowed_fields:
                continue
            before = getattr(row, key)
            after = value
            if key in {
                "coin_name",
                "country",
                "issuer",
                "denomination",
                "series",
                "mint_mark",
                "composition",
                "metal_type",
                "km_number",
                "pcgs_no",
                "ngc_id",
                "mintage",
                "price_source",
                "source_url",
                "tags",
                "obverse_description",
                "reverse_description",
                "notes",
            }:
                after = (str(value or "")).strip()
            if key == "is_active":
                after = bool(value)
            if before != after:
                setattr(row, key, after)
                changes[key] = {"before": before, "after": after}

        ValidationService.require_non_empty("Coin name", row.coin_name)
        ValidationService.require_non_negative_decimal("Weight (grams)", row.weight_grams)
        ValidationService.require_non_negative_decimal("ASW (oz)", row.asw_oz)
        ValidationService.require_non_negative_decimal("Diameter (mm)", row.diameter_mm)
        ValidationService.require_non_negative_decimal("Thickness (mm)", row.thickness_mm)
        ValidationService.require_non_negative_decimal("Estimated value low", row.estimated_value_low)
        ValidationService.require_non_negative_decimal("Estimated value high", row.estimated_value_high)
        if row.year_start is not None and row.year_end is not None and int(row.year_end) < int(row.year_start):
            raise ValueError("Year end must be greater than or equal to year start.")

        if changes:
            self._record_audit("coin_reference", row.id, "update", actor, changes)
            self.db.commit()
            self.db.refresh(row)
        return row

    def list_coin_references(
        self,
        *,
        query: str | None = None,
        country: str | None = None,
        metal_type: str | None = None,
        active_only: bool = False,
        limit: int = 500,
    ) -> list[CoinReferenceCatalog]:
        stmt = select(CoinReferenceCatalog)
        if active_only:
            stmt = stmt.where(CoinReferenceCatalog.is_active.is_(True))
        if country and country.strip():
            stmt = stmt.where(CoinReferenceCatalog.country == country.strip())
        if metal_type and metal_type.strip():
            stmt = stmt.where(CoinReferenceCatalog.metal_type == metal_type.strip())
        if query and query.strip():
            token = f"%{query.strip()}%"
            stmt = stmt.where(
                or_(
                    CoinReferenceCatalog.coin_name.ilike(token),
                    CoinReferenceCatalog.series.ilike(token),
                    CoinReferenceCatalog.country.ilike(token),
                    CoinReferenceCatalog.denomination.ilike(token),
                    CoinReferenceCatalog.km_number.ilike(token),
                    CoinReferenceCatalog.pcgs_no.ilike(token),
                    CoinReferenceCatalog.ngc_id.ilike(token),
                    CoinReferenceCatalog.tags.ilike(token),
                )
            )
        stmt = stmt.order_by(
            CoinReferenceCatalog.coin_name.asc(),
            CoinReferenceCatalog.country.asc(),
            CoinReferenceCatalog.series.asc(),
            CoinReferenceCatalog.id.desc(),
        ).limit(max(1, int(limit)))
        return self.db.scalars(stmt).all()

    def log_ai_chat_interaction(
        self,
        *,
        actor: str,
        prompt: str,
        intent: str,
        allowed_domains: list[str],
        citations: list[dict[str, Any]],
        answer_preview: str = "",
        denied: bool = False,
        elapsed_ms: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "prompt": (prompt or "").strip()[:2000],
            "intent": (intent or "").strip(),
            "allowed_domains": list(allowed_domains or []),
            "citations": citations or [],
            "answer_preview": (answer_preview or "").strip()[:500],
            "denied": bool(denied),
            "elapsed_ms": int(max(0, int(elapsed_ms))),
            "metadata": metadata or {},
        }
        self._record_audit(
            entity_type="ai_chat",
            entity_id=None,
            action="query",
            actor=(actor or "system").strip() or "system",
            changes={"after": payload},
        )
        self.db.commit()

    def list_ai_chat_interactions(
        self,
        *,
        limit: int = 200,
        actor: str | None = None,
        event_type: str | None = None,
    ) -> list[dict[str, Any]]:
        stmt = (
            select(AuditLog)
            .where(AuditLog.entity_type == "ai_chat")
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(max(1, int(limit)))
        )
        if actor and str(actor).strip():
            stmt = stmt.where(AuditLog.actor == str(actor).strip())

        rows = self.db.scalars(stmt).all()
        output: list[dict[str, Any]] = []
        for row in rows:
            payload: dict[str, Any] = {}
            try:
                parsed = json.loads(row.changes_json or "{}")
                if isinstance(parsed, dict):
                    payload = parsed.get("after", {}) or {}
            except Exception:
                payload = {}

            metadata = payload.get("metadata", {}) if isinstance(payload, dict) else {}
            if not isinstance(metadata, dict):
                metadata = {}
            row_event_type = str(metadata.get("event_type") or "").strip().lower()
            if event_type and row_event_type != str(event_type).strip().lower():
                continue

            output.append(
                {
                    "id": row.id,
                    "created_at": row.created_at,
                    "actor": row.actor,
                    "intent": str(payload.get("intent") or "").strip(),
                    "denied": bool(payload.get("denied")),
                    "elapsed_ms": int(payload.get("elapsed_ms") or 0),
                    "event_type": row_event_type,
                    "goldy_mode": str(metadata.get("goldy_mode") or "").strip(),
                    "goldy_role": str(metadata.get("goldy_role") or "").strip(),
                    "goldy_plan_status": str(metadata.get("goldy_plan_status") or "").strip(),
                    "scope_env": str(metadata.get("scope_env") or "").strip(),
                    "scope_user": str(metadata.get("scope_user") or "").strip(),
                    "prompt_preview": str(payload.get("prompt") or "")[:160],
                }
            )
        return output

    def log_integration_event(
        self,
        *,
        actor: str,
        integration: str,
        action: str,
        status: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "integration": str(integration or "").strip().lower(),
            "action": str(action or "").strip().lower(),
            "status": str(status or "").strip().lower(),
            "details": details or {},
        }
        self._record_audit(
            entity_type="integration_event",
            entity_id=None,
            action=payload["action"] or "event",
            actor=(actor or "system").strip() or "system",
            changes={"after": payload},
        )
        self.db.commit()
