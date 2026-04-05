import base64
import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, or_, select

try:
    from app.db.models import (
        AIProviderConfig,
        AppUser,
        AuditLog,
        CoinAIRun,
        CoinReferenceCatalog,
        DocumentArtifact,
        DocumentTemplateProfile,
        EbayPublishPreset,
        EbayListingTemplateProfile,
        InventoryMovement,
        InventorySource,
        IntegrationAutomationApproval,
        IntegrationAutomationRule,
        IntegrationQueueJob,
        MarketplaceListing,
        MediaAsset,
        Order,
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
        EbayPublishPreset,
        EbayListingTemplateProfile,
        InventoryMovement,
        InventorySource,
        IntegrationAutomationApproval,
        IntegrationAutomationRule,
        IntegrationQueueJob,
        MarketplaceListing,
        MediaAsset,
        Order,
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
    )
    from services.validation import ValidationService
    from services.security import hash_password, verify_password
    from utils.time import utcnow_naive
    from config import settings


class InventoryRepository:
    def __init__(self, db_session):
        self.db = db_session

    def _serialize_audit_value(self, value: Any) -> Any:
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, datetime):
            return value.isoformat()
        return value

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

        product = Product(
            sku=sku,
            title=title,
            category=category,
            inventory_class=resolved_inventory_class,
            description=description,
            metal_type=metal_type,
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
            self._record_inventory_movement(
                product_id=product.id,
                movement_type="initial_stock",
                quantity_before=0,
                quantity_after=current_quantity,
                unit_cost=acquisition_cost,
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

    def list_products(self) -> list[Product]:
        return self.db.scalars(select(Product).order_by(Product.created_at.desc())).all()

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

    def list_listings(self) -> list[MarketplaceListing]:
        return self.db.scalars(
            select(MarketplaceListing).order_by(MarketplaceListing.created_at.desc())
        ).all()

    def create_sale(
        self,
        marketplace: str,
        sold_price: Decimal,
        fees: Decimal,
        shipping_cost: Decimal,
        quantity_sold: int,
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

        movement_payload: dict[str, Any] | None = None
        if product_id is not None:
            product = self.db.get(Product, product_id)
            if product:
                quantity_before = int(product.current_quantity)
                quantity_after = max(0, quantity_before - int(quantity_sold))
                product.current_quantity = quantity_after
                movement_payload = {
                    "product_id": product.id,
                    "movement_type": "sale",
                    "quantity_before": quantity_before,
                    "quantity_after": quantity_after,
                    "unit_cost": product.acquisition_cost,
                    "reference_type": "sale",
                    "notes": "Inventory reduced from recorded sale.",
                    "occurred_at": sold_at or utcnow_naive(),
                }

        self.db.add(sale)
        self.db.flush()
        if movement_payload is not None:
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

        if ret.restocked and ret.product_id is not None:
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

            if old_restocked != new_restocked or old_quantity != new_quantity or old_product_id != new_product_id:
                if old_restocked and old_product_id is not None:
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

                if new_restocked and new_product_id is not None:
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
        fees: Decimal | None = None,
        shipping_cost: Decimal | None = None,
        notes: str = "",
        actor: str = "system",
    ) -> Order:
        ValidationService.require_non_empty("Marketplace", marketplace)
        ValidationService.require_non_negative_decimal("Order fees", fees)
        ValidationService.require_non_negative_decimal("Order shipping cost", shipping_cost)

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
        total_amount = subtotal

        order = Order(
            marketplace=marketplace,
            external_order_id=resolved_external_order_id,
            order_status=order_status,
            subtotal_amount=subtotal,
            fees=fees_value,
            shipping_cost=shipping_cost_value,
            total_amount=total_amount,
            sold_at=sold_at,
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

    def list_order_items(self) -> list[OrderItem]:
        return self.db.scalars(select(OrderItem).order_by(OrderItem.created_at.desc())).all()

    def update_order(self, order_id: int, updates: dict[str, Any], actor: str = "system") -> Order:
        order = self.db.get(Order, order_id)
        if order is None:
            raise ValueError(f"Order {order_id} not found.")

        new_marketplace = updates.get("marketplace", order.marketplace)
        new_external_order_id = updates.get("external_order_id", order.external_order_id)
        new_fees = updates.get("fees", order.fees)
        new_shipping_cost = updates.get("shipping_cost", order.shipping_cost)
        ValidationService.require_non_empty("Marketplace", new_marketplace)
        ValidationService.require_non_negative_decimal("Order fees", new_fees)
        ValidationService.require_non_negative_decimal("Order shipping cost", new_shipping_cost)
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

    def list_media_assets(self) -> list[MediaAsset]:
        return self.db.scalars(select(MediaAsset).order_by(MediaAsset.created_at.desc())).all()

    def list_media_assets_for_product(self, product_id: int) -> list[MediaAsset]:
        return self.db.scalars(
            select(MediaAsset)
            .where(MediaAsset.product_id == product_id)
            .order_by(MediaAsset.created_at.desc())
        ).all()

    def list_media_assets_for_listing(self, listing_id: int) -> list[MediaAsset]:
        return self.db.scalars(
            select(MediaAsset)
            .where(MediaAsset.listing_id == listing_id)
            .order_by(MediaAsset.created_at.desc())
        ).all()

    def create_purchase_lot(
        self,
        lot_code: str,
        vendor: str,
        purchase_date: datetime,
        total_cost: Decimal | None,
        total_tax_paid: Decimal | None = None,
        total_shipping_paid: Decimal | None = None,
        total_handling_paid: Decimal | None = None,
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
        lot = PurchaseLot(
            source_id=source_id,
            lot_code=lot_code,
            vendor=resolved_vendor,
            purchase_date=purchase_date,
            total_cost=total_cost,
            total_tax_paid=total_tax_paid,
            total_shipping_paid=total_shipping_paid,
            total_handling_paid=total_handling_paid,
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
    ) -> ProductLotAssignment:
        ValidationService.require_non_negative_decimal("Unit cost", unit_cost)
        ValidationService.require_non_negative_decimal("Unit tax paid", unit_tax_paid)
        ValidationService.require_non_negative_decimal("Unit shipping paid", unit_shipping_paid)
        ValidationService.require_non_negative_decimal("Unit handling paid", unit_handling_paid)
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
            acquired_at=acquired_at,
        )
        self.db.add(assignment)
        self.db.flush()
        self._record_audit(
            entity_type="product_lot_assignment",
            entity_id=assignment.id,
            action="create",
            actor="system",
            changes={"after": {"product_id": product_id, "lot_id": lot_id}},
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

        occurred = acquired_at or utcnow_naive()
        qty_before = int(product.current_quantity or 0)
        qty_after = qty_before + int(quantity_acquired)

        assignment = None
        allocated_cost = (unit_cost * int(quantity_acquired)) if unit_cost is not None else None
        if lot_id is not None:
            assignment = ProductLotAssignment(
                product_id=product.id,
                lot_id=lot_id,
                quantity_acquired=int(quantity_acquired),
                unit_cost=unit_cost,
                allocated_cost=allocated_cost,
                acquired_at=occurred,
            )
            self.db.add(assignment)
            self.db.flush()

        existing_unit_cost = product.acquisition_cost
        new_unit_cost = existing_unit_cost
        if unit_cost is not None:
            if existing_unit_cost is not None and qty_before > 0:
                weighted_total = (existing_unit_cost * qty_before) + (unit_cost * int(quantity_acquired))
                new_unit_cost = weighted_total / Decimal(qty_after)
            else:
                new_unit_cost = unit_cost

        product.current_quantity = qty_after
        product.acquisition_cost = new_unit_cost
        product.acquired_at = occurred

        self._record_inventory_movement(
            product_id=product.id,
            movement_type="repurchase_in",
            quantity_before=qty_before,
            quantity_after=qty_after,
            unit_cost=unit_cost,
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
                "repurchase": {
                    "quantity_acquired": int(quantity_acquired),
                    "unit_cost": self._serialize_audit_value(unit_cost),
                    "allocated_cost": self._serialize_audit_value(allocated_cost),
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

    def list_inventory_movements(self, limit: int = 500) -> list[InventoryMovement]:
        return self.db.scalars(
            select(InventoryMovement).order_by(InventoryMovement.occurred_at.desc()).limit(limit)
        ).all()

    def update_product(self, product_id: int, updates: dict[str, Any], actor: str = "system") -> Product:
        product = self.db.get(Product, product_id)
        if product is None:
            raise ValueError(f"Product {product_id} not found.")

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
            inventory_fields = {"product_id", "quantity_sold"}
            inventory_changed = any(field in changes for field in inventory_fields)
            if inventory_changed:
                if old_product_id is not None:
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

                if new_product_id is not None:
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
        product_count = self.db.scalar(select(func.count()).select_from(Product)) or 0
        listing_count = self.db.scalar(select(func.count()).select_from(MarketplaceListing)) or 0
        sale_count = self.db.scalar(select(func.count()).select_from(Sale)) or 0

        inventory_cost = (
            self.db.scalar(
                select(func.coalesce(func.sum(Product.acquisition_cost * Product.current_quantity), 0))
            )
            or 0
        )

        gross_sales = self.db.scalar(select(func.coalesce(func.sum(Sale.sold_price), 0))) or 0
        net_sales = (
            self.db.scalar(
                select(func.coalesce(func.sum(Sale.sold_price - Sale.fees - Sale.shipping_cost), 0))
            )
            or 0
        )

        return {
            "product_count": int(product_count),
            "listing_count": int(listing_count),
            "sale_count": int(sale_count),
            "inventory_cost": float(inventory_cost),
            "gross_sales": float(gross_sales),
            "net_sales": float(net_sales),
        }

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
