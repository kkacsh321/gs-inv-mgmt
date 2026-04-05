import unittest
import json
from datetime import datetime
from decimal import Decimal

from app.db.models import AuditLog, Base, CoinReferenceCatalog, Product, Sale
from app.repository import InventoryRepository
from app.auth import has_permission
from app.config import settings
from test_support import create_test_product, in_memory_repo


class InventoryMovementsRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._repo_context = in_memory_repo()
        self.db, self.repo = self._repo_context.__enter__()

    def tearDown(self) -> None:
        self._repo_context.__exit__(None, None, None)

    def _create_product(self, sku: str = "GS-TEST-001", qty: int = 10) -> Product:
        return create_test_product(self.repo, sku_seed=sku, qty=qty)

    def test_create_sale_records_movement_and_updates_inventory(self) -> None:
        product = self._create_product(qty=10)
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("10.00"),
            shipping_cost=Decimal("5.00"),
            quantity_sold=3,
            product_id=product.id,
            sold_at=datetime(2026, 3, 2, 15, 30, 0),
        )

        refreshed = self.db.get(Product, product.id)
        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed.current_quantity, 7)

        movements = self.repo.list_inventory_movements(limit=50)
        sale_movement = next((m for m in movements if m.movement_type == "sale"), None)
        self.assertIsNotNone(sale_movement)
        self.assertEqual(sale_movement.reference_type, "sale")
        self.assertEqual(sale_movement.reference_id, sale.id)
        self.assertEqual(sale_movement.quantity_before, 10)
        self.assertEqual(sale_movement.quantity_after, 7)
        self.assertEqual(sale_movement.quantity_delta, -3)

    def test_update_sale_non_inventory_fields_does_not_create_movement(self) -> None:
        product = self._create_product(sku="GS-TEST-002", qty=12)
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("75.00"),
            fees=Decimal("8.00"),
            shipping_cost=Decimal("4.00"),
            quantity_sold=2,
            product_id=product.id,
            tracking_status="label_created",
            sold_at=datetime(2026, 3, 3, 9, 0, 0),
        )
        movement_count_before = len(self.repo.list_inventory_movements(limit=200))
        qty_before = self.db.get(Product, product.id).current_quantity

        self.repo.update_sale(
            sale.id,
            {"tracking_status": "in_transit", "shipping_provider": "usps"},
            actor="qa-user",
        )

        movement_count_after = len(self.repo.list_inventory_movements(limit=200))
        qty_after = self.db.get(Product, product.id).current_quantity
        self.assertEqual(movement_count_after, movement_count_before)
        self.assertEqual(qty_after, qty_before)

    def test_update_sale_quantity_records_revert_and_apply_movements(self) -> None:
        product = self._create_product(sku="GS-TEST-003", qty=10)
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("50.00"),
            fees=Decimal("5.00"),
            shipping_cost=Decimal("3.00"),
            quantity_sold=2,
            product_id=product.id,
            sold_at=datetime(2026, 3, 4, 10, 0, 0),
        )
        qty_after_initial_sale = self.db.get(Product, product.id).current_quantity
        self.assertEqual(qty_after_initial_sale, 8)

        self.repo.update_sale(
            sale.id,
            {"quantity_sold": 5},
            actor="ops-user",
        )

        refreshed = self.db.get(Product, product.id)
        self.assertEqual(refreshed.current_quantity, 5)

        movements = self.repo.list_inventory_movements(limit=200)
        movement_types = [m.movement_type for m in movements]
        self.assertIn("sale_adjustment_revert", movement_types)
        self.assertIn("sale_adjustment_apply", movement_types)

        revert_row = next(m for m in movements if m.movement_type == "sale_adjustment_revert")
        apply_row = next(m for m in movements if m.movement_type == "sale_adjustment_apply")
        self.assertEqual(revert_row.reference_id, sale.id)
        self.assertEqual(apply_row.reference_id, sale.id)
        self.assertEqual(revert_row.quantity_delta, 2)
        self.assertEqual(apply_row.quantity_delta, -5)

    def test_create_lot_with_source_uses_standardized_source(self) -> None:
        source = self.repo.create_inventory_source(
            name="APMEX",
            source_type="dealer",
            contact_name="Rep",
            contact_email="rep@example.com",
            is_active=True,
        )
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-20260323-A",
            vendor="",
            purchase_date=datetime(2026, 3, 23, 8, 0, 0),
            total_cost=Decimal("500.00"),
            notes="test lot",
            source_id=source.id,
        )
        self.assertEqual(lot.source_id, source.id)
        self.assertEqual(lot.vendor, "APMEX")

    def test_create_order_creates_order_and_line_items(self) -> None:
        p1 = self._create_product(sku="GS-ORD-001", qty=10)
        p2 = self._create_product(sku="GS-ORD-002", qty=5)
        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="EBAY-ORDER-1",
            order_status="paid",
            sold_at=datetime(2026, 3, 23, 10, 0, 0),
            fees=Decimal("6.00"),
            shipping_cost=Decimal("4.00"),
            notes="multi-line order",
            items=[
                {"product_id": p1.id, "listing_id": None, "quantity": 2, "unit_price": Decimal("30.00")},
                {"product_id": p2.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("45.00")},
            ],
            actor="qa-user",
        )
        self.assertEqual(order.marketplace, "ebay")
        self.assertEqual(float(order.subtotal_amount), 105.0)
        items = self.repo.list_order_items()
        order_items = [i for i in items if i.order_id == order.id]
        self.assertEqual(len(order_items), 2)

    def test_create_order_allows_unmapped_line_items(self) -> None:
        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="EBAY-ORDER-UNMAPPED-1",
            order_status="paid",
            sold_at=datetime(2026, 3, 23, 10, 30, 0),
            fees=Decimal("1.50"),
            shipping_cost=Decimal("2.50"),
            notes="imported order with unknown sku mapping",
            items=[
                {"product_id": None, "listing_id": None, "quantity": 1, "unit_price": Decimal("19.99")},
            ],
            actor="qa-user",
        )
        self.assertEqual(order.external_order_id, "EBAY-ORDER-UNMAPPED-1")
        order_items = [i for i in self.repo.list_order_items() if i.order_id == order.id]
        self.assertEqual(len(order_items), 1)
        self.assertIsNone(order_items[0].product_id)

    def test_update_order_not_found_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "Order 999 not found"):
            self.repo.update_order(999, {"order_status": "paid"}, actor="qa-user")

    def test_update_order_blocks_duplicate_marketplace_external_order_id(self) -> None:
        p1 = self._create_product(sku="GS-ORD-UPD-001", qty=4)
        p2 = self._create_product(sku="GS-ORD-UPD-002", qty=4)
        order_1 = self.repo.create_order(
            marketplace="ebay",
            external_order_id="EBAY-ORDER-UPD-1",
            order_status="paid",
            sold_at=datetime(2026, 3, 24, 9, 0, 0),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("0.00"),
            items=[{"product_id": p1.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("20.00")}],
            actor="qa-user",
        )
        self.repo.create_order(
            marketplace="ebay",
            external_order_id="EBAY-ORDER-UPD-2",
            order_status="paid",
            sold_at=datetime(2026, 3, 24, 10, 0, 0),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("0.00"),
            items=[{"product_id": p2.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("21.00")}],
            actor="qa-user",
        )
        with self.assertRaises(ValueError):
            self.repo.update_order(order_1.id, {"external_order_id": "EBAY-ORDER-UPD-2"}, actor="qa-user")

    def test_update_order_no_changes_does_not_add_update_audit(self) -> None:
        product = self._create_product(sku="GS-ORD-UPD-003", qty=3)
        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="EBAY-ORDER-UPD-3",
            order_status="paid",
            sold_at=datetime(2026, 3, 24, 11, 0, 0),
            fees=Decimal("1.00"),
            shipping_cost=Decimal("2.00"),
            items=[{"product_id": product.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("30.00")}],
            actor="qa-user",
        )
        before = [
            row
            for row in self.repo.list_audit_logs(limit=200)
            if row.entity_type == "order" and row.entity_id == order.id and row.action == "update"
        ]
        self.repo.update_order(order.id, {"order_status": "paid", "fees": Decimal("1.00")}, actor="qa-user")
        after = [
            row
            for row in self.repo.list_audit_logs(limit=200)
            if row.entity_type == "order" and row.entity_id == order.id and row.action == "update"
        ]
        self.assertEqual(len(after), len(before))

    def test_create_sale_can_link_to_order(self) -> None:
        product = self._create_product(sku="GS-ORD-003", qty=8)
        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="EBAY-ORDER-2",
            order_status="paid",
            sold_at=datetime(2026, 3, 23, 11, 0, 0),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("0.00"),
            items=[{"product_id": product.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("20.00")}],
            actor="qa-user",
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("20.00"),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("0.00"),
            quantity_sold=1,
            order_id=order.id,
            product_id=product.id,
            external_order_id=order.external_order_id,
            sold_at=datetime(2026, 3, 23, 11, 5, 0),
        )
        self.assertEqual(sale.order_id, order.id)

    def test_update_sale_not_found_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "Sale 999 not found"):
            self.repo.update_sale(999, {"tracking_status": "in_transit"}, actor="qa-user")

    def test_update_sale_unknown_fields_and_noop_do_not_create_update_audit(self) -> None:
        product = self._create_product(sku="GS-SALE-NOOP", qty=10)
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("20.00"),
            fees=Decimal("1.00"),
            shipping_cost=Decimal("2.00"),
            quantity_sold=1,
            product_id=product.id,
            sold_at=datetime(2026, 3, 4, 11, 0, 0),
        )
        before = [
            row
            for row in self.repo.list_audit_logs(limit=200)
            if row.entity_type == "sale" and row.entity_id == sale.id and row.action == "update"
        ]
        result = self.repo.update_sale(
            sale.id,
            {"unknown_field": "ignored", "quantity_sold": sale.quantity_sold},
            actor="qa-user",
        )
        self.assertEqual(result.id, sale.id)
        after = [
            row
            for row in self.repo.list_audit_logs(limit=200)
            if row.entity_type == "sale" and row.entity_id == sale.id and row.action == "update"
        ]
        self.assertEqual(len(after), len(before))

    def test_update_sale_reassign_product_reverts_old_and_applies_new(self) -> None:
        old_product = self._create_product(sku="GS-SALE-UPD-OLD", qty=10)
        new_product = self._create_product(sku="GS-SALE-UPD-NEW", qty=8)
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("55.00"),
            fees=Decimal("3.00"),
            shipping_cost=Decimal("2.00"),
            quantity_sold=2,
            product_id=old_product.id,
            sold_at=datetime(2026, 3, 25, 9, 0, 0),
        )
        self.assertEqual(self.db.get(Product, old_product.id).current_quantity, 8)
        self.assertEqual(self.db.get(Product, new_product.id).current_quantity, 8)

        self.repo.update_sale(
            sale.id,
            {"product_id": new_product.id, "quantity_sold": 3},
            actor="qa-user",
        )
        self.assertEqual(self.db.get(Product, old_product.id).current_quantity, 10)
        self.assertEqual(self.db.get(Product, new_product.id).current_quantity, 5)

        movements = [m for m in self.repo.list_inventory_movements(limit=500) if m.reference_id == sale.id]
        movement_types = [m.movement_type for m in movements]
        self.assertIn("sale_adjustment_revert", movement_types)
        self.assertIn("sale_adjustment_apply", movement_types)

    def test_update_sale_allows_same_tracking_for_same_sale(self) -> None:
        product = self._create_product(sku="GS-SALE-UPD-TRK-1", qty=6)
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("30.00"),
            fees=Decimal("2.00"),
            shipping_cost=Decimal("2.00"),
            quantity_sold=1,
            product_id=product.id,
            tracking_status="label_created",
            tracking_number="1Z12345E0205271688",
            external_order_id="ORDER-SAME-TRK",
            sold_at=datetime(2026, 3, 25, 10, 0, 0),
        )
        updated = self.repo.update_sale(
            sale.id,
            {"tracking_status": "in_transit", "tracking_number": "1Z12345E0205271688"},
            actor="qa-user",
        )
        self.assertEqual(updated.tracking_status, "in_transit")

    def test_update_sale_blocks_reused_tracking_from_another_sale(self) -> None:
        product = self._create_product(sku="GS-SALE-UPD-TRK-2", qty=10)
        self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("25.00"),
            fees=Decimal("1.00"),
            shipping_cost=Decimal("1.00"),
            quantity_sold=1,
            product_id=product.id,
            tracking_status="label_created",
            tracking_number="1Z12345E0205271111",
            external_order_id="ORDER-TRK-A",
            sold_at=datetime(2026, 3, 25, 11, 0, 0),
        )
        sale_b = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("26.00"),
            fees=Decimal("1.00"),
            shipping_cost=Decimal("1.00"),
            quantity_sold=1,
            product_id=product.id,
            tracking_status="pending",
            tracking_number="",
            external_order_id="ORDER-TRK-B",
            sold_at=datetime(2026, 3, 25, 12, 0, 0),
        )
        with self.assertRaises(ValueError):
            self.repo.update_sale(
                sale_b.id,
                {"tracking_status": "label_created", "tracking_number": "1Z12345E0205271111"},
                actor="qa-user",
            )

    def test_create_return_restock_increases_inventory_and_logs_movement(self) -> None:
        product = self._create_product(sku="GS-RET-001", qty=4)
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("40.00"),
            fees=Decimal("4.00"),
            shipping_cost=Decimal("0.00"),
            quantity_sold=1,
            product_id=product.id,
            sold_at=datetime(2026, 3, 23, 12, 0, 0),
        )
        qty_after_sale = self.db.get(Product, product.id).current_quantity
        self.assertEqual(qty_after_sale, 3)

        ret = self.repo.create_return(
            marketplace="ebay",
            sale_id=sale.id,
            quantity=1,
            refund_amount=Decimal("40.00"),
            return_status="processed",
            disposition="restock",
            restocked=True,
            returned_at=datetime(2026, 3, 24, 9, 0, 0),
            actor="qa-user",
        )
        self.assertEqual(ret.sale_id, sale.id)
        qty_after_return = self.db.get(Product, product.id).current_quantity
        self.assertEqual(qty_after_return, 4)

        movements = self.repo.list_inventory_movements(limit=200)
        self.assertTrue(any(m.reference_type == "return" and m.reference_id == ret.id for m in movements))

    def test_update_return_toggle_restock_reconciles_inventory(self) -> None:
        product = self._create_product(sku="GS-RET-002", qty=6)
        ret = self.repo.create_return(
            marketplace="ebay",
            product_id=product.id,
            quantity=2,
            refund_amount=Decimal("10.00"),
            return_status="processed",
            disposition="damaged",
            restocked=False,
            returned_at=datetime(2026, 3, 25, 8, 0, 0),
            actor="qa-user",
        )
        self.assertEqual(self.db.get(Product, product.id).current_quantity, 6)

        self.repo.update_return(ret.id, {"restocked": True, "disposition": "restock"}, actor="qa-user")
        self.assertEqual(self.db.get(Product, product.id).current_quantity, 8)

        self.repo.update_return(ret.id, {"restocked": False, "disposition": "scrap"}, actor="qa-user")
        self.assertEqual(self.db.get(Product, product.id).current_quantity, 6)

    def test_create_shipping_preset_and_manage_default(self) -> None:
        p1 = self.repo.create_shipping_preset(
            name="USPS Ground",
            shipping_provider="usps",
            shipping_service="Ground Advantage",
            shipping_package_type="small_box",
            is_default=True,
            actor="qa-user",
        )
        p2 = self.repo.create_shipping_preset(
            name="UPS Saver",
            shipping_provider="ups",
            shipping_service="2nd Day Air",
            shipping_package_type="medium_box",
            is_default=False,
            actor="qa-user",
        )
        self.assertTrue(p1.is_default)
        self.assertFalse(p2.is_default)

        self.repo.update_shipping_preset(p2.id, {"is_default": True}, actor="qa-user")
        refreshed = self.repo.list_shipping_presets(active_only=False)
        default_presets = [p for p in refreshed if p.is_default]
        self.assertEqual(len(default_presets), 1)
        self.assertEqual(default_presets[0].id, p2.id)

    def test_mark_shipments_exported_sets_timestamp(self) -> None:
        product = self._create_product(sku="GS-EXP-001", qty=3)
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("60.00"),
            fees=Decimal("6.00"),
            shipping_cost=Decimal("4.00"),
            quantity_sold=1,
            product_id=product.id,
            sold_at=datetime(2026, 3, 26, 10, 0, 0),
        )
        updated = self.repo.mark_shipments_exported([sale.id], actor="qa-user")
        self.assertEqual(updated, 1)
        refreshed_sale = self.db.get(Sale, sale.id)
        self.assertIsNotNone(refreshed_sale.shipment_exported_at)

    def test_document_template_profiles_default_is_per_env_and_doc_type(self) -> None:
        p1 = self.repo.create_document_template_profile(
            environment="local",
            doc_type="invoice",
            name="Local Invoice A",
            template_name="Classic",
            accent_color="#111111",
            company_name="GoldenStackers",
            is_default=True,
            actor="qa-user",
        )
        p2 = self.repo.create_document_template_profile(
            environment="local",
            doc_type="invoice",
            name="Local Invoice B",
            template_name="Merchant Modern",
            accent_color="#222222",
            company_name="GoldenStackers",
            is_default=False,
            actor="qa-user",
        )
        p3 = self.repo.create_document_template_profile(
            environment="dev",
            doc_type="invoice",
            name="Dev Invoice A",
            template_name="Ledger Dark",
            accent_color="#333333",
            company_name="GoldenStackers Dev",
            is_default=True,
            actor="qa-user",
        )

        self.assertTrue(p1.is_default)
        self.assertFalse(p2.is_default)
        self.assertTrue(p3.is_default)

        self.repo.update_document_template_profile(p2.id, {"is_default": True}, actor="qa-user")

        local_invoice_profiles = self.repo.list_document_template_profiles(
            environment="local",
            doc_type="invoice",
            include_all_doc_type=False,
            active_only=False,
        )
        local_defaults = [p for p in local_invoice_profiles if p.is_default]
        self.assertEqual(len(local_defaults), 1)
        self.assertEqual(local_defaults[0].id, p2.id)

        dev_invoice_profiles = self.repo.list_document_template_profiles(
            environment="dev",
            doc_type="invoice",
            include_all_doc_type=False,
            active_only=False,
        )
        dev_defaults = [p for p in dev_invoice_profiles if p.is_default]
        self.assertEqual(len(dev_defaults), 1)
        self.assertEqual(dev_defaults[0].id, p3.id)

    def test_create_listing_blocks_duplicate_marketplace_external_id(self) -> None:
        product = self._create_product(sku="GS-LST-001", qty=2)
        self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="First Listing",
            listing_price=Decimal("10.00"),
            quantity_listed=1,
            external_listing_id="EBAY-LIST-1",
        )
        with self.assertRaises(ValueError):
            self.repo.create_listing(
                product_id=product.id,
                marketplace="ebay",
                listing_title="Duplicate Listing",
                listing_price=Decimal("11.00"),
                quantity_listed=1,
                external_listing_id="EBAY-LIST-1",
            )

    def test_create_listing_allows_multiple_blank_external_ids(self) -> None:
        product = self._create_product(sku="GS-LST-002", qty=2)
        first = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Draft Listing A",
            listing_price=Decimal("10.00"),
            quantity_listed=1,
            external_listing_id="",
        )
        second = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Draft Listing B",
            listing_price=Decimal("11.00"),
            quantity_listed=1,
            external_listing_id="",
        )
        self.assertNotEqual(first.id, second.id)

    def test_create_listing_forces_draft_and_pending_review(self) -> None:
        product = self._create_product(sku="GS-LST-003", qty=2)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Attempt Active",
            listing_price=Decimal("15.00"),
            quantity_listed=1,
            listing_status="active",
        )
        self.assertEqual(listing.listing_status, "draft")
        self.assertEqual(listing.review_status, "pending")

    def test_update_listing_blocks_active_when_not_review_approved(self) -> None:
        product = self._create_product(sku="GS-LST-004", qty=2)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Needs Review",
            listing_price=Decimal("15.00"),
            quantity_listed=1,
        )
        with self.assertRaisesRegex(ValueError, "approved in review"):
            self.repo.update_listing(
                listing.id,
                {"listing_status": "active"},
                actor="ops-user",
            )

    def test_review_listing_rejected_demotes_active_to_draft(self) -> None:
        product = self._create_product(sku="GS-LST-005", qty=2)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Review History",
            listing_price=Decimal("15.00"),
            quantity_listed=1,
        )
        approved = self.repo.review_listing(
            listing.id,
            decision="approved",
            actor="reviewer1",
            notes="Looks good",
        )
        active = self.repo.update_listing(
            approved.id,
            {"listing_status": "active", "review_status": "approved", "reviewed_by": "reviewer1"},
            actor="publisher1",
        )
        self.assertEqual(active.listing_status, "active")

        rejected = self.repo.review_listing(
            listing.id,
            decision="rejected",
            actor="reviewer2",
            notes="Needs edits",
        )
        self.assertEqual(rejected.listing_status, "draft")
        self.assertEqual(rejected.review_status, "rejected")
        self.assertIn("review_history", (rejected.marketplace_details or ""))
        self.assertIn("Needs edits", (rejected.marketplace_details or ""))

    def test_update_listing_enforces_two_person_review_policy(self) -> None:
        product = self._create_product(sku="GS-LST-006", qty=2)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Two Person Policy",
            listing_price=Decimal("19.00"),
            quantity_listed=1,
        )
        self.repo.review_listing(listing.id, decision="approved", actor="reviewer1", notes="approved")
        self.repo.upsert_runtime_setting(
            environment=settings.app_env,
            key="listing_review_two_person_required",
            value="true",
            value_type="bool",
            actor="qa-user",
        )
        self.repo.upsert_runtime_setting(
            environment=settings.app_env,
            key="listing_review_two_person_channels_csv",
            value="ebay",
            value_type="str",
            actor="qa-user",
        )

        with self.assertRaisesRegex(ValueError, "Two-person review policy"):
            self.repo.update_listing(
                listing.id,
                {"listing_status": "active", "review_status": "approved", "reviewed_by": "reviewer1"},
                actor="reviewer1",
            )

        updated = self.repo.update_listing(
            listing.id,
            {"listing_status": "active", "review_status": "approved", "reviewed_by": "reviewer1"},
            actor="publisher2",
        )
        self.assertEqual(updated.listing_status, "active")

    def test_update_listing_not_found_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "Listing 999 not found"):
            self.repo.update_listing(999, {"listing_title": "Updated"}, actor="qa-user")

    def test_review_listing_rejects_invalid_decision_and_not_found(self) -> None:
        product = self._create_product(sku="GS-LST-007", qty=2)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Bad Decision",
            listing_price=Decimal("10.00"),
            quantity_listed=1,
        )
        with self.assertRaisesRegex(ValueError, "approved, rejected, pending"):
            self.repo.review_listing(listing.id, decision="ship-it", actor="qa-user")
        with self.assertRaisesRegex(ValueError, "Listing 999 not found"):
            self.repo.review_listing(999, decision="approved", actor="qa-user")

    def test_review_listing_handles_non_json_details_and_bounds_history(self) -> None:
        product = self._create_product(sku="GS-LST-008", qty=2)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="History Bounds",
            listing_price=Decimal("12.00"),
            quantity_listed=1,
            marketplace_details="legacy free-text details",
        )
        history = [
            {
                "decision": "approved",
                "actor": f"reviewer-{i}",
                "reviewed_at": datetime(2026, 3, 1, 0, 0, 0).isoformat(),
                "notes": "old",
            }
            for i in range(120)
        ]
        details = json.dumps({"review_history": history})
        self.repo.update_listing(listing.id, {"marketplace_details": details}, actor="qa-user")

        reviewed = self.repo.review_listing(
            listing.id,
            decision="approved",
            actor="reviewer-final",
            notes="final note",
        )
        payload = json.loads(reviewed.marketplace_details or "{}")
        self.assertIn("review", payload)
        self.assertEqual(payload["review"]["actor"], "reviewer-final")
        self.assertEqual(len(payload.get("review_history", [])), 100)
        self.assertEqual(payload["review_history"][-1]["notes"], "final note")

    def test_review_listing_handles_json_non_object_details_payload(self) -> None:
        product = self._create_product(sku="GS-LST-JSONLIST", qty=2)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="JSON List Details",
            listing_price=Decimal("9.00"),
            quantity_listed=1,
            marketplace_details='["legacy", "list"]',
        )
        reviewed = self.repo.review_listing(
            listing.id,
            decision="pending",
            actor="reviewer-json",
            notes="normalize non-object details",
        )
        payload = json.loads(reviewed.marketplace_details or "{}")
        self.assertIsInstance(payload, dict)
        self.assertIn("notes", payload)
        self.assertIn("review", payload)
        self.assertEqual(payload["review"]["actor"], "reviewer-json")

    def test_ebay_publish_presets_are_scoped_by_user_env_and_default(self) -> None:
        p1 = self.repo.create_ebay_publish_preset(
            environment="dev",
            username="ops1",
            name="Coins",
            marketplace_id="EBAY_US",
            currency="USD",
            content_language="en-US",
            merchant_location_key="LOC1",
            payment_policy_id="PAY1",
            fulfillment_policy_id="FUL1",
            return_policy_id="RET1",
            category_id="11111",
            format_type="FIXED_PRICE",
            listing_duration="GTC",
            condition_value="NEW",
            is_default=True,
            actor="qa-user",
        )
        p2 = self.repo.create_ebay_publish_preset(
            environment="dev",
            username="ops1",
            name="Bullion Auction",
            marketplace_id="EBAY_US",
            currency="USD",
            content_language="en-US",
            merchant_location_key="LOC1",
            payment_policy_id="PAY1",
            fulfillment_policy_id="FUL1",
            return_policy_id="RET1",
            category_id="22222",
            format_type="AUCTION",
            listing_duration="DAYS_7",
            condition_value="NEW",
            is_default=True,
            actor="qa-user",
        )
        p3 = self.repo.create_ebay_publish_preset(
            environment="prod",
            username="ops1",
            name="Prod Default",
            marketplace_id="EBAY_US",
            currency="USD",
            content_language="en-US",
            merchant_location_key="LOC2",
            payment_policy_id="PAY2",
            fulfillment_policy_id="FUL2",
            return_policy_id="RET2",
            category_id="33333",
            format_type="FIXED_PRICE",
            listing_duration="GTC",
            condition_value="NEW",
            is_default=True,
            actor="qa-user",
        )

        dev_rows = self.repo.list_ebay_publish_presets(environment="dev", username="ops1", active_only=False)
        self.assertEqual([row.id for row in dev_rows], [p2.id, p1.id])
        self.assertTrue(dev_rows[0].is_default)
        self.assertFalse(dev_rows[1].is_default)

        prod_rows = self.repo.list_ebay_publish_presets(environment="prod", username="ops1", active_only=False)
        self.assertEqual(len(prod_rows), 1)
        self.assertEqual(prod_rows[0].id, p3.id)

        self.repo.update_ebay_publish_preset(p1.id, {"is_default": True}, actor="qa-user")
        refreshed_dev = self.repo.list_ebay_publish_presets(environment="dev", username="ops1", active_only=False)
        defaults = [row for row in refreshed_dev if row.is_default]
        self.assertEqual(len(defaults), 1)
        self.assertEqual(defaults[0].id, p1.id)

    def test_create_order_blocks_duplicate_marketplace_external_order_id(self) -> None:
        product = self._create_product(sku="GS-ORD-DUP-001", qty=3)
        payload = {
            "marketplace": "ebay",
            "external_order_id": "EBAY-ORDER-DUP-1",
            "order_status": "paid",
            "sold_at": datetime(2026, 3, 23, 11, 0, 0),
            "fees": Decimal("0.00"),
            "shipping_cost": Decimal("0.00"),
            "items": [
                {"product_id": product.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("20.00")}
            ],
            "actor": "qa-user",
        }
        self.repo.create_order(**payload)
        with self.assertRaises(ValueError):
            self.repo.create_order(**payload)

    def test_create_sale_requires_tracking_number_for_delivered_status(self) -> None:
        product = self._create_product(sku="GS-SALE-TRK-001", qty=5)
        with self.assertRaises(ValueError):
            self.repo.create_sale(
                marketplace="ebay",
                sold_price=Decimal("30.00"),
                fees=Decimal("3.00"),
                shipping_cost=Decimal("2.00"),
                quantity_sold=1,
                product_id=product.id,
                tracking_status="delivered",
                tracking_number="",
                sold_at=datetime(2026, 3, 23, 12, 0, 0),
            )

    def test_create_sale_blocks_invalid_tracking_format(self) -> None:
        product = self._create_product(sku="GS-SALE-TRK-002", qty=5)
        with self.assertRaises(ValueError):
            self.repo.create_sale(
                marketplace="ebay",
                sold_price=Decimal("30.00"),
                fees=Decimal("3.00"),
                shipping_cost=Decimal("2.00"),
                quantity_sold=1,
                product_id=product.id,
                tracking_status="label_created",
                tracking_number="bad tracking with spaces",
                sold_at=datetime(2026, 3, 23, 12, 0, 0),
            )

    def test_create_sale_blocks_tracking_reuse_across_different_orders(self) -> None:
        product = self._create_product(sku="GS-SALE-TRK-003", qty=10)
        self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("30.00"),
            fees=Decimal("3.00"),
            shipping_cost=Decimal("2.00"),
            quantity_sold=1,
            product_id=product.id,
            tracking_status="label_created",
            tracking_number="1Z12345E0205271688",
            external_order_id="ORDER-A",
            sold_at=datetime(2026, 3, 23, 12, 0, 0),
        )
        with self.assertRaises(ValueError):
            self.repo.create_sale(
                marketplace="ebay",
                sold_price=Decimal("31.00"),
                fees=Decimal("3.00"),
                shipping_cost=Decimal("2.00"),
                quantity_sold=1,
                product_id=product.id,
                tracking_status="label_created",
                tracking_number="1Z12345E0205271688",
                external_order_id="ORDER-B",
                sold_at=datetime(2026, 3, 23, 13, 0, 0),
            )

    def test_create_sale_audit_uses_passed_actor(self) -> None:
        product = self._create_product(sku="GS-AUD-001", qty=4)
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("25.00"),
            fees=Decimal("1.00"),
            shipping_cost=Decimal("2.00"),
            quantity_sold=1,
            product_id=product.id,
            actor="qa-user",
            sold_at=datetime(2026, 3, 27, 10, 0, 0),
        )
        logs = self.repo.list_audit_logs(limit=100)
        row = next((l for l in logs if l.entity_type == "sale" and l.entity_id == sale.id and l.action == "create"), None)
        self.assertIsNotNone(row)
        self.assertEqual(row.actor, "qa-user")

    def test_media_assets_create_list_update_and_not_found(self) -> None:
        product = self._create_product(sku="GS-MEDIA-001", qty=2)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Media Listing",
            listing_price=Decimal("12.00"),
            quantity_listed=1,
        )
        media = self.repo.create_media_asset(
            media_type="image",
            original_filename="coin.jpg",
            content_type="image/jpeg",
            size_bytes=12345,
            s3_bucket="bucket",
            s3_key="media/coin.jpg",
            s3_url="https://example/media/coin.jpg",
            product_id=product.id,
            listing_id=listing.id,
            uploaded_by="qa-user",
        )
        self.assertEqual(media.product_id, product.id)
        all_rows = self.repo.list_media_assets()
        self.assertTrue(any(r.id == media.id for r in all_rows))
        self.assertEqual(len(self.repo.list_media_assets_for_product(product.id)), 1)
        self.assertEqual(len(self.repo.list_media_assets_for_listing(listing.id)), 1)

        updated = self.repo.update_media_asset(media.id, {"original_filename": "coin_new.jpg"}, actor="qa-user")
        self.assertEqual(updated.original_filename, "coin_new.jpg")

        unchanged = self.repo.update_media_asset(media.id, {"original_filename": "coin_new.jpg"}, actor="qa-user")
        self.assertEqual(unchanged.original_filename, "coin_new.jpg")

        with self.assertRaisesRegex(ValueError, "Media asset 999 not found"):
            self.repo.update_media_asset(999, {"original_filename": "x.jpg"}, actor="qa-user")

    def test_delete_media_asset_true_false_and_audit(self) -> None:
        product = self._create_product(sku="GS-MEDIA-DEL-001", qty=1)
        media = self.repo.create_media_asset(
            media_type="image",
            original_filename="delete_me.jpg",
            content_type="image/jpeg",
            size_bytes=10,
            s3_bucket="bucket",
            s3_key="media/delete_me.jpg",
            s3_url="https://example/media/delete_me.jpg",
            product_id=product.id,
            listing_id=None,
            uploaded_by="qa-user",
        )

        deleted = self.repo.delete_media_asset(media.id, actor="qa-user")
        self.assertTrue(deleted)
        self.assertIsNone(self.db.get(type(media), media.id))
        logs = self.repo.list_audit_logs(limit=100)
        row = next(
            (l for l in logs if l.entity_type == "media_asset" and l.entity_id == media.id and l.action == "delete"),
            None,
        )
        self.assertIsNotNone(row)
        payload = json.loads(row.changes_json or "{}")
        before = payload.get("before", {}) if isinstance(payload, dict) else {}
        self.assertEqual(before.get("filename"), "delete_me.jpg")
        self.assertEqual(before.get("s3_key"), "media/delete_me.jpg")

        missing = self.repo.delete_media_asset(999999, actor="qa-user")
        self.assertFalse(missing)

    def test_inventory_source_update_active_filter_and_not_found(self) -> None:
        source_a = self.repo.create_inventory_source(name="Source A", source_type="dealer", is_active=True)
        source_b = self.repo.create_inventory_source(name="Source B", source_type="dealer", is_active=False)
        active = self.repo.list_inventory_sources(active_only=True)
        self.assertEqual([s.id for s in active], [source_a.id])

        updated = self.repo.update_inventory_source(
            source_a.id,
            {"source_url": "https://dealer.example", "payment_method": "wire"},
            actor="qa-user",
        )
        self.assertEqual(updated.source_url, "https://dealer.example")
        self.assertEqual(updated.payment_method, "wire")

        with self.assertRaisesRegex(ValueError, "Inventory source 999 not found"):
            self.repo.update_inventory_source(999, {"name": "Missing"}, actor="qa-user")
        self.assertFalse(any(s.id == source_b.id for s in self.repo.list_inventory_sources(active_only=True)))

    def test_assign_product_to_lot_sets_allocated_cost(self) -> None:
        product = self._create_product(sku="GS-LOT-ASSIGN-1", qty=1)
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-ALLOC-1",
            vendor="Dealer",
            purchase_date=datetime(2026, 3, 26, 8, 0, 0),
            total_cost=Decimal("200.00"),
            notes="alloc test",
        )
        assignment = self.repo.assign_product_to_lot(
            product_id=product.id,
            lot_id=lot.id,
            quantity_acquired=3,
            unit_cost=Decimal("10.50"),
            acquired_at=datetime(2026, 3, 26, 9, 0, 0),
        )
        self.assertEqual(assignment.allocated_cost, Decimal("31.50"))
        self.assertTrue(any(a.id == assignment.id for a in self.repo.list_product_lot_assignments()))

    def test_record_audit_event_and_entity_filter(self) -> None:
        row = self.repo.record_audit_event(
            entity_type=" Product ",
            entity_id=12,
            action=" ",
            actor="qa-user",
            changes={"key": "value"},
        )
        self.assertEqual(row.entity_type, "product")
        self.assertEqual(row.action, "note")
        filtered = self.repo.list_audit_logs_for_entity(entity_type="product", entity_id=12, limit=5)
        self.assertTrue(any(r.id == row.id for r in filtered))

    def test_record_audit_event_raises_when_row_not_persisted(self) -> None:
        class _NoRow:
            def first(self):
                return None

        original_scalars = self.repo.db.scalars
        try:
            self.repo.db.scalars = lambda *_args, **_kwargs: _NoRow()
            with self.assertRaisesRegex(RuntimeError, "Failed to persist audit event"):
                self.repo.record_audit_event(
                    entity_type="product",
                    entity_id=1,
                    action="note",
                    actor="qa-user",
                    changes={"x": 1},
                )
        finally:
            self.repo.db.scalars = original_scalars

    def test_update_app_user_set_password_and_not_found_paths(self) -> None:
        user = self.repo.upsert_app_user(
            username="david",
            role="viewer",
            display_name="David",
            email="david@example.com",
            password="Start123",
            is_active=True,
            actor="qa-user",
        )
        changed = self.repo.update_app_user(user.id, {"role": "ops", "display_name": "David Ops"}, actor="qa-user")
        self.assertEqual(changed.role, "ops")
        unchanged = self.repo.update_app_user(user.id, {"role": "ops"}, actor="qa-user")
        self.assertEqual(unchanged.role, "ops")

        pw_row = self.repo.set_app_user_password(user.id, "NewStrong123", actor="qa-user")
        self.assertTrue(bool(pw_row.password_hash))
        self.assertIsNotNone(pw_row.password_updated_at)

        with self.assertRaisesRegex(ValueError, "App user 999 not found"):
            self.repo.update_app_user(999, {"role": "ops"}, actor="qa-user")
        with self.assertRaisesRegex(ValueError, "App user 999 not found"):
            self.repo.set_app_user_password(999, "x", actor="qa-user")

    def test_set_role_permissions_requires_role_and_noop(self) -> None:
        with self.assertRaisesRegex(ValueError, "Role is required"):
            self.repo.set_role_permissions("", {"read"}, actor="qa-user")

        self.repo.set_role_permissions("ops", {"read", "update"}, actor="qa-user")
        before = self.repo.list_role_permissions().get("ops")
        self.repo.set_role_permissions("ops", {"read", "update"}, actor="qa-user")
        after = self.repo.list_role_permissions().get("ops")
        self.assertEqual(before, after)

    def test_role_permission_matrix(self) -> None:
        self.assertTrue(has_permission("admin", "manage_settings"))
        self.assertTrue(has_permission("ops", "bulk_update"))
        self.assertFalse(has_permission("viewer", "update"))

    def test_upsert_app_user_create_then_update(self) -> None:
        created = self.repo.upsert_app_user(
            username="alice",
            role="ops",
            display_name="Alice",
            email="alice@example.com",
            password="StrongPass123",
            is_active=True,
            actor="qa-user",
        )
        self.assertEqual(created.username, "alice")
        self.assertEqual(created.role, "ops")

        updated = self.repo.upsert_app_user(
            username="alice",
            role="admin",
            display_name="Alice Admin",
            email="alice.admin@example.com",
            is_active=True,
            actor="qa-user",
        )
        self.assertEqual(updated.id, created.id)
        self.assertEqual(updated.role, "admin")
        self.assertEqual(updated.display_name, "Alice Admin")

    def test_upsert_app_user_requires_password_for_new_user(self) -> None:
        with self.assertRaisesRegex(ValueError, "Password is required"):
            self.repo.upsert_app_user(
                username="charlie",
                role="ops",
                display_name="Charlie",
                email="charlie@example.com",
                password="",
                is_active=True,
                actor="qa-user",
            )

    def test_set_role_permissions_replaces_role_matrix(self) -> None:
        self.repo.set_role_permissions("ops", {"read", "update", "bulk_update"}, actor="qa-user")
        matrix = self.repo.list_role_permissions()
        self.assertEqual(matrix.get("ops"), {"read", "update", "bulk_update"})

        self.repo.set_role_permissions("ops", {"read", "export"}, actor="qa-user")
        matrix2 = self.repo.list_role_permissions()
        self.assertEqual(matrix2.get("ops"), {"read", "export"})

    def test_authenticate_app_user_with_password(self) -> None:
        row = self.repo.upsert_app_user(
            username="bob",
            role="ops",
            display_name="Bob",
            email="bob@example.com",
            password="StrongPass123",
            is_active=True,
            actor="qa-user",
        )
        self.assertTrue(bool(row.password_hash))
        self.assertIsNotNone(self.repo.authenticate_app_user("bob", "StrongPass123"))
        self.assertIsNone(self.repo.authenticate_app_user("bob", "wrong-password"))

    def test_authenticate_app_user_blank_and_inactive_paths(self) -> None:
        self.assertIsNone(self.repo.authenticate_app_user("", "x"))
        self.repo.upsert_app_user(
            username="inactive-user",
            role="viewer",
            display_name="Inactive",
            email="inactive@example.com",
            password="StrongPass123",
            is_active=False,
            actor="qa-user",
        )
        self.assertIsNone(self.repo.authenticate_app_user("inactive-user", "StrongPass123"))

    def test_list_app_users_active_only_filter(self) -> None:
        self.repo.upsert_app_user(
            username="active-u",
            role="viewer",
            display_name="Active",
            email="a@example.com",
            password="StrongPass123",
            is_active=True,
            actor="qa-user",
        )
        self.repo.upsert_app_user(
            username="inactive-u",
            role="viewer",
            display_name="Inactive",
            email="i@example.com",
            password="StrongPass123",
            is_active=False,
            actor="qa-user",
        )
        all_rows = self.repo.list_app_users(active_only=False)
        active_rows = self.repo.list_app_users(active_only=True)
        all_usernames = {r.username for r in all_rows}
        active_usernames = {r.username for r in active_rows}
        self.assertIn("active-u", all_usernames)
        self.assertIn("inactive-u", all_usernames)
        self.assertIn("active-u", active_usernames)
        self.assertNotIn("inactive-u", active_usernames)

    def test_sync_run_event_error_lifecycle(self) -> None:
        run = self.repo.create_sync_run(
            provider="ebay",
            job_name="ebay_orders_pull",
            direction="pull",
            status="running",
            notes="manual test run",
            actor="qa-user",
        )
        self.assertEqual(run.provider, "ebay")
        self.assertEqual(run.status, "running")

        ev = self.repo.add_sync_event(
            sync_run_id=run.id,
            entity_type="order",
            entity_id="EBAY-1001",
            action="upsert",
            status="ok",
            message="created local order",
        )
        er = self.repo.add_sync_error(
            sync_run_id=run.id,
            code="RATE_LIMIT",
            message="retry later",
            severity="warning",
        )
        self.assertEqual(ev.sync_run_id, run.id)
        self.assertEqual(er.sync_run_id, run.id)

        updated = self.repo.update_sync_run(
            run.id,
            {
                "status": "partial",
                "records_processed": 12,
                "records_created": 8,
                "records_updated": 3,
                "records_failed": 1,
            },
            actor="qa-user",
        )
        self.assertEqual(updated.status, "partial")
        self.assertEqual(updated.records_failed, 1)

        runs = self.repo.list_sync_runs(provider="ebay")
        self.assertTrue(any(r.id == run.id for r in runs))
        self.assertGreaterEqual(len(self.repo.list_sync_events(run.id)), 1)
        self.assertGreaterEqual(len(self.repo.list_sync_errors(run.id)), 1)

    def test_retry_sync_run_creates_linked_retry(self) -> None:
        failed_run = self.repo.create_sync_run(
            provider="ebay",
            job_name="ebay_orders_pull_import",
            direction="pull",
            status="failed",
            notes="original failed run",
            actor="qa-user",
        )
        retry = self.repo.retry_sync_run(failed_run.id, actor="qa-user")
        self.assertEqual(retry.retry_of_run_id, failed_run.id)
        self.assertEqual(retry.retry_count, 1)
        self.assertEqual(retry.status, "queued")
        self.assertEqual(retry.provider, failed_run.provider)
        self.assertEqual(retry.job_name, failed_run.job_name)

    def test_retry_sync_run_rejects_non_failed_status(self) -> None:
        success_run = self.repo.create_sync_run(
            provider="ebay",
            job_name="ebay_orders_pull_import",
            direction="pull",
            status="success",
            notes="completed run",
            actor="qa-user",
        )
        with self.assertRaisesRegex(ValueError, "failed/partial"):
            self.repo.retry_sync_run(success_run.id, actor="qa-user")

    def test_retry_sync_run_not_found_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "Sync run 999999 not found"):
            self.repo.retry_sync_run(999999, actor="qa-user")

    def test_update_sync_run_not_found_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "Sync run 999 not found"):
            self.repo.update_sync_run(999, {"status": "failed"}, actor="qa-user")

    def test_update_sync_run_noop_and_unknown_fields(self) -> None:
        run = self.repo.create_sync_run(
            provider="ebay",
            job_name="ebay_orders_pull_import",
            status="queued",
            actor="qa-user",
        )
        before = [
            row
            for row in self.repo.list_audit_logs(limit=200)
            if row.entity_type == "sync_run" and row.entity_id == run.id and row.action == "update"
        ]
        updated = self.repo.update_sync_run(
            run.id,
            {"unknown_field": "ignored", "status": run.status},
            actor="qa-user",
        )
        self.assertEqual(updated.id, run.id)
        after = [
            row
            for row in self.repo.list_audit_logs(limit=200)
            if row.entity_type == "sync_run" and row.entity_id == run.id and row.action == "update"
        ]
        self.assertEqual(len(after), len(before))

    def test_list_sync_runs_without_provider_filter(self) -> None:
        self.repo.create_sync_run(provider="ebay", job_name="job1", status="queued", actor="qa-user")
        self.repo.create_sync_run(provider="google", job_name="job2", status="queued", actor="qa-user")
        rows = self.repo.list_sync_runs(limit=50)
        providers = {r.provider for r in rows}
        self.assertIn("ebay", providers)
        self.assertIn("google", providers)

    def test_integration_automation_rule_crud_and_validation(self) -> None:
        with self.assertRaisesRegex(ValueError, "Integration is required"):
            self.repo.create_integration_automation_rule(
                environment="local",
                integration="",
                action="post_message",
                name="rule",
                trigger_status="queued",
                conditions_json="{}",
                effect_json="{}",
                actor="qa-user",
            )
        with self.assertRaisesRegex(ValueError, "Action is required"):
            self.repo.create_integration_automation_rule(
                environment="local",
                integration="slack",
                action="",
                name="rule",
                trigger_status="queued",
                conditions_json="{}",
                effect_json="{}",
                actor="qa-user",
            )
        with self.assertRaisesRegex(ValueError, "Rule name is required"):
            self.repo.create_integration_automation_rule(
                environment="local",
                integration="slack",
                action="post_message",
                name="",
                trigger_status="queued",
                conditions_json="{}",
                effect_json="{}",
                actor="qa-user",
            )
        with self.assertRaisesRegex(ValueError, "Rule JSON must be valid JSON"):
            self.repo.create_integration_automation_rule(
                environment="local",
                integration="slack",
                action="post_message",
                name="rule",
                trigger_status="queued",
                conditions_json="{bad",
                effect_json="{}",
                actor="qa-user",
            )

        rule = self.repo.create_integration_automation_rule(
            environment="local",
            integration="slack",
            action="post_message",
            name="slack queue rule",
            trigger_status="queued",
            conditions_json='{"all":[{"field":"status","op":"eq","value":"queued"}]}',
            effect_json='{"set":{"status":"queued"}}',
            requires_approval=True,
            is_active=True,
            actor="qa-user",
        )
        rows = self.repo.list_integration_automation_rules(
            environment="local",
            integration="slack",
            action="post_message",
            active_only=True,
            limit=20,
        )
        self.assertTrue(any(r.id == rule.id for r in rows))

        updated = self.repo.update_integration_automation_rule(
            rule.id,
            {"name": "slack queue rule v2", "is_active": False},
            actor="qa-user",
        )
        self.assertEqual(updated.name, "slack queue rule v2")
        self.assertFalse(updated.is_active)
        unchanged = self.repo.update_integration_automation_rule(rule.id, {"name": "slack queue rule v2"}, actor="qa-user")
        self.assertEqual(unchanged.name, "slack queue rule v2")

        with self.assertRaisesRegex(ValueError, "Integration automation rule 999 not found"):
            self.repo.update_integration_automation_rule(999, {"name": "x"}, actor="qa-user")
        with self.assertRaisesRegex(ValueError, "conditions_json must be valid JSON"):
            self.repo.update_integration_automation_rule(rule.id, {"conditions_json": "{bad"}, actor="qa-user")
        with self.assertRaisesRegex(ValueError, "effect_json must be valid JSON"):
            self.repo.update_integration_automation_rule(rule.id, {"effect_json": "{bad"}, actor="qa-user")

        self.assertTrue(self.repo.delete_integration_automation_rule(rule_id=rule.id, actor="qa-user"))
        self.assertFalse(self.repo.delete_integration_automation_rule(rule_id=rule.id, actor="qa-user"))

    def test_integration_automation_approval_and_queue_job_crud(self) -> None:
        rule = self.repo.create_integration_automation_rule(
            environment="local",
            integration="google",
            action="gmail_send_document_email",
            name="gmail approval rule",
            trigger_status="queued",
            conditions_json="{}",
            effect_json="{}",
            requires_approval=True,
            is_active=True,
            actor="qa-user",
        )
        job = self.repo.create_integration_queue_job(
            environment="local",
            integration="google",
            action="gmail_send_document_email",
            payload_json='{"to":"x@y.com"}',
            requested_by="qa-user",
            max_retries=2,
            actor="qa-user",
        )
        listed_jobs = self.repo.list_integration_queue_jobs(
            environment="local",
            integration="google",
            statuses={"queued"},
            limit=20,
        )
        self.assertTrue(any(r.id == job.id for r in listed_jobs))

        updated_job = self.repo.update_integration_queue_job(
            job.id,
            {"status": "running", "last_error": "temp"},
            actor="qa-user",
        )
        self.assertEqual(updated_job.status, "running")
        with self.assertRaisesRegex(ValueError, "Integration queue job 999 not found"):
            self.repo.update_integration_queue_job(999, {"status": "failed"}, actor="qa-user")

        with self.assertRaisesRegex(ValueError, "Integration automation rule 999 not found"):
            self.repo.create_integration_automation_approval(
                environment="local",
                rule_id=999,
                queue_job_id=None,
                actor="qa-user",
            )
        with self.assertRaisesRegex(ValueError, "Integration queue job 999 not found"):
            self.repo.create_integration_automation_approval(
                environment="local",
                rule_id=rule.id,
                queue_job_id=999,
                actor="qa-user",
            )

        approval = self.repo.create_integration_automation_approval(
            environment="local",
            rule_id=rule.id,
            queue_job_id=job.id,
            notes="approved",
            approved_by="qa-user",
            approved_at=datetime(2026, 3, 29, 10, 0, 0),
            expires_at=datetime(2026, 4, 29, 10, 0, 0),
            actor="qa-user",
        )
        approvals = self.repo.list_integration_automation_approvals(
            environment="local",
            rule_id=rule.id,
            queue_job_id=job.id,
            active_only=True,
            limit=20,
        )
        self.assertTrue(any(a.id == approval.id for a in approvals))
        self.assertTrue(
            self.repo.has_active_integration_automation_approval(
                environment="local",
                rule_id=rule.id,
                queue_job_id=job.id,
                as_of=datetime(2026, 3, 30, 10, 0, 0),
            )
        )
        self.assertFalse(
            self.repo.has_active_integration_automation_approval(
                environment="local",
                rule_id=rule.id,
                queue_job_id=job.id,
                as_of=datetime(2026, 5, 30, 10, 0, 0),
            )
        )

        revoked = self.repo.revoke_integration_automation_approval(approval_id=approval.id, actor="qa-user")
        self.assertFalse(revoked.is_active)
        self.assertEqual(revoked.status, "revoked")
        with self.assertRaisesRegex(ValueError, "Integration automation approval 999 not found"):
            self.repo.revoke_integration_automation_approval(approval_id=999, actor="qa-user")

    def test_integration_automation_rule_list_and_update_noop_paths(self) -> None:
        active_rule = self.repo.create_integration_automation_rule(
            environment="local",
            integration="slack",
            action="post_message",
            name="active rule",
            trigger_status="queued",
            conditions_json="{}",
            effect_json="{}",
            is_active=True,
            actor="qa-user",
        )
        self.repo.create_integration_automation_rule(
            environment="local",
            integration="slack",
            action="post_message",
            name="inactive rule",
            trigger_status="queued",
            conditions_json="{}",
            effect_json="{}",
            is_active=False,
            actor="qa-user",
        )
        all_rows = self.repo.list_integration_automation_rules(environment="local", active_only=False, limit=50)
        active_rows = self.repo.list_integration_automation_rules(environment="local", active_only=True, limit=50)
        self.assertGreaterEqual(len(all_rows), 2)
        self.assertTrue(any(r.id == active_rule.id for r in active_rows))

        before_updates = [
            row
            for row in self.repo.list_audit_logs(limit=200)
            if row.entity_type == "integration_automation_rule" and row.entity_id == active_rule.id and row.action == "update"
        ]
        unchanged = self.repo.update_integration_automation_rule(
            active_rule.id,
            {"unknown_field": "ignored", "name": active_rule.name},
            actor="qa-user",
        )
        self.assertEqual(unchanged.id, active_rule.id)
        after_updates = [
            row
            for row in self.repo.list_audit_logs(limit=200)
            if row.entity_type == "integration_automation_rule" and row.entity_id == active_rule.id and row.action == "update"
        ]
        self.assertEqual(len(after_updates), len(before_updates))

    def test_integration_approval_queue_optional_and_double_revoke(self) -> None:
        rule = self.repo.create_integration_automation_rule(
            environment="local",
            integration="google",
            action="gmail_send_document_email",
            name="queue optional approval",
            trigger_status="queued",
            conditions_json="{}",
            effect_json="{}",
            actor="qa-user",
        )
        approval = self.repo.create_integration_automation_approval(
            environment="local",
            rule_id=rule.id,
            queue_job_id=None,
            notes="approved without queue id",
            actor="qa-user",
        )
        # queue_job_id=None should still satisfy checks when no queue job is requested
        self.assertTrue(
            self.repo.has_active_integration_automation_approval(
                environment="local",
                rule_id=rule.id,
                queue_job_id=None,
            )
        )
        # queue_job_id-specific check accepts global approvals (queue_job_id is null)
        self.assertTrue(
            self.repo.has_active_integration_automation_approval(
                environment="local",
                rule_id=rule.id,
                queue_job_id=12345,
            )
        )
        first = self.repo.revoke_integration_automation_approval(approval_id=approval.id, actor="qa-user")
        self.assertFalse(first.is_active)
        second = self.repo.revoke_integration_automation_approval(approval_id=approval.id, actor="qa-user")
        self.assertEqual(second.status, "revoked")
        self.assertFalse(second.is_active)

    def test_integration_queue_list_empty_status_filter_and_noop_update(self) -> None:
        job = self.repo.create_integration_queue_job(
            environment="local",
            integration="google",
            action="gmail_send_document_email",
            payload_json="{}",
            requested_by="qa-user",
            actor="qa-user",
        )
        # statuses with empty tokens should behave like no status filter
        listed = self.repo.list_integration_queue_jobs(
            environment="local",
            integration=None,
            statuses={"", "   "},
            limit=20,
        )
        self.assertTrue(any(r.id == job.id for r in listed))

        before_updates = [
            row
            for row in self.repo.list_audit_logs(limit=200)
            if row.entity_type == "integration_queue_job" and row.entity_id == job.id and row.action == "update"
        ]
        unchanged = self.repo.update_integration_queue_job(
            job.id,
            {"unknown_field": "ignored", "status": job.status},
            actor="qa-user",
        )
        self.assertEqual(unchanged.id, job.id)
        after_updates = [
            row
            for row in self.repo.list_audit_logs(limit=200)
            if row.entity_type == "integration_queue_job" and row.entity_id == job.id and row.action == "update"
        ]
        self.assertEqual(len(after_updates), len(before_updates))

    def test_integration_approval_and_queue_list_default_filter_branches(self) -> None:
        rule = self.repo.create_integration_automation_rule(
            environment="local",
            integration="google",
            action="gmail_send_document_email",
            name="branch coverage rule",
            trigger_status="queued",
            conditions_json="{}",
            effect_json="{}",
            actor="qa-user",
        )
        job = self.repo.create_integration_queue_job(
            environment="local",
            integration="google",
            action="gmail_send_document_email",
            payload_json="{}",
            requested_by="qa-user",
            actor="qa-user",
        )
        approval = self.repo.create_integration_automation_approval(
            environment="local",
            rule_id=rule.id,
            queue_job_id=job.id,
            actor="qa-user",
        )
        self.assertIsNotNone(approval.id)

        # rule_id/queue_job_id omitted => branch where filters are not applied.
        approvals = self.repo.list_integration_automation_approvals(
            environment="local",
            rule_id=None,
            queue_job_id=None,
            active_only=False,
            limit=50,
        )
        self.assertTrue(any(a.id == approval.id for a in approvals))

        # unresolved_only=False and provider omitted => both conditional filters skipped.
        run = self.repo.create_sync_run(provider="ebay", job_name="orders", status="failed", actor="qa-user")
        err = self.repo.add_sync_error(sync_run_id=run.id, code="E", message="m", severity="error")
        self.repo.resolve_sync_error(err.id, actor="qa-user")
        queue = self.repo.list_sync_error_queue(
            provider=None,
            unresolved_only=False,
            limit=50,
        )
        self.assertTrue(any(row[0].id == err.id for row in queue))

        # statuses omitted => list_integration_queue_jobs should skip status filtering branch.
        jobs = self.repo.list_integration_queue_jobs(
            environment="local",
            integration=None,
            statuses=None,
            limit=50,
        )
        self.assertTrue(any(j.id == job.id for j in jobs))

    def test_sync_event_error_queue_and_resolution_paths(self) -> None:
        run = self.repo.create_sync_run(
            provider="ebay",
            job_name="ebay_orders_pull_import",
            direction="pull",
            status="running",
            actor="qa-user",
        )
        event = self.repo.add_sync_event(
            sync_run_id=run.id,
            entity_type="order",
            entity_id="ORD-1",
            action="upsert",
            status="ok",
            message="ok",
            payload_json="{}",
        )
        err = self.repo.add_sync_error(
            sync_run_id=run.id,
            code="E1",
            message="bad thing",
            severity="error",
            context_json="{}",
        )
        self.assertTrue(any(r.id == event.id for r in self.repo.list_sync_events(run.id, limit=50)))
        self.assertTrue(any(r.id == err.id for r in self.repo.list_sync_errors(run.id, limit=50)))

        queue_rows = self.repo.list_sync_error_queue(provider="ebay", unresolved_only=True, limit=50)
        self.assertTrue(any(e.id == err.id for e, _r in queue_rows))
        resolved = self.repo.resolve_sync_error(err.id, actor="qa-user", resolved_at=datetime(2026, 3, 30, 9, 0, 0))
        self.assertIsNotNone(resolved.resolved_at)
        resolved_again = self.repo.resolve_sync_error(err.id, actor="qa-user")
        self.assertEqual(resolved_again.id, err.id)
        with self.assertRaisesRegex(ValueError, "Sync error 999 not found"):
            self.repo.resolve_sync_error(999, actor="qa-user")

        entity_events = self.repo.list_sync_events_for_entity(entity_type="order", entity_id="ORD-1", limit=50)
        self.assertTrue(any(r.id == event.id for r in entity_events))

    def test_ai_provider_config_crud_defaults_and_validation(self) -> None:
        with self.assertRaisesRegex(ValueError, "Profile name is required"):
            self.repo.upsert_ai_provider_config(
                environment="local",
                name="",
                provider="openai",
                model="gpt-5",
                multimodal_model="",
                base_url="https://api.openai.com/v1",
                endpoint_type="responses",
                api_key="k",
                temperature=Decimal("0.2"),
                max_output_tokens=1024,
                timeout_seconds=30,
                actor="qa-user",
            )
        with self.assertRaisesRegex(ValueError, "Provider must be"):
            self.repo.upsert_ai_provider_config(
                environment="local",
                name="bad-provider",
                provider="invalid",
                model="gpt-5",
                multimodal_model="",
                base_url="https://api.openai.com/v1",
                endpoint_type="responses",
                api_key="k",
                temperature=Decimal("0.2"),
                max_output_tokens=1024,
                timeout_seconds=30,
                actor="qa-user",
            )
        with self.assertRaisesRegex(ValueError, "Endpoint type must be"):
            self.repo.upsert_ai_provider_config(
                environment="local",
                name="bad-endpoint",
                provider="openai",
                model="gpt-5",
                multimodal_model="",
                base_url="https://api.openai.com/v1",
                endpoint_type="bad",
                api_key="k",
                temperature=Decimal("0.2"),
                max_output_tokens=1024,
                timeout_seconds=30,
                actor="qa-user",
            )

        first = self.repo.upsert_ai_provider_config(
            environment="local",
            name="primary",
            provider="openai",
            model="gpt-5",
            multimodal_model="",
            base_url="https://api.openai.com/v1",
            endpoint_type="responses",
            api_key="k1",
            temperature=Decimal("0.2"),
            max_output_tokens=1024,
            timeout_seconds=30,
            is_default=True,
            actor="qa-user",
        )
        second = self.repo.upsert_ai_provider_config(
            environment="local",
            name="backup",
            provider="localai",
            model="llama",
            multimodal_model="llava",
            base_url="http://localai:8080/v1",
            endpoint_type="chat_completions",
            api_key="",
            temperature=Decimal("0.1"),
            max_output_tokens=2048,
            timeout_seconds=45,
            is_default=True,
            actor="qa-user",
        )
        rows = self.repo.list_ai_provider_configs(environment="local", active_only=False)
        self.assertEqual(rows[0].id, second.id)
        first_row = next(r for r in rows if r.id == first.id)
        self.assertFalse(first_row.is_default)
        default_row = self.repo.get_default_ai_provider_config(environment="local")
        self.assertIsNotNone(default_row)
        self.assertEqual(default_row.id, second.id)

        updated = self.repo.upsert_ai_provider_config(
            environment="local",
            name="backup",
            provider="localai",
            model="llama-2",
            multimodal_model="",
            base_url="http://localai:8080/v1/",
            endpoint_type="responses",
            api_key="",
            temperature=Decimal("0.3"),
            max_output_tokens=1536,
            timeout_seconds=60,
            is_default=False,
            is_active=True,
            actor="qa-user",
        )
        self.assertEqual(updated.model, "llama-2")
        self.assertEqual(updated.multimodal_model, "llama-2")
        self.assertEqual(updated.base_url, "http://localai:8080/v1")

        direct_updated = self.repo.update_ai_provider_config(
            second.id,
            {"is_default": True, "notes": "promoted"},
            actor="qa-user",
        )
        self.assertTrue(direct_updated.is_default)
        with self.assertRaisesRegex(ValueError, "AI provider config 999 not found"):
            self.repo.update_ai_provider_config(999, {"is_active": False}, actor="qa-user")

        self.assertTrue(self.repo.delete_ai_provider_config_by_id(config_id=first.id, actor="qa-user"))
        self.assertFalse(self.repo.delete_ai_provider_config_by_id(config_id=first.id, actor="qa-user"))

    def test_runtime_settings_list_get_delete_and_validation(self) -> None:
        with self.assertRaisesRegex(ValueError, "Setting key is required"):
            self.repo.upsert_runtime_setting(
                environment="local",
                key="",
                value="x",
                value_type="str",
                actor="qa-user",
            )
        with self.assertRaisesRegex(ValueError, "Value type must be one of"):
            self.repo.upsert_runtime_setting(
                environment="local",
                key="bad_type",
                value="x",
                value_type="yaml",
                actor="qa-user",
            )

        row = self.repo.upsert_runtime_setting(
            environment="local",
            key="feature_flag",
            value="true",
            value_type="bool",
            description="flag",
            is_active=True,
            actor="qa-user",
        )
        self.repo.upsert_runtime_setting(
            environment="local",
            key="disabled_flag",
            value="true",
            value_type="bool",
            description="disabled",
            is_active=False,
            actor="qa-user",
        )
        active_rows = self.repo.list_runtime_settings(environment="local", active_only=True)
        self.assertTrue(any(r.id == row.id for r in active_rows))
        self.assertFalse(any(r.key == "disabled_flag" for r in active_rows))
        self.assertIsNotNone(self.repo.get_runtime_setting(environment="local", key="feature_flag", active_only=True))
        self.assertIsNone(self.repo.get_runtime_setting(environment="local", key="disabled_flag", active_only=True))

        self.assertTrue(self.repo.delete_runtime_setting_by_id(setting_id=row.id, actor="qa-user"))
        self.assertFalse(self.repo.delete_runtime_setting_by_id(setting_id=row.id, actor="qa-user"))

    def test_saved_filter_profiles_admin_paths_defaults_and_transfer(self) -> None:
        user_default = self.repo.upsert_saved_filter_profile(
            environment="local",
            username="ops1",
            scope="products",
            name="Default Mine",
            filter_json='{"q":"gold"}',
            is_shared=False,
            is_default=True,
            actor="qa-user",
        )
        user_other = self.repo.upsert_saved_filter_profile(
            environment="local",
            username="ops1",
            scope="products",
            name="My Other",
            filter_json='{"q":"silver"}',
            is_shared=False,
            is_default=True,
            actor="qa-user",
        )
        self.assertFalse(self.db.get(type(user_default), user_default.id).is_default)
        self.assertTrue(self.db.get(type(user_other), user_other.id).is_default)

        shared_a = self.repo.upsert_saved_filter_profile(
            environment="local",
            username="ops1",
            scope="products",
            name="Shared A",
            filter_json="{}",
            is_shared=True,
            is_default=True,
            actor="qa-user",
        )
        shared_b = self.repo.upsert_saved_filter_profile(
            environment="local",
            username="ops2",
            scope="products",
            name="Shared B",
            filter_json="{}",
            is_shared=True,
            is_default=True,
            actor="qa-user",
        )
        _conflict_row = self.repo.upsert_saved_filter_profile(
            environment="local",
            username="ops1",
            scope="products",
            name="Shared B",
            filter_json="{}",
            is_shared=False,
            is_default=False,
            actor="qa-user",
        )
        self.assertFalse(self.db.get(type(shared_a), shared_a.id).is_default)
        self.assertTrue(self.db.get(type(shared_b), shared_b.id).is_default)

        visible_to_ops1 = self.repo.list_saved_filter_profiles(
            environment="local",
            scope="products",
            username="ops1",
            include_shared=True,
            active_only=True,
        )
        self.assertTrue(any(r.id == shared_b.id for r in visible_to_ops1))

        with self.assertRaisesRegex(ValueError, "Ownership transfer is only supported for shared filters"):
            self.repo.transfer_shared_filter_ownership(
                profile_id=user_other.id,
                new_username="ops2",
                actor="qa-user",
            )
        with self.assertRaisesRegex(ValueError, "New owner username is required"):
            self.repo.transfer_shared_filter_ownership(
                profile_id=shared_b.id,
                new_username="",
                actor="qa-user",
            )
        with self.assertRaisesRegex(ValueError, "same environment/scope/name"):
            self.repo.transfer_shared_filter_ownership(
                profile_id=shared_b.id,
                new_username="ops1",
                actor="qa-user",
            )
        with self.assertRaisesRegex(ValueError, "Saved filter profile 999 not found"):
            self.repo.transfer_shared_filter_ownership(
                profile_id=999,
                new_username="ops3",
                actor="qa-user",
            )

        self.assertTrue(self.repo.delete_saved_filter_profile(
            environment="local",
            username="ops1",
            scope="products",
            name="My Other",
            actor="qa-user",
        ))
        self.assertFalse(self.repo.delete_saved_filter_profile(
            environment="local",
            username="ops1",
            scope="products",
            name="My Other",
            actor="qa-user",
        ))
        with self.assertRaisesRegex(ValueError, "only for shared filters"):
            self.repo.delete_shared_filter_profile_by_id(profile_id=user_default.id, actor="qa-user")
        self.assertTrue(self.repo.delete_shared_filter_profile_by_id(profile_id=shared_b.id, actor="qa-user"))
        self.assertFalse(self.repo.delete_saved_filter_profile_by_id(profile_id=shared_b.id, actor="qa-user"))

    def test_ebay_listing_template_profile_upsert_list_update_and_validation(self) -> None:
        with self.assertRaisesRegex(ValueError, "Username is required"):
            self.repo.upsert_ebay_listing_template_profile(
                environment="local",
                username="",
                name="Template",
                actor="qa-user",
            )
        with self.assertRaisesRegex(ValueError, "Template name is required"):
            self.repo.upsert_ebay_listing_template_profile(
                environment="local",
                username="ops",
                name="",
                actor="qa-user",
            )

        personal_default = self.repo.upsert_ebay_listing_template_profile(
            environment="local",
            username="ops",
            name="Coins",
            listing_title_template="{title}",
            listing_price_default=Decimal("25.00"),
            quantity_default=1,
            is_default=True,
            is_shared=False,
            actor="qa-user",
        )
        replacement_default = self.repo.upsert_ebay_listing_template_profile(
            environment="local",
            username="ops",
            name="Bullion",
            listing_title_template="{title}",
            listing_price_default=Decimal("30.00"),
            quantity_default=2,
            is_default=True,
            is_shared=False,
            actor="qa-user",
        )
        shared_other = self.repo.upsert_ebay_listing_template_profile(
            environment="local",
            username="ops2",
            name="Shared Team",
            listing_title_template="{title}",
            listing_price_default=Decimal("35.00"),
            quantity_default=1,
            is_default=False,
            is_shared=True,
            actor="qa-user",
        )
        self.assertFalse(self.db.get(type(personal_default), personal_default.id).is_default)
        self.assertTrue(self.db.get(type(replacement_default), replacement_default.id).is_default)

        visible = self.repo.list_ebay_listing_template_profiles(
            environment="local",
            username="ops",
            include_shared=True,
            active_only=True,
        )
        self.assertTrue(any(r.id == shared_other.id for r in visible))

        updated = self.repo.update_ebay_listing_template_profile(
            replacement_default.id,
            {"listing_status_default": "active", "quantity_default": 3},
            actor="qa-user",
        )
        self.assertEqual(updated.quantity_default, 3)
        with self.assertRaisesRegex(ValueError, "eBay listing template 999 not found"):
            self.repo.update_ebay_listing_template_profile(999, {"name": "x"}, actor="qa-user")
        with self.assertRaises(ValueError):
            self.repo.update_ebay_listing_template_profile(
                replacement_default.id,
                {"quantity_default": 0},
                actor="qa-user",
            )

    def test_ebay_listing_template_profile_upsert_existing_row_updates_fields(self) -> None:
        created = self.repo.upsert_ebay_listing_template_profile(
            environment="local",
            username="ops-upsert",
            name="Template A",
            listing_title_template="{title}",
            marketplace_details_template="initial",
            listing_price_default=Decimal("12.00"),
            quantity_default=1,
            listing_status_default="draft",
            is_shared=False,
            is_default=False,
            is_active=True,
            actor="qa-user",
        )
        updated = self.repo.upsert_ebay_listing_template_profile(
            environment="local",
            username="ops-upsert",
            name="Template A",
            listing_title_template="{title} UPDATED",
            marketplace_details_template="updated",
            listing_price_default=Decimal("15.50"),
            quantity_default=3,
            listing_status_default="active",
            is_shared=True,
            is_default=True,
            is_active=False,
            actor="qa-user",
        )
        self.assertEqual(updated.id, created.id)
        self.assertEqual(str(updated.listing_title_template), "{title} UPDATED")
        self.assertEqual(str(updated.marketplace_details_template), "updated")
        self.assertEqual(float(updated.listing_price_default), 15.5)
        self.assertEqual(int(updated.quantity_default), 3)
        self.assertEqual(str(updated.listing_status_default), "active")
        self.assertTrue(bool(updated.is_shared))
        self.assertTrue(bool(updated.is_default))
        self.assertFalse(bool(updated.is_active))

    def test_document_template_profile_include_all_and_not_found_update(self) -> None:
        all_profile = self.repo.create_document_template_profile(
            environment="local",
            doc_type="all",
            name="All Default",
            template_name="Classic",
            accent_color="#111111",
            company_name="GoldenStackers",
            is_default=False,
            actor="qa-user",
        )
        invoice_profile = self.repo.create_document_template_profile(
            environment="local",
            doc_type="invoice",
            name="Invoice Default",
            template_name="Classic",
            accent_color="#222222",
            company_name="GoldenStackers",
            is_default=True,
            actor="qa-user",
        )
        with_all = self.repo.list_document_template_profiles(
            environment="local",
            doc_type="invoice",
            include_all_doc_type=True,
            active_only=False,
        )
        without_all = self.repo.list_document_template_profiles(
            environment="local",
            doc_type="invoice",
            include_all_doc_type=False,
            active_only=False,
        )
        self.assertTrue(any(r.id == all_profile.id for r in with_all))
        self.assertTrue(any(r.id == invoice_profile.id for r in with_all))
        self.assertFalse(any(r.id == all_profile.id for r in without_all))
        self.assertTrue(any(r.id == invoice_profile.id for r in without_all))
        with self.assertRaisesRegex(ValueError, "Document template profile 999 not found"):
            self.repo.update_document_template_profile(999, {"name": "missing"}, actor="qa-user")

    def test_coin_ai_run_create_and_list_filters(self) -> None:
        p1 = self._create_product(sku="GS-AI-001", qty=1)
        run1 = self.repo.create_coin_ai_run(
            environment="local",
            tool_name="Coin_Grader",
            username="ops1",
            product_id=p1.id,
            input_hint="grade this",
            image_filename="coin1.jpg",
            result_markdown="ok",
            actor="qa-user",
        )
        run2 = self.repo.create_coin_ai_run(
            environment="local",
            tool_name="coin_identifier",
            username="ops2",
            product_id=None,
            listing_id=None,
            input_hint="identify this",
            image_filename="coin2.jpg",
            result_markdown="ok2",
            actor="qa-user",
        )
        self.assertEqual(run1.tool_name, "coin_grader")
        self.assertEqual(run2.tool_name, "coin_identifier")

        all_runs = self.repo.list_coin_ai_runs(limit=0)
        self.assertEqual(len(all_runs), 1)
        self.assertEqual(all_runs[0].id, run2.id)

        by_tool = self.repo.list_coin_ai_runs(tool_name="COIN_GRADER", limit=20)
        self.assertEqual([row.id for row in by_tool], [run1.id])
        by_user = self.repo.list_coin_ai_runs(username="ops2", limit=20)
        self.assertEqual([row.id for row in by_user], [run2.id])

    def test_document_artifact_create_list_and_content(self) -> None:
        art = self.repo.create_document_artifact(
            environment="local",
            source_type="sale",
            source_id=123,
            doc_type="invoice",
            document_number="INV-1001",
            artifact_kind="printable_html",
            file_name="inv-1001.html",
            mime_type="text/html",
            content_bytes=b"<html>ok</html>",
            actor="qa-user",
        )
        self.assertTrue(bool(art.storage_ref))
        self.assertEqual(self.repo.get_document_artifact_content(art.id), b"<html>ok</html>")

        filtered = self.repo.list_document_artifacts_for_source(
            source_type="sale",
            source_id=123,
            doc_type="invoice",
            limit=10,
        )
        self.assertEqual([row.id for row in filtered], [art.id])

        none_source = self.repo.create_document_artifact(
            environment="local",
            source_type="sale",
            source_id=None,
            doc_type="receipt",
            document_number="R-1",
            artifact_kind="printable_html",
            file_name="r1.html",
            mime_type="text/html",
            content_bytes=b"r1",
            actor="qa-user",
        )
        filtered_none = self.repo.list_document_artifacts_for_source(
            source_type="sale",
            source_id=None,
            doc_type="receipt",
            limit=10,
        )
        self.assertEqual([row.id for row in filtered_none], [none_source.id])

        blank = self.db.get(type(art), art.id)
        blank.content_base64 = ""
        self.db.commit()
        self.assertEqual(self.repo.get_document_artifact_content(art.id), b"")

        with self.assertRaisesRegex(ValueError, "not found"):
            self.repo.get_document_artifact_content(99999)
        with self.assertRaisesRegex(ValueError, "must be bytes"):
            self.repo.create_document_artifact(
                environment="local",
                source_type="sale",
                source_id=1,
                doc_type="invoice",
                document_number="INV-X",
                artifact_kind="printable_html",
                file_name="x.html",
                mime_type="text/html",
                content_bytes="not-bytes",  # type: ignore[arg-type]
                actor="qa-user",
            )
        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            self.repo.create_document_artifact(
                environment="local",
                source_type="sale",
                source_id=1,
                doc_type="invoice",
                document_number="INV-Y",
                artifact_kind="printable_html",
                file_name="y.html",
                mime_type="text/html",
                content_bytes=b"",
                actor="qa-user",
            )

    def test_purchase_document_create_list_update(self) -> None:
        p1 = self._create_product(sku="GS-PDOC-001", qty=2)
        doc = self.repo.create_purchase_document(
            document_kind="incoming_invoice",
            title="Supplier Invoice",
            original_filename="invoice.pdf",
            content_type="application/pdf",
            size_bytes=1234,
            content_sha256="ABCD1234",
            s3_bucket="media",
            s3_key="docs/invoice.pdf",
            s3_url="https://cdn.example.com/docs/invoice.pdf",
            product_id=p1.id,
            ai_extracted_json="",
            uploaded_by="ops1",
            actor="qa-user",
        )
        self.assertEqual(doc.ai_extracted_json, "{}")
        self.assertEqual(doc.content_sha256, "abcd1234")

        rows = self.repo.list_purchase_documents(limit=0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].id, doc.id)

        updated = self.repo.update_purchase_document(
            doc.id,
            {"title": "Updated Invoice", "ai_summary": "parsed"},
            actor="qa-user",
        )
        self.assertEqual(updated.title, "Updated Invoice")
        self.assertEqual(updated.ai_summary, "parsed")

        with self.assertRaisesRegex(ValueError, "Purchase document 999 not found"):
            self.repo.update_purchase_document(999, {"title": "x"}, actor="qa-user")

    def test_ai_chat_interaction_log_and_list_filters(self) -> None:
        self.repo.log_ai_chat_interaction(
            actor="ops1",
            prompt="What is spot price?",
            intent="pricing",
            allowed_domains=["example.com"],
            citations=[{"url": "https://example.com"}],
            answer_preview="preview",
            denied=False,
            elapsed_ms=120,
            metadata={"event_type": "chat", "goldy_mode": "assist"},
        )
        self.repo.log_ai_chat_interaction(
            actor="ops2",
            prompt="Do thing",
            intent="ops",
            allowed_domains=[],
            citations=[],
            answer_preview="preview2",
            denied=True,
            elapsed_ms=90,
            metadata={"event_type": "action", "goldy_mode": "agent"},
        )

        rows_all = self.repo.list_ai_chat_interactions(limit=10)
        self.assertEqual(len(rows_all), 2)
        rows_actor = self.repo.list_ai_chat_interactions(limit=10, actor="ops1")
        self.assertEqual(len(rows_actor), 1)
        self.assertEqual(rows_actor[0]["actor"], "ops1")
        rows_event = self.repo.list_ai_chat_interactions(limit=10, event_type="action")
        self.assertEqual(len(rows_event), 1)
        self.assertEqual(rows_event[0]["event_type"], "action")

        bad = AuditLog(
            entity_type="ai_chat",
            entity_id=None,
            action="query",
            actor="ops3",
            changes_json="{bad json",
        )
        self.db.add(bad)
        self.db.commit()
        rows_after_bad = self.repo.list_ai_chat_interactions(limit=10)
        self.assertEqual(len(rows_after_bad), 3)

    def test_dashboard_metrics_returns_counts_and_amounts(self) -> None:
        p1 = self._create_product(sku="GS-MET-001", qty=3)
        self.repo.create_listing(
            product_id=p1.id,
            marketplace="ebay",
            external_listing_id="",
            listing_title="T1",
            listing_price=Decimal("50.00"),
            listing_status="draft",
            quantity_listed=1,
            listed_at=datetime(2026, 3, 20, 10, 0, 0),
        )
        self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("120.00"),
            fees=Decimal("10.00"),
            shipping_cost=Decimal("5.00"),
            quantity_sold=1,
            product_id=p1.id,
            sold_at=datetime(2026, 3, 21, 12, 0, 0),
        )
        metrics = self.repo.dashboard_metrics()
        self.assertGreaterEqual(metrics["product_count"], 1)
        self.assertGreaterEqual(metrics["listing_count"], 1)
        self.assertGreaterEqual(metrics["sale_count"], 1)
        self.assertIsInstance(metrics["inventory_cost"], float)
        self.assertEqual(metrics["gross_sales"], 120.0)
        self.assertEqual(metrics["net_sales"], 105.0)

    def test_coin_reference_create_update_list_and_validation(self) -> None:
        with self.assertRaisesRegex(ValueError, "Coin name is required"):
            self.repo.create_coin_reference(
                coin_name="",
                actor="qa-user",
            )
        with self.assertRaisesRegex(ValueError, "Year end must be greater than or equal to year start"):
            self.repo.create_coin_reference(
                coin_name="Bad Year",
                year_start=2020,
                year_end=2019,
                actor="qa-user",
            )

        row = self.repo.create_coin_reference(
            coin_name="Morgan Dollar",
            country="US",
            denomination="$1",
            series="Morgan",
            year_start=1881,
            year_end=1881,
            metal_type="silver",
            km_number="KM-110",
            tags="morgan,silver",
            actor="qa-user",
        )
        self.assertEqual(row.coin_name, "Morgan Dollar")

        updated = self.repo.update_coin_reference(
            row.id,
            {
                "coin_name": "Morgan Dollar 1881",
                "country": "US",
                "notes": "updated",
                "is_active": False,
            },
            actor="qa-user",
        )
        self.assertEqual(updated.coin_name, "Morgan Dollar 1881")
        self.assertFalse(updated.is_active)

        by_query = self.repo.list_coin_references(query="1881", active_only=False, limit=100)
        self.assertTrue(any(r.id == row.id for r in by_query))
        by_country = self.repo.list_coin_references(country="US", active_only=False, limit=100)
        self.assertTrue(any(r.id == row.id for r in by_country))
        by_metal = self.repo.list_coin_references(metal_type="silver", active_only=False, limit=100)
        self.assertTrue(any(r.id == row.id for r in by_metal))
        active_only = self.repo.list_coin_references(active_only=True, limit=100)
        self.assertFalse(any(r.id == row.id for r in active_only))

        with self.assertRaisesRegex(ValueError, "Coin reference 999 not found"):
            self.repo.update_coin_reference(999, {"coin_name": "x"}, actor="qa-user")
        with self.assertRaisesRegex(ValueError, "Year end must be greater than or equal to year start"):
            self.repo.update_coin_reference(row.id, {"year_start": 2020, "year_end": 2019}, actor="qa-user")
        with self.assertRaisesRegex(ValueError, "Coin name is required"):
            self.repo.update_coin_reference(row.id, {"coin_name": ""}, actor="qa-user")

    def test_coin_reference_update_ignores_unknown_and_noop(self) -> None:
        row = self.repo.create_coin_reference(
            coin_name="Branch Coin",
            country="US",
            series="Branch",
            year_start=1900,
            year_end=1900,
            actor="qa-user",
        )
        before_updates = [
            log
            for log in self.repo.list_audit_logs(limit=200)
            if log.entity_type == "coin_reference" and log.entity_id == row.id and log.action == "update"
        ]
        updated = self.repo.update_coin_reference(
            row.id,
            {"unknown_field": "ignored", "coin_name": row.coin_name},
            actor="qa-user",
        )
        self.assertEqual(updated.id, row.id)
        after_updates = [
            log
            for log in self.repo.list_audit_logs(limit=200)
            if log.entity_type == "coin_reference" and log.entity_id == row.id and log.action == "update"
        ]
        self.assertEqual(len(after_updates), len(before_updates))

    def test_log_integration_event_and_list_filter(self) -> None:
        self.repo.log_integration_event(
            actor="ops1",
            integration="shipping",
            action="purchase_label",
            status="success",
            details={"provider": "pirateship"},
        )
        self.repo.log_integration_event(
            actor="ops2",
            integration="sync",
            action="orders_pull",
            status="failed",
            details={"error": "timeout"},
        )

        all_rows = self.repo.list_ai_chat_interactions(limit=50)
        # ensure ai chat rows from previous tests are ignored for this specific filter call
        event_rows = self.repo.list_ai_chat_interactions(limit=50, event_type="integration")
        self.assertIsInstance(all_rows, list)
        self.assertIsInstance(event_rows, list)

        # integration events are audit rows in separate entity type
        audit_rows = self.repo.list_audit_logs(limit=100)
        integration = [r for r in audit_rows if r.entity_type == "integration_event"]
        self.assertEqual(len(integration), 2)

    def test_list_ai_chat_interactions_handles_non_dict_parsed_and_metadata_not_dict(self) -> None:
        # parsed JSON is a list => payload remains {}
        self.db.add(
            AuditLog(
                entity_type="ai_chat",
                entity_id=None,
                action="query",
                actor="qa-user",
                changes_json='["not-a-dict"]',
                created_at=datetime(2026, 3, 30, 12, 0, 0),
            )
        )
        # parsed JSON is dict but metadata is scalar => metadata normalization branch.
        self.db.add(
            AuditLog(
                entity_type="ai_chat",
                entity_id=None,
                action="query",
                actor="qa-user",
                changes_json=json.dumps(
                    {
                        "after": {
                            "intent": "test",
                            "denied": False,
                            "elapsed_ms": 12,
                            "metadata": "bad-metadata-type",
                        }
                    }
                ),
                created_at=datetime(2026, 3, 30, 12, 1, 0),
            )
        )
        self.db.commit()
        rows = self.repo.list_ai_chat_interactions(limit=20)
        self.assertGreaterEqual(len(rows), 2)
        self.assertTrue(any(r.get("intent") == "test" for r in rows))

    def test_record_product_repurchase_updates_weighted_cost_and_quantity(self) -> None:
        product = self._create_product(sku="GS-REP-001", qty=10)
        self.assertEqual(product.acquisition_cost, Decimal("25.00"))
        updated = self.repo.record_product_repurchase(
            product_id=product.id,
            quantity_acquired=5,
            unit_cost=Decimal("35.00"),
            actor="qa-user",
            notes="supplier restock",
        )
        self.assertEqual(updated.current_quantity, 15)
        # weighted avg = (10*25 + 5*35) / 15 = 28.333..., rounded by model precision.
        self.assertEqual(float(updated.acquisition_cost), 28.33)
        movements = self.repo.list_inventory_movements(limit=50)
        self.assertTrue(any(m.movement_type == "repurchase_in" for m in movements))

    def test_record_product_repurchase_with_lot_creates_assignment(self) -> None:
        product = self._create_product(sku="GS-REP-002", qty=2)
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-REP-1",
            vendor="Rep Vendor",
            purchase_date=datetime(2026, 3, 1, 8, 0, 0),
            total_cost=Decimal("500.00"),
            notes="restock lot",
        )
        updated = self.repo.record_product_repurchase(
            product_id=product.id,
            quantity_acquired=3,
            unit_cost=Decimal("40.00"),
            lot_id=lot.id,
            actor="qa-user",
        )
        self.assertEqual(updated.current_quantity, 5)
        assignments = self.repo.list_product_lot_assignments()
        self.assertTrue(any(a.product_id == product.id and a.lot_id == lot.id for a in assignments))
        with self.assertRaisesRegex(ValueError, "Product 99999 not found"):
            self.repo.record_product_repurchase(
                product_id=99999,
                quantity_acquired=1,
                unit_cost=Decimal("1.00"),
                actor="qa-user",
            )

    def test_update_product_validates_ebay_purchase_fields_and_coin_reference(self) -> None:
        product = self._create_product(sku="GS-UPD-001", qty=5)
        with self.assertRaisesRegex(ValueError, "eBay purchase item ID is required"):
            self.repo.update_product(
                product.id,
                {"ebay_purchase": True, "ebay_purchase_item_id": ""},
                actor="qa-user",
            )

        with self.assertRaisesRegex(ValueError, "Selected coin reference does not exist"):
            self.repo.update_product(
                product.id,
                {"coin_reference_id": 99999},
                actor="qa-user",
            )

        coin = CoinReferenceCatalog(coin_name="Test Coin", series="Series", year_start=2000, year_end=2000)
        self.db.add(coin)
        self.db.commit()
        updated = self.repo.update_product(
            product.id,
            {
                "title": "Updated Product",
                "ebay_purchase": True,
                "ebay_purchase_item_id": "12345",
                "ebay_purchase_url": "https://www.ebay.com/itm/12345",
                "coin_reference_id": coin.id,
            },
            actor="qa-user",
        )
        self.assertEqual(updated.title, "Updated Product")
        self.assertTrue(updated.ebay_purchase)
        self.assertEqual(updated.ebay_purchase_item_id, "12345")
        self.assertEqual(updated.coin_reference_id, coin.id)

        disabled = self.repo.update_product(
            product.id,
            {"ebay_purchase": False},
            actor="qa-user",
        )
        self.assertFalse(disabled.ebay_purchase)
        self.assertEqual(disabled.ebay_purchase_item_id or "", "")
        self.assertEqual(disabled.ebay_purchase_url or "", "")

    def test_update_product_not_found_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "Product 99999 not found"):
            self.repo.update_product(99999, {"title": "x"}, actor="qa-user")

    def test_update_product_ignores_unknown_fields_and_no_change_no_audit(self) -> None:
        product = self._create_product(sku="GS-UPD-002", qty=5)
        before_updates = [
            row
            for row in self.repo.list_audit_logs(limit=200)
            if row.entity_type == "product" and row.entity_id == product.id and row.action == "update"
        ]
        result = self.repo.update_product(
            product.id,
            {"unknown_field": "ignored", "title": product.title},
            actor="qa-user",
        )
        self.assertEqual(result.id, product.id)
        after_updates = [
            row
            for row in self.repo.list_audit_logs(limit=200)
            if row.entity_type == "product" and row.entity_id == product.id and row.action == "update"
        ]
        self.assertEqual(len(after_updates), len(before_updates))

    def test_convert_inventory_to_product_happy_path_and_errors(self) -> None:
        source = self._create_product(sku="GS-CONV-SRC-1", qty=20)
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-CONV-1",
            vendor="Vendor",
            purchase_date=datetime(2026, 3, 1, 8, 0, 0),
            total_cost=Decimal("100.00"),
            notes="",
        )
        target = self.repo.convert_inventory_to_product(
            source_product_id=source.id,
            source_quantity_used=5,
            target_sku="GS-CONV-TGT-1",
            target_title="Converted Item",
            target_category="bullion",
            target_quantity_created=2,
            target_unit_cost=None,
            lot_id=lot.id,
            notes="melted/recast",
            actor="qa-user",
        )
        self.assertEqual(target.sku, "GS-CONV-TGT-1")
        self.assertEqual(target.current_quantity, 2)
        source_after = self.db.get(Product, source.id)
        self.assertEqual(source_after.current_quantity, 15)

        movements = self.repo.list_inventory_movements(limit=100)
        mtypes = [m.movement_type for m in movements]
        self.assertIn("conversion_out", mtypes)
        self.assertIn("conversion_in", mtypes)
        assignments = self.repo.list_product_lot_assignments()
        self.assertTrue(any(a.product_id == target.id and a.lot_id == lot.id for a in assignments))

        with self.assertRaisesRegex(ValueError, "Source product 99999 not found"):
            self.repo.convert_inventory_to_product(
                source_product_id=99999,
                source_quantity_used=1,
                target_sku="GS-X",
                target_title="X",
                target_category="other",
            )
        with self.assertRaisesRegex(ValueError, "Not enough source quantity on hand"):
            self.repo.convert_inventory_to_product(
                source_product_id=source.id,
                source_quantity_used=999,
                target_sku="GS-X2",
                target_title="X2",
                target_category="other",
            )

    def test_convert_inventory_to_multiple_products_paths(self) -> None:
        source = self._create_product(sku="GS-CONV-SRC-2", qty=30)
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-CONV-2",
            vendor="Vendor",
            purchase_date=datetime(2026, 3, 1, 8, 0, 0),
            total_cost=Decimal("100.00"),
            notes="",
        )

        with self.assertRaisesRegex(ValueError, "At least one target product is required"):
            self.repo.convert_inventory_to_multiple_products(
                source_product_id=source.id,
                source_quantity_used=1,
                targets=[],
            )

        with self.assertRaisesRegex(ValueError, "Source product 99999 not found"):
            self.repo.convert_inventory_to_multiple_products(
                source_product_id=99999,
                source_quantity_used=1,
                targets=[{"sku": "A", "title": "A", "quantity_created": 1}],
            )
        with self.assertRaisesRegex(ValueError, "Not enough source quantity on hand"):
            self.repo.convert_inventory_to_multiple_products(
                source_product_id=source.id,
                source_quantity_used=999,
                targets=[{"sku": "A", "title": "A", "quantity_created": 1}],
            )

        targets = [
            {
                "sku": "GS-CONV-A",
                "title": "Converted A",
                "category": "bullion",
                "inventory_class": "sellable",
                "quantity_created": 2,
                "unit_cost": None,
            },
            {
                "sku": "GS-CONV-B",
                "title": "Converted B",
                "category": "bullion",
                "inventory_class": "sellable",
                "quantity_created": 3,
                "unit_cost": Decimal("5.00"),
            },
        ]
        created = self.repo.convert_inventory_to_multiple_products(
            source_product_id=source.id,
            source_quantity_used=10,
            targets=targets,
            lot_id=lot.id,
            notes="bulk conversion",
            actor="qa-user",
        )
        self.assertEqual(len(created), 2)
        self.assertEqual(self.db.get(Product, source.id).current_quantity, 20)
        self.assertTrue(any(p.sku == "GS-CONV-A" for p in created))
        self.assertTrue(any(p.sku == "GS-CONV-B" for p in created))
        assignments = self.repo.list_product_lot_assignments()
        created_ids = {p.id for p in created}
        self.assertTrue(any(a.product_id in created_ids and a.lot_id == lot.id for a in assignments))

    def test_update_purchase_lot_not_found_and_noop_paths(self) -> None:
        with self.assertRaisesRegex(ValueError, "Purchase lot 99999 not found"):
            self.repo.update_purchase_lot(99999, {"vendor": "x"}, actor="qa-user")

        lot = self.repo.create_purchase_lot(
            lot_code="LOT-UPD-1",
            vendor="Vendor A",
            purchase_date=datetime(2026, 3, 1, 8, 0, 0),
            total_cost=Decimal("100.00"),
            notes="",
        )
        before = [
            row
            for row in self.repo.list_audit_logs(limit=200)
            if row.entity_type == "purchase_lot" and row.entity_id == lot.id and row.action == "update"
        ]
        unchanged = self.repo.update_purchase_lot(lot.id, {"vendor": "Vendor A"}, actor="qa-user")
        self.assertEqual(unchanged.vendor, "Vendor A")
        unchanged_unknown = self.repo.update_purchase_lot(
            lot.id,
            {"vendor": "Vendor A", "unknown_field": "ignored"},
            actor="qa-user",
        )
        self.assertEqual(unchanged_unknown.vendor, "Vendor A")
        after = [
            row
            for row in self.repo.list_audit_logs(limit=200)
            if row.entity_type == "purchase_lot" and row.entity_id == lot.id and row.action == "update"
        ]
        self.assertEqual(len(after), len(before))

    def test_create_product_create_sale_and_assign_to_lot_edge_paths(self) -> None:
        with self.assertRaisesRegex(ValueError, "eBay purchase item ID is required"):
            self.repo.create_product(
                sku="GS-CR-EDGE-1",
                title="Edge",
                category="other",
                description="",
                metal_type="",
                weight_oz=None,
                acquisition_cost=Decimal("1.00"),
                current_quantity=1,
                ebay_purchase=True,
                ebay_purchase_item_id="",
            )

        with self.assertRaisesRegex(ValueError, "coin reference does not exist"):
            self.repo.create_product(
                sku="GS-CR-EDGE-2",
                title="Edge2",
                category="other",
                description="",
                metal_type="",
                weight_oz=None,
                acquisition_cost=Decimal("1.00"),
                current_quantity=0,
                coin_reference_id=99999,
            )

        lot = self.repo.create_purchase_lot(
            lot_code="LOT-ALLOC-1",
            vendor="Vendor B",
            purchase_date=datetime(2026, 3, 2, 9, 0, 0),
            total_cost=Decimal("200.00"),
            total_tax_paid=Decimal("20.00"),
            total_shipping_paid=Decimal("10.00"),
            total_handling_paid=Decimal("6.00"),
            notes="",
        )
        product = self.repo.create_product(
            sku="GS-ALLOC-1",
            title="Allocated Product",
            category="bullion",
            description="",
            metal_type="silver",
            weight_oz=Decimal("1.00"),
            acquisition_cost=Decimal("5.00"),
            current_quantity=10,
            lot_id=lot.id,
            acquisition_tax_paid=None,
            acquisition_shipping_paid=None,
            acquisition_handling_paid=None,
        )
        assignments = self.repo.list_product_lot_assignments()
        assigned = next(a for a in assignments if a.product_id == product.id and a.lot_id == lot.id)
        self.assertIsNotNone(assigned.allocated_tax_paid)
        self.assertIsNotNone(assigned.allocated_shipping_paid)
        self.assertIsNotNone(assigned.allocated_handling_paid)

        start_movements = len(self.repo.list_inventory_movements(limit=500))
        self.repo.create_sale(
            marketplace="local",
            sold_price=Decimal("5.00"),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("0.00"),
            quantity_sold=1,
            product_id=99999,
            sold_at=datetime(2026, 3, 3, 10, 0, 0),
        )
        end_movements = len(self.repo.list_inventory_movements(limit=500))
        self.assertEqual(end_movements, start_movements)

        p2 = self._create_product(sku="GS-ALLOC-2", qty=1)
        lot2 = self.repo.create_purchase_lot(
            lot_code="LOT-ALLOC-2",
            vendor="Vendor C",
            purchase_date=datetime(2026, 3, 2, 9, 30, 0),
            total_cost=Decimal("120.00"),
            total_tax_paid=Decimal("12.00"),
            total_shipping_paid=Decimal("8.00"),
            total_handling_paid=Decimal("4.00"),
            notes="",
        )
        assigned2 = self.repo.assign_product_to_lot(
            product_id=p2.id,
            lot_id=lot2.id,
            quantity_acquired=2,
            unit_cost=Decimal("10.00"),
            acquired_at=datetime(2026, 3, 3, 10, 0, 0),
            unit_tax_paid=None,
            unit_shipping_paid=None,
            unit_handling_paid=None,
        )
        self.assertIsNotNone(assigned2.allocated_tax_paid)
        self.assertIsNotNone(assigned2.allocated_shipping_paid)
        self.assertIsNotNone(assigned2.allocated_handling_paid)

    def test_saved_filter_profile_upsert_existing_row_updates_and_default_demotion(self) -> None:
        old_default = self.repo.upsert_saved_filter_profile(
            environment="local",
            username="ops-shared-a",
            scope="products",
            name="Shared Existing",
            filter_json='{"q":"old"}',
            is_shared=True,
            is_default=True,
            is_active=True,
            actor="qa-user",
        )
        contender = self.repo.upsert_saved_filter_profile(
            environment="local",
            username="ops-shared-b",
            scope="products",
            name="Shared New Default",
            filter_json='{"q":"new"}',
            is_shared=True,
            is_default=False,
            is_active=True,
            actor="qa-user",
        )
        updated = self.repo.upsert_saved_filter_profile(
            environment="local",
            username="ops-shared-b",
            scope="products",
            name="Shared New Default",
            filter_json='{"q":"updated"}',
            is_shared=True,
            is_default=True,
            is_active=False,
            actor="qa-user",
        )
        self.assertEqual(updated.id, contender.id)
        self.assertEqual(str(updated.filter_json), '{"q":"updated"}')
        self.assertTrue(bool(updated.is_default))
        self.assertFalse(bool(updated.is_active))
        demoted = self.db.get(type(old_default), old_default.id)
        self.assertFalse(bool(demoted.is_default))

    def test_list_helpers_for_orders_items_and_purchase_lots(self) -> None:
        product = self._create_product(sku="GS-LIST-ORD-001", qty=2)
        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="EBAY-LIST-ORD-1",
            order_status="paid",
            sold_at=datetime(2026, 3, 24, 11, 0, 0),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("0.00"),
            items=[{"product_id": product.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("11.00")}],
            actor="qa-user",
        )
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-LIST-1",
            vendor="Vendor List",
            purchase_date=datetime(2026, 3, 24, 10, 0, 0),
            total_cost=Decimal("20.00"),
            notes="",
        )
        orders = self.repo.list_orders()
        order_items = self.repo.list_order_items()
        purchase_lots = self.repo.list_purchase_lots()
        self.assertTrue(any(o.id == order.id for o in orders))
        self.assertTrue(any(oi.order_id == order.id for oi in order_items))
        self.assertTrue(any(pl.id == lot.id for pl in purchase_lots))

    def test_update_purchase_document_and_inventory_source_noop_unknown_paths(self) -> None:
        source = self.repo.create_inventory_source(
            name="Source Upd",
            source_type="dealer",
            contact_name="Rep",
            contact_email="rep@example.com",
            is_active=True,
        )
        unchanged_source = self.repo.update_inventory_source(
            source.id,
            {"name": "Source Upd", "unknown_field": "ignored"},
            actor="qa-user",
        )
        self.assertEqual(unchanged_source.name, "Source Upd")

        product = self._create_product(sku="GS-PDOC-UPD-001", qty=1)
        doc = self.repo.create_purchase_document(
            document_kind="invoice",
            title="Invoice",
            original_filename="invoice.pdf",
            content_type="application/pdf",
            size_bytes=1024,
            content_sha256="abc123",
            s3_bucket="media-bucket",
            s3_key="incoming/invoice.pdf",
            s3_url="https://media.example.com/incoming/invoice.pdf",
            lot_id=None,
            product_id=product.id,
            source_id=source.id,
            ai_extracted_json='{"lines":[]}',
            ai_summary="Parsed invoice",
            uploaded_by="qa-user",
            actor="qa-user",
        )
        unchanged_doc = self.repo.update_purchase_document(
            doc.id,
            {"title": "Invoice", "unknown_field": "ignored"},
            actor="qa-user",
        )
        self.assertEqual(unchanged_doc.title, "Invoice")

    def test_purchase_lot_source_autofill_and_shared_filter_admin_paths(self) -> None:
        src = self.repo.create_inventory_source(
            name="APMEX",
            source_type="dealer",
            contact_name="Rep",
            contact_email="rep@example.com",
            is_active=True,
        )
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-SRC-AUTO",
            vendor="",
            purchase_date=datetime(2026, 3, 25, 8, 0, 0),
            total_cost=Decimal("90.00"),
            source_id=src.id,
        )
        self.assertEqual(lot.vendor, "APMEX")

        shared = self.repo.upsert_saved_filter_profile(
            environment="local",
            username="ops-x",
            scope="products",
            name="Shared Keep",
            filter_json="{}",
            is_shared=True,
            is_default=False,
            is_active=True,
            actor="qa-user",
        )
        same_owner = self.repo.transfer_shared_filter_ownership(
            profile_id=shared.id,
            new_username="ops-x",
            actor="qa-user",
        )
        self.assertEqual(same_owner.id, shared.id)

        self.assertFalse(
            self.repo.delete_shared_filter_profile_by_id(profile_id=999999, actor="qa-user")
        )
        self.assertTrue(
            self.repo.delete_saved_filter_profile_by_id(profile_id=shared.id, actor="qa-user")
        )

    def test_validate_inventory_class_rejects_invalid_value(self) -> None:
        with self.assertRaises(ValueError):
            self.repo.create_product(
                sku="GS-BAD-CLASS-001",
                title="Bad Class",
                category="misc",
                description="",
                metal_type="other",
                weight_oz=None,
                acquisition_cost=Decimal("1.00"),
                current_quantity=1,
                inventory_class="invalid_class",
                acquired_at=datetime(2026, 3, 26, 9, 0, 0),
            )

    def test_shipping_preset_and_list_sales_paths(self) -> None:
        product = self._create_product(sku="GS-SALE-LIST-001", qty=2)
        sale = self.repo.create_sale(
            marketplace="local",
            sold_price=Decimal("40.00"),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("0.00"),
            quantity_sold=1,
            product_id=product.id,
            sold_at=datetime(2026, 3, 26, 11, 0, 0),
        )
        sales = self.repo.list_sales()
        self.assertTrue(any(row.id == sale.id for row in sales))

        default_preset = self.repo.create_shipping_preset(
            name="USPS Ground",
            shipping_provider="usps",
            shipping_service="ground_advantage",
            is_default=True,
            is_active=False,
            actor="qa-user",
        )
        self.repo.create_shipping_preset(
            name="UPS Ground",
            shipping_provider="ups",
            shipping_service="ground",
            is_default=True,
            is_active=True,
            actor="qa-user",
        )
        all_rows = self.repo.list_shipping_presets(active_only=False)
        active_rows = self.repo.list_shipping_presets(active_only=True)
        self.assertTrue(any(r.id == default_preset.id for r in all_rows))
        self.assertFalse(any(r.id == default_preset.id for r in active_rows))
        with self.assertRaises(ValueError):
            self.repo.update_shipping_preset(999999, {"name": "x"}, actor="qa-user")

    def test_mark_shipments_exported_handles_empty_missing_and_same_stamp(self) -> None:
        self.assertEqual(self.repo.mark_shipments_exported([]), 0)
        stamp = datetime(2026, 3, 26, 12, 0, 0)
        self.assertEqual(self.repo.mark_shipments_exported([999999], exported_at=stamp), 0)

        product = self._create_product(sku="GS-SHIP-EXP-001", qty=3)
        sale = self.repo.create_sale(
            marketplace="local",
            sold_price=Decimal("30.00"),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("5.00"),
            quantity_sold=1,
            product_id=product.id,
            sold_at=datetime(2026, 3, 26, 12, 30, 0),
        )
        self.assertEqual(self.repo.mark_shipments_exported([sale.id], exported_at=stamp), 1)
        self.assertEqual(self.repo.mark_shipments_exported([sale.id], exported_at=stamp), 0)

    def test_runtime_setting_lifecycle_paths(self) -> None:
        row = self.repo.upsert_runtime_setting(
            environment="local",
            key="qa.example.key",
            value="1",
            value_type="int",
            description="qa",
            is_active=True,
            actor="qa-user",
        )
        active_rows = self.repo.list_runtime_settings(environment="local", active_only=True)
        self.assertTrue(any(r.id == row.id for r in active_rows))
        fetched = self.repo.get_runtime_setting(
            environment="local", key="qa.example.key", active_only=True
        )
        self.assertIsNotNone(fetched)

        updated = self.repo.upsert_runtime_setting(
            environment="local",
            key="qa.example.key",
            value="2",
            value_type="int",
            description="qa2",
            is_active=False,
            actor="qa-user",
        )
        self.assertEqual(updated.value, "2")
        self.assertFalse(bool(updated.is_active))
        self.assertTrue(self.repo.delete_runtime_setting_by_id(setting_id=row.id, actor="qa-user"))
        self.assertFalse(
            self.repo.delete_runtime_setting_by_id(setting_id=999999, actor="qa-user")
        )

    def test_saved_filter_conflict_transfer_and_delete_shared_guard(self) -> None:
        shared = self.repo.upsert_saved_filter_profile(
            environment="local",
            username="owner-a",
            scope="products",
            name="Conflict Name",
            filter_json="{}",
            is_shared=True,
            is_default=False,
            is_active=True,
            actor="qa-user",
        )
        self.repo.upsert_saved_filter_profile(
            environment="local",
            username="owner-b",
            scope="products",
            name="Conflict Name",
            filter_json="{}",
            is_shared=True,
            is_default=False,
            is_active=True,
            actor="qa-user",
        )
        with self.assertRaises(ValueError):
            self.repo.transfer_shared_filter_ownership(
                profile_id=shared.id, new_username="owner-b", actor="qa-user"
            )

        personal = self.repo.upsert_saved_filter_profile(
            environment="local",
            username="owner-c",
            scope="products",
            name="Personal",
            filter_json="{}",
            is_shared=False,
            is_default=False,
            is_active=True,
            actor="qa-user",
        )
        visible = self.repo.list_saved_filter_profiles(
            environment="local",
            scope="products",
            username="owner-c",
            include_shared=False,
            active_only=True,
        )
        self.assertTrue(any(r.id == personal.id for r in visible))
        with self.assertRaises(ValueError):
            self.repo.delete_shared_filter_profile_by_id(profile_id=personal.id, actor="qa-user")

    def test_inventory_source_active_filter_and_update_lot_not_found(self) -> None:
        source = self.repo.create_inventory_source(
            name="Inactive Source",
            source_type="dealer",
            contact_name="Rep",
            contact_email="rep@example.com",
            is_active=False,
        )
        active_sources = self.repo.list_inventory_sources(active_only=True)
        self.assertFalse(any(s.id == source.id for s in active_sources))

        with self.assertRaises(ValueError):
            self.repo.update_purchase_lot(999999, {"vendor": "x"}, actor="qa-user")

    def test_transfer_shared_filter_ownership_success_path(self) -> None:
        shared = self.repo.upsert_saved_filter_profile(
            environment="local",
            username="owner-1",
            scope="sales",
            name="Shared Transfer",
            filter_json="{}",
            is_shared=True,
            is_default=False,
            is_active=True,
            actor="qa-user",
        )
        moved = self.repo.transfer_shared_filter_ownership(
            profile_id=shared.id,
            new_username="owner-2",
            actor="qa-user",
        )
        self.assertEqual(moved.username, "owner-2")

    def test_ebay_listing_template_update_and_filters(self) -> None:
        first = self.repo.upsert_ebay_listing_template_profile(
            environment="local",
            username="tmpl-user",
            name="Template A",
            marketplace="ebay",
            listing_title_template="Template A title",
            marketplace_details_template="Template A details",
            quantity_default=1,
            listing_price_default=Decimal("10.00"),
            listing_status_default="draft",
            is_shared=False,
            is_default=True,
            is_active=True,
            actor="qa-user",
        )
        second = self.repo.upsert_ebay_listing_template_profile(
            environment="local",
            username="tmpl-user",
            name="Template B",
            marketplace="ebay",
            listing_title_template="Template B title",
            marketplace_details_template="Template B details",
            quantity_default=1,
            listing_price_default=Decimal("12.00"),
            listing_status_default="draft",
            is_shared=False,
            is_default=False,
            is_active=True,
            actor="qa-user",
        )
        updated = self.repo.update_ebay_listing_template_profile(
            second.id,
            {
                "is_default": True,
                "quantity_default": 2,
                "listing_price_default": Decimal("13.00"),
                "unknown_field": "ignored",
            },
            actor="qa-user",
        )
        self.assertTrue(bool(updated.is_default))
        self.assertEqual(updated.quantity_default, 2)
        self.assertEqual(Decimal(str(updated.listing_price_default)), Decimal("13.00"))
        demoted = self.db.get(type(first), first.id)
        self.assertFalse(bool(demoted.is_default))

        filtered = self.repo.list_ebay_listing_template_profiles(
            environment="local",
            username="tmpl-user",
            include_shared=False,
            active_only=True,
        )
        self.assertTrue(any(t.id == second.id for t in filtered))
        with self.assertRaises(ValueError):
            self.repo.update_ebay_listing_template_profile(999999, {"name": "x"}, actor="qa-user")

    def test_ai_provider_default_fallback_and_validation(self) -> None:
        primary = self.repo.upsert_ai_provider_config(
            environment="local",
            name="Primary",
            provider="openai",
            model="gpt-test",
            multimodal_model="",
            base_url="https://api.example.com/v1",
            endpoint_type="responses",
            api_key="secret",
            temperature=Decimal("0.2"),
            max_output_tokens=256,
            timeout_seconds=20,
            notes="",
            is_default=True,
            is_active=False,
            actor="qa-user",
        )
        fallback = self.repo.upsert_ai_provider_config(
            environment="local",
            name="Fallback",
            provider="localai",
            model="local-model",
            multimodal_model="",
            base_url="http://localhost:8080/v1",
            endpoint_type="chat_completions",
            api_key="",
            temperature=Decimal("0.1"),
            max_output_tokens=128,
            timeout_seconds=15,
            notes="",
            is_default=False,
            is_active=True,
            actor="qa-user",
        )
        resolved = self.repo.get_default_ai_provider_config(environment="local")
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.id, fallback.id)
        active_rows = self.repo.list_ai_provider_configs(environment="local", active_only=True)
        self.assertTrue(any(r.id == fallback.id for r in active_rows))
        self.assertFalse(any(r.id == primary.id for r in active_rows))

        with self.assertRaises(ValueError):
            self.repo.upsert_ai_provider_config(
                environment="local",
                name="Bad Tokens",
                provider="openai",
                model="gpt-test",
                multimodal_model="",
                base_url="https://api.example.com/v1",
                endpoint_type="responses",
                api_key="secret",
                temperature=Decimal("0.2"),
                max_output_tokens=0,
                timeout_seconds=20,
                actor="qa-user",
            )
        with self.assertRaises(ValueError):
            self.repo.upsert_ai_provider_config(
                environment="local",
                name="Bad Timeout",
                provider="openai",
                model="gpt-test",
                multimodal_model="",
                base_url="https://api.example.com/v1",
                endpoint_type="responses",
                api_key="secret",
                temperature=Decimal("0.2"),
                max_output_tokens=100,
                timeout_seconds=0,
                actor="qa-user",
            )

    def test_upsert_app_user_can_update_password(self) -> None:
        row = self.repo.upsert_app_user(
            username="qa-upsert-user",
            display_name="QA User",
            email="qa@example.com",
            role="employee",
            password="password123",
            is_active=True,
            actor="qa-user",
        )
        before_updated_at = row.password_updated_at
        updated = self.repo.upsert_app_user(
            username="qa-upsert-user",
            display_name="QA User Updated",
            email="qa-updated@example.com",
            role="admin",
            password="password456",
            is_active=True,
            actor="qa-user",
        )
        self.assertEqual(updated.id, row.id)
        self.assertEqual(updated.role, "admin")
        self.assertNotEqual(updated.email, "qa@example.com")
        self.assertTrue(bool(updated.password_hash))
        self.assertTrue(updated.password_updated_at >= before_updated_at)

    def test_update_order_with_actual_change_commits(self) -> None:
        product = self._create_product(sku="GS-ORD-UPD-CHG-1", qty=4)
        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="ORD-UPDATE-CHANGE-1",
            order_status="paid",
            sold_at=datetime(2026, 3, 27, 12, 0, 0),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("0.00"),
            items=[{"product_id": product.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("10.00")}],
            actor="qa-user",
        )
        changed = self.repo.update_order(order.id, {"order_status": "shipped"}, actor="qa-user")
        self.assertEqual(changed.order_status, "shipped")

    def test_update_purchase_lot_with_real_change(self) -> None:
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-UPD-REAL-1",
            vendor="Vendor Before",
            purchase_date=datetime(2026, 3, 27, 9, 0, 0),
            total_cost=Decimal("55.00"),
            notes="before",
        )
        updated = self.repo.update_purchase_lot(
            lot.id,
            {"vendor": "Vendor After", "notes": "after"},
            actor="qa-user",
        )
        self.assertEqual(updated.vendor, "Vendor After")
        self.assertEqual(updated.notes, "after")

    def test_upsert_app_user_no_change_path_and_update_app_user_unknown_field(self) -> None:
        created = self.repo.upsert_app_user(
            username="qa-no-change-user",
            display_name="No Change",
            email="nochange@example.com",
            role="employee",
            password="password123",
            is_active=True,
            actor="qa-user",
        )
        # Same values, no password update: exercises no-change return branch.
        same = self.repo.upsert_app_user(
            username="qa-no-change-user",
            display_name="No Change",
            email="nochange@example.com",
            role="employee",
            password="",
            is_active=True,
            actor="qa-user",
        )
        self.assertEqual(same.id, created.id)

        # Unknown field should be ignored while valid field is applied.
        updated = self.repo.update_app_user(
            created.id,
            {"display_name": "Changed", "unknown_field": "ignored"},
            actor="qa-user",
        )
        self.assertEqual(updated.display_name, "Changed")

    def test_update_media_asset_ignores_unknown_field(self) -> None:
        product = self._create_product(sku="GS-MEDIA-UPD-001", qty=1)
        media = self.repo.create_media_asset(
            media_type="image",
            original_filename="img.jpg",
            content_type="image/jpeg",
            size_bytes=1024,
            s3_bucket="bucket",
            s3_key="media/img.jpg",
            s3_url="https://media.example.com/img.jpg",
            product_id=product.id,
            listing_id=None,
        )
        updated = self.repo.update_media_asset(
            media.id,
            {"original_filename": "img-updated.jpg", "unknown_field": "ignored"},
            actor="qa-user",
        )
        self.assertEqual(updated.original_filename, "img-updated.jpg")


if __name__ == "__main__":
    unittest.main()
