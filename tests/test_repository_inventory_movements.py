import unittest
import json
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import select

from app.db.models import (
    AuditLog,
    Base,
    CoinReferenceCatalog,
    Customer,
    MediaAsset,
    OrderFinanceEntry,
    Product,
    ProductLotAssignment,
    Sale,
)
from app.repository import InventoryRepository
from app.auth import has_permission
from app.config import settings
from app.utils.time import utcnow_naive
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

    def test_create_sale_for_bundle_listing_updates_each_component_inventory(self) -> None:
        primary = self._create_product(sku="GS-BUNDLE-PRIMARY", qty=20)
        extra = self._create_product(sku="GS-BUNDLE-EXTRA", qty=12)
        listing = self.repo.create_listing(
            product_id=primary.id,
            marketplace="ebay",
            listing_title="Mixed bundle",
            listing_price=Decimal("50.00"),
            quantity_listed=3,
            marketplace_details=json.dumps(
                {
                    "bundle": {
                        "enabled": True,
                        "kind": "mixed_product_bundle",
                        "primary_product_id": primary.id,
                        "components": [
                            {
                                "product_id": primary.id,
                                "sku": primary.sku,
                                "quantity_per_listing": 2,
                                "current_quantity": 20,
                            },
                            {
                                "product_id": extra.id,
                                "sku": extra.sku,
                                "quantity_per_listing": 3,
                                "current_quantity": 12,
                            },
                        ],
                    }
                }
            ),
            actor="qa-user",
        )

        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("10.00"),
            shipping_cost=Decimal("5.00"),
            quantity_sold=2,
            product_id=primary.id,
            listing_id=listing.id,
            sold_at=datetime(2026, 3, 2, 15, 30, 0),
        )

        self.assertEqual(self.db.get(Product, primary.id).current_quantity, 16)
        self.assertEqual(self.db.get(Product, extra.id).current_quantity, 6)
        movements = [
            m for m in self.repo.list_inventory_movements(limit=50)
            if m.reference_type == "sale" and m.reference_id == sale.id
        ]
        self.assertEqual(sorted(m.movement_type for m in movements), ["sale_bundle_component", "sale_bundle_component"])
        self.assertEqual(sorted((m.product_id, m.quantity_delta) for m in movements), [(primary.id, -4), (extra.id, -6)])

    def test_create_sale_for_inferred_single_product_lot_listing_updates_inventory(self) -> None:
        product = self._create_product(sku="GS-INFERRED-LOT", qty=30)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Lot of 20 Mercury Silver Dimes 90% Silver Coins",
            listing_price=Decimal("69.95"),
            quantity_listed=1,
            marketplace_details=json.dumps({"bundle": {"enabled": False, "components": []}}),
            actor="qa-user",
        )

        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("69.95"),
            fees=Decimal("7.22"),
            shipping_cost=Decimal("0.00"),
            shipping_label_cost=Decimal("6.87"),
            quantity_sold=1,
            product_id=product.id,
            listing_id=listing.id,
            sold_at=datetime(2026, 6, 1, 22, 22, 4),
        )

        refreshed = self.db.get(Product, product.id)
        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed.current_quantity, 10)

        movements = [
            m for m in self.repo.list_inventory_movements(limit=50)
            if m.reference_type == "sale" and m.reference_id == sale.id
        ]
        self.assertEqual(len(movements), 1)
        self.assertEqual(movements[0].movement_type, "sale_bundle_component")
        self.assertEqual(movements[0].quantity_delta, -20)

    def test_create_sale_for_full_tube_listing_updates_inventory_units(self) -> None:
        product = self._create_product(sku="GS-INFERRED-TUBE", qty=30)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Full Tube of 20 1 oz American Prospector Copper Tribute Coins Rounds Collectible",
            listing_price=Decimal("79.95"),
            quantity_listed=1,
            marketplace_details=json.dumps({"bundle": {"enabled": False, "components": []}}),
            actor="qa-user",
        )

        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("79.95"),
            fees=Decimal("8.63"),
            shipping_cost=Decimal("13.41"),
            shipping_label_cost=Decimal("7.90"),
            quantity_sold=1,
            product_id=product.id,
            listing_id=listing.id,
            sold_at=datetime(2026, 6, 9, 6, 48, 24),
        )

        self.assertEqual(self.db.get(Product, product.id).current_quantity, 10)
        movements = [
            m for m in self.repo.list_inventory_movements(limit=50)
            if m.reference_type == "sale" and m.reference_id == sale.id
        ]
        self.assertEqual(len(movements), 1)
        self.assertEqual(movements[0].movement_type, "sale_bundle_component")
        self.assertEqual(movements[0].quantity_delta, -20)

    def test_create_sale_for_roll_of_listing_updates_inventory_units(self) -> None:
        product = self._create_product(sku="GS-INFERRED-ROLL-OF", qty=30)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Roll of 20 Mercury Silver Dimes 90% Silver Coins",
            listing_price=Decimal("79.95"),
            quantity_listed=1,
            marketplace_details=json.dumps({"bundle": {"enabled": False, "components": []}}),
            actor="qa-user",
        )

        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("79.95"),
            fees=Decimal("8.63"),
            shipping_cost=Decimal("13.41"),
            shipping_label_cost=Decimal("7.90"),
            quantity_sold=1,
            product_id=product.id,
            listing_id=listing.id,
            sold_at=datetime(2026, 6, 9, 6, 48, 24),
        )

        self.assertEqual(self.db.get(Product, product.id).current_quantity, 10)
        movements = [
            m for m in self.repo.list_inventory_movements(limit=50)
            if m.reference_type == "sale" and m.reference_id == sale.id
        ]
        self.assertEqual(len(movements), 1)
        self.assertEqual(movements[0].movement_type, "sale_bundle_component")
        self.assertEqual(movements[0].quantity_delta, -20)

    def test_inferred_single_product_lot_listing_does_not_treat_roll_weight_as_quantity(self) -> None:
        product = self._create_product(sku="GS-ROLL-WEIGHT-SINGLE", qty=1)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="George Washington Ballistic Roll 425 Net Grams 50 $1 Coins Uncirculated Roll",
            listing_price=Decimal("79.95"),
            quantity_listed=1,
            marketplace_details=json.dumps({"bundle": {"enabled": False, "components": []}}),
            actor="qa-user",
        )

        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("79.95"),
            fees=Decimal("8.63"),
            shipping_cost=Decimal("13.41"),
            shipping_label_cost=Decimal("7.90"),
            quantity_sold=1,
            product_id=product.id,
            listing_id=listing.id,
            sold_at=datetime(2026, 6, 9, 6, 48, 24),
        )

        self.assertEqual(self.db.get(Product, product.id).current_quantity, 0)
        movements = [
            m for m in self.repo.list_inventory_movements(limit=50)
            if m.reference_type == "sale" and m.reference_id == sale.id
        ]
        self.assertEqual(len(movements), 1)
        self.assertEqual(movements[0].movement_type, "sale")
        self.assertEqual(movements[0].quantity_delta, -1)

    def test_inferred_single_product_lot_listing_supports_lot_without_of(self) -> None:
        product = self._create_product(sku="GS-INFERRED-LOT-NO-OF", qty=20)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Lot 13 Washington Silver Quarters Pre 1964 90% Silver Coins Vintage Collectible",
            listing_price=Decimal("138.00"),
            quantity_listed=1,
            marketplace_details=json.dumps({"bundle": {"enabled": False, "components": []}}),
            actor="qa-user",
        )

        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("138.00"),
            fees=Decimal("6.57"),
            shipping_cost=Decimal("19.56"),
            shipping_label_cost=Decimal("6.07"),
            quantity_sold=1,
            product_id=product.id,
            listing_id=listing.id,
            sold_at=datetime(2026, 6, 8, 2, 31, 51),
        )

        self.assertEqual(self.db.get(Product, product.id).current_quantity, 7)
        movement = next(
            m for m in self.repo.list_inventory_movements(limit=50)
            if m.reference_type == "sale" and m.reference_id == sale.id
        )
        self.assertEqual(movement.quantity_delta, -13)

    def test_inferred_single_product_lot_listing_does_not_treat_percent_as_quantity(self) -> None:
        product = self._create_product(sku="GS-INFERRED-LOT-PERCENT", qty=20)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Lot 90% Silver Washington Quarters Vintage Collectible",
            listing_price=Decimal("138.00"),
            quantity_listed=1,
            marketplace_details=json.dumps({"bundle": {"enabled": False, "components": []}}),
            actor="qa-user",
        )

        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("138.00"),
            fees=Decimal("6.57"),
            shipping_cost=Decimal("19.56"),
            shipping_label_cost=Decimal("6.07"),
            quantity_sold=1,
            product_id=product.id,
            listing_id=listing.id,
            sold_at=datetime(2026, 6, 8, 2, 31, 51),
        )

        self.assertEqual(self.db.get(Product, product.id).current_quantity, 19)
        movement = next(
            m for m in self.repo.list_inventory_movements(limit=50)
            if m.reference_type == "sale" and m.reference_id == sale.id
        )
        self.assertEqual(movement.movement_type, "sale")
        self.assertEqual(movement.quantity_delta, -1)

    def test_accounting_exception_flags_legacy_inferred_lot_sale_movement_mismatch(self) -> None:
        product = self.repo.create_product(
            sku="GS-INFERRED-LOT-LEGACY",
            title="Legacy Inferred Lot Product",
            category="coin",
            description="",
            metal_type="silver",
            weight_oz=None,
            acquisition_cost=Decimal("3.00"),
            current_quantity=30,
            acquired_at=datetime(2026, 5, 20, 12, 0, 0),
        )
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Random Year Mercury Dime Circulated 90% Silver",
            listing_price=Decimal("69.95"),
            quantity_listed=1,
            marketplace_details=json.dumps({"bundle": {"enabled": False, "components": []}}),
            actor="qa-user",
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("69.95"),
            fees=Decimal("7.22"),
            shipping_cost=Decimal("0.00"),
            shipping_label_cost=Decimal("6.87"),
            quantity_sold=1,
            product_id=product.id,
            listing_id=listing.id,
            external_order_id="EBAY-LEGACY-LOT-20",
            sold_at=datetime(2026, 6, 1, 22, 22, 4),
        )
        self.repo.update_listing(
            listing.id,
            {
                "listing_title": "Lot of 20 Mercury Silver Dimes 90% Silver Coins Vintage U.S. Constitutional",
                "marketplace_details": json.dumps({"bundle": {"enabled": False, "components": []}}),
            },
            actor="qa-user",
        )

        rows = self.repo.report_accounting_exception_rows(
            start_dt=datetime(2026, 6, 1, 0, 0, 0),
            end_dt=datetime(2026, 6, 2, 0, 0, 0),
        )
        matches = [
            row
            for row in rows
            if row.get("exception_type") == "listing_lot_inventory_movement_mismatch"
            and int(row.get("entity_id") or 0) == int(sale.id)
        ]

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["severity"], "P1")
        self.assertEqual(matches[0]["entity_type"], "sale")
        self.assertEqual(matches[0]["reference"], "EBAY-LEGACY-LOT-20")
        self.assertAlmostEqual(float(matches[0].get("amount") or 0.0), 19.0, places=2)
        self.assertIn("represent 20 inventory unit(s)", matches[0]["details"])
        self.assertIn("consumed 1 unit(s)", matches[0]["details"])
        live = self.repo.dashboard_live_metrics(now=datetime(2026, 6, 2, 0, 0, 0), include_fee_type_breakdown=False)
        self.assertEqual(live["sales_30d_lot_listing_movement_mismatch_count"], 1)
        self.assertAlmostEqual(live["sales_30d_lot_listing_movement_mismatch_units"], 19.0, places=2)
        self.assertEqual(live["sales_30d_lot_listing_movement_mismatch_sale_ids"], [sale.id])
        audit_rows = self.repo.dashboard_profit_basis_rows(now=datetime(2026, 6, 2, 0, 0, 0), limit=10)
        audit_row = next(row for row in audit_rows if int(row["sale_id"]) == int(sale.id))
        self.assertEqual(audit_row["inventory_movement_units_expected"], 20)
        self.assertEqual(audit_row["inventory_movement_units_recorded"], 1)
        self.assertEqual(audit_row["listing_lot_movement_mismatch_units"], 19)
        self.assertTrue(audit_row["listing_lot_movement_mismatch"])

        preview = self.repo.repair_sale_lot_listing_inventory_movements(
            sale.id,
            actor="qa-user",
            dry_run=True,
        )
        self.assertTrue(preview["dry_run"])
        self.assertEqual(preview["total_repair_units"], 19)
        self.assertEqual(preview["repairs"][0]["recorded_units_before"], 1)
        self.assertEqual(preview["repairs"][0]["expected_units"], 20)

        result = self.repo.repair_sale_lot_listing_inventory_movements(
            sale.id,
            actor="qa-user",
        )

        self.assertFalse(result["dry_run"])
        self.assertEqual(result["total_repair_units"], 19)
        self.assertEqual(self.db.get(Product, product.id).current_quantity, 10)
        repaired_movements = [
            m
            for m in self.repo.list_inventory_movements(limit=100)
            if m.reference_type == "sale" and int(m.reference_id or 0) == int(sale.id)
        ]
        self.assertEqual(sum(abs(m.quantity_delta) for m in repaired_movements if m.quantity_delta < 0), 20)
        self.assertTrue(any(m.movement_type == "sale_lot_listing_movement_repair" for m in repaired_movements))
        repaired_rows = self.repo.report_accounting_exception_rows(
            start_dt=datetime(2026, 6, 1, 0, 0, 0),
            end_dt=datetime(2026, 6, 2, 0, 0, 0),
        )
        repaired_matches = [
            row
            for row in repaired_rows
            if row.get("exception_type") == "listing_lot_inventory_movement_mismatch"
            and int(row.get("entity_id") or 0) == int(sale.id)
        ]
        self.assertEqual(repaired_matches, [])
        repair_audit = self.db.scalars(
            select(AuditLog).where(
                AuditLog.entity_type == "sale",
                AuditLog.entity_id == sale.id,
                AuditLog.action == "repair_lot_movements",
            )
        ).first()
        self.assertIsNotNone(repair_audit)

    def test_lot_listing_movement_repair_can_preserve_reconciled_current_inventory(self) -> None:
        product = self.repo.create_product(
            sku="GS-INFERRED-LOT-STOCK-ZERO",
            title="Legacy Inferred Lot Current Stock Zero",
            category="coin",
            description="",
            metal_type="silver",
            weight_oz=None,
            acquisition_cost=Decimal("1.00"),
            current_quantity=1,
            acquired_at=datetime(2026, 5, 20, 12, 0, 0),
        )
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Kennedy Half Dollar Single",
            listing_price=Decimal("80.00"),
            quantity_listed=1,
            marketplace_details=json.dumps({"bundle": {"enabled": False, "components": []}}),
            actor="qa-user",
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("80.00"),
            fees=Decimal("8.00"),
            shipping_cost=Decimal("6.00"),
            shipping_label_cost=Decimal("5.00"),
            quantity_sold=1,
            product_id=product.id,
            listing_id=listing.id,
            external_order_id="EBAY-LEGACY-LOT-52",
            sold_at=datetime(2026, 6, 1, 12, 0, 0),
        )
        self.repo.update_listing(
            listing.id,
            {
                "listing_title": "Lot of 52 Kennedy Half Dollars 1971 to 2010 D, P, None, and a few S! Huge lot!",
                "marketplace_details": json.dumps({"bundle": {"enabled": False, "components": []}}),
            },
            actor="qa-user",
        )
        product.current_quantity = 0
        self.db.commit()

        with self.assertRaisesRegex(ValueError, "without overdrawing current stock"):
            self.repo.repair_sale_lot_listing_inventory_movements(sale.id, actor="qa-user")

        preview = self.repo.repair_sale_lot_listing_inventory_movements(
            sale.id,
            actor="qa-user",
            preserve_current_inventory=True,
            dry_run=True,
        )
        self.assertTrue(preview["preserve_current_inventory"])
        self.assertEqual(preview["total_repair_units"], 51)
        self.assertEqual(preview["repairs"][0]["quantity_before"], 0)
        self.assertEqual(preview["repairs"][0]["quantity_after"], 0)
        self.assertEqual(preview["repairs"][0]["movement_quantity_before"], 51)
        self.assertEqual(preview["repairs"][0]["movement_quantity_after"], 0)

        result = self.repo.repair_sale_lot_listing_inventory_movements(
            sale.id,
            actor="qa-user",
            preserve_current_inventory=True,
        )

        self.assertFalse(result["dry_run"])
        self.assertEqual(result["total_repair_units"], 51)
        self.assertEqual(self.db.get(Product, product.id).current_quantity, 0)
        repaired_movements = [
            m
            for m in self.repo.list_inventory_movements(limit=100)
            if m.reference_type == "sale" and int(m.reference_id or 0) == int(sale.id)
        ]
        self.assertEqual(sum(abs(m.quantity_delta) for m in repaired_movements if m.quantity_delta < 0), 52)
        repair_movement = next(m for m in repaired_movements if m.movement_type == "sale_lot_listing_movement_repair")
        self.assertEqual(repair_movement.quantity_delta, -51)
        self.assertIn("preserve_current_inventory=True", repair_movement.notes)
        repaired_rows = self.repo.report_accounting_exception_rows(
            start_dt=datetime(2026, 6, 1, 0, 0, 0),
            end_dt=datetime(2026, 6, 2, 0, 0, 0),
        )
        repaired_matches = [
            row
            for row in repaired_rows
            if row.get("exception_type") == "listing_lot_inventory_movement_mismatch"
            and int(row.get("entity_id") or 0) == int(sale.id)
        ]
        self.assertEqual(repaired_matches, [])
        live = self.repo.dashboard_live_metrics(now=datetime(2026, 6, 2, 0, 0, 0), include_fee_type_breakdown=False)
        self.assertEqual(live["sales_30d_lot_listing_movement_mismatch_count"], 0)
        self.assertEqual(live["sales_30d_lot_listing_movement_mismatch_units"], 0.0)
        self.assertEqual(live["sales_30d_lot_listing_movement_mismatch_sale_ids"], [])
        audit_rows = self.repo.dashboard_profit_basis_rows(now=datetime(2026, 6, 2, 0, 0, 0), limit=10)
        audit_row = next(row for row in audit_rows if int(row["sale_id"]) == int(sale.id))
        self.assertEqual(audit_row["inventory_movement_units_expected"], 52)
        self.assertEqual(audit_row["inventory_movement_units_recorded"], 52)
        self.assertEqual(audit_row["listing_lot_movement_mismatch_units"], 0)
        self.assertFalse(audit_row["listing_lot_movement_mismatch"])

    def test_update_sale_for_bundle_listing_reconciles_component_inventory(self) -> None:
        primary = self._create_product(sku="GS-BUNDLE-UPD-PRIMARY", qty=20)
        extra = self._create_product(sku="GS-BUNDLE-UPD-EXTRA", qty=20)
        listing = self.repo.create_listing(
            product_id=primary.id,
            marketplace="ebay",
            listing_title="Update bundle",
            listing_price=Decimal("50.00"),
            quantity_listed=5,
            marketplace_details=json.dumps(
                {
                    "bundle": {
                        "enabled": True,
                        "components": [
                            {"product_id": primary.id, "sku": primary.sku, "quantity_per_listing": 1},
                            {"product_id": extra.id, "sku": extra.sku, "quantity_per_listing": 2},
                        ],
                    }
                }
            ),
            actor="qa-user",
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("10.00"),
            shipping_cost=Decimal("5.00"),
            quantity_sold=1,
            product_id=primary.id,
            listing_id=listing.id,
            sold_at=datetime(2026, 3, 2, 15, 30, 0),
        )

        self.repo.update_sale(sale.id, {"quantity_sold": 3}, actor="qa-user")

        self.assertEqual(self.db.get(Product, primary.id).current_quantity, 17)
        self.assertEqual(self.db.get(Product, extra.id).current_quantity, 14)

    def test_report_sale_unit_cost_maps_prices_bundle_components_per_listing_unit(self) -> None:
        primary = self._create_product(sku="GS-BUNDLE-COGS-PRIMARY", qty=20)
        extra = self._create_product(sku="GS-BUNDLE-COGS-EXTRA", qty=20)
        listing = self.repo.create_listing(
            product_id=primary.id,
            marketplace="ebay",
            listing_title="COGS bundle",
            listing_price=Decimal("50.00"),
            quantity_listed=3,
            marketplace_details=json.dumps(
                {
                    "bundle": {
                        "enabled": True,
                        "components": [
                            {"product_id": primary.id, "sku": primary.sku, "quantity_per_listing": 2},
                            {"product_id": extra.id, "sku": extra.sku, "quantity_per_listing": 3},
                        ],
                    }
                }
            ),
            actor="qa-user",
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("10.00"),
            shipping_cost=Decimal("5.00"),
            quantity_sold=2,
            product_id=primary.id,
            listing_id=listing.id,
            sold_at=datetime(2026, 3, 2, 15, 30, 0),
        )

        cost_maps = self.repo.report_sale_unit_cost_maps(
            end_dt=datetime(2026, 3, 3, 0, 0, 0),
            default_unit_cost_by_product={primary.id: 25.0, extra.id: 25.0},
        )

        self.assertAlmostEqual(
            float(cost_maps["fifo_unit_cost_by_sale"][int(sale.id)]),
            125.0,
            places=2,
        )
        self.assertEqual(cost_maps["fifo_unit_cost_source_by_sale"][int(sale.id)], "product_default_landed_cost")

    def test_profit_basis_uses_inferred_single_product_lot_listing_quantity(self) -> None:
        product = self._create_product(sku="GS-INFERRED-LOT-COGS", qty=30)
        product.acquisition_cost = Decimal("3.00")
        self.db.commit()
        sold_at = datetime(2026, 6, 1, 22, 22, 4)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Lot of 20 Mercury Silver Dimes 90% Silver Coins",
            listing_price=Decimal("69.95"),
            quantity_listed=1,
            marketplace_details=json.dumps({"bundle": {"enabled": False, "components": []}}),
            actor="qa-user",
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("69.95"),
            fees=Decimal("7.22"),
            shipping_cost=Decimal("0.00"),
            shipping_label_cost=Decimal("6.87"),
            quantity_sold=1,
            product_id=product.id,
            listing_id=listing.id,
            sold_at=sold_at,
        )

        cost_maps = self.repo.report_sale_unit_cost_maps(
            end_dt=sold_at + timedelta(days=1),
            default_unit_cost_by_product={product.id: 3.0},
        )
        audit_rows = self.repo.dashboard_profit_basis_rows(now=sold_at + timedelta(days=1), limit=10)
        audit_row = next(row for row in audit_rows if int(row["sale_id"]) == int(sale.id))
        sales_rows = self.repo.report_sales_rows(start_dt=sold_at - timedelta(days=1), end_dt=sold_at + timedelta(days=1))
        sales_row = next(row for row in sales_rows if int(row["sale_id"]) == int(sale.id))

        self.assertAlmostEqual(float(cost_maps["fifo_unit_cost_by_sale"][int(sale.id)]), 60.0, places=2)
        self.assertAlmostEqual(float(cost_maps["fifo_total_cost_by_sale"][int(sale.id)]), 60.0, places=2)
        self.assertEqual(audit_row["quantity_sold"], 1)
        self.assertEqual(audit_row["fifo_cogs"], 60.0)
        self.assertTrue(audit_row["listing_is_bundle"])
        self.assertEqual(audit_row["listing_bundle_component_count"], 1)
        self.assertEqual(audit_row["listing_bundle_units_per_listing"], 20)
        self.assertEqual(audit_row["listing_bundle_inventory_units_sold"], 20)
        self.assertTrue(sales_row["listing_is_bundle"])
        self.assertEqual(sales_row["listing_bundle_kind"], "single_product_lot")
        self.assertEqual(sales_row["listing_bundle_units_per_listing"], 20)
        self.assertEqual(sales_row["listing_bundle_inventory_units_sold"], 20)

    def test_profit_basis_uses_inferred_full_tube_listing_quantity(self) -> None:
        product = self._create_product(sku="GS-INFERRED-TUBE-COGS", qty=30)
        product.acquisition_cost = Decimal("2.49")
        self.db.commit()
        sold_at = datetime(2026, 6, 9, 6, 48, 24)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Full Tube of 20 1 oz American Prospector Copper Tribute Coins Rounds Collectible",
            listing_price=Decimal("79.95"),
            quantity_listed=1,
            marketplace_details=json.dumps({"bundle": {"enabled": False, "components": []}}),
            actor="qa-user",
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("79.95"),
            fees=Decimal("8.63"),
            shipping_cost=Decimal("13.41"),
            shipping_label_cost=Decimal("7.90"),
            quantity_sold=1,
            product_id=product.id,
            listing_id=listing.id,
            sold_at=sold_at,
        )

        cost_maps = self.repo.report_sale_unit_cost_maps(
            end_dt=sold_at + timedelta(days=1),
            default_unit_cost_by_product={product.id: 2.49},
        )
        audit_row = next(
            row for row in self.repo.dashboard_profit_basis_rows(now=sold_at + timedelta(days=1), limit=10)
            if int(row["sale_id"]) == int(sale.id)
        )

        self.assertEqual(audit_row["quantity_sold"], 1)
        self.assertAlmostEqual(float(cost_maps["fifo_unit_cost_by_sale"][int(sale.id)]), 49.80, places=2)
        self.assertAlmostEqual(float(cost_maps["fifo_total_cost_by_sale"][int(sale.id)]), 49.80, places=2)
        self.assertEqual(audit_row["fifo_cogs"], 49.8)
        self.assertTrue(audit_row["listing_is_bundle"])
        self.assertEqual(audit_row["listing_bundle_units_per_listing"], 20)
        self.assertEqual(audit_row["listing_bundle_inventory_units_sold"], 20)
        self.assertEqual(audit_row["inventory_movement_units_expected"], 20)
        self.assertEqual(audit_row["inventory_movement_units_recorded"], 20)
        self.assertFalse(audit_row["listing_lot_movement_mismatch"])

    def test_bundle_sale_without_primary_product_link_uses_component_cogs(self) -> None:
        primary = self._create_product(sku="GS-BUNDLE-NOLINK-PRIMARY", qty=20)
        extra = self._create_product(sku="GS-BUNDLE-NOLINK-EXTRA", qty=20)
        listing = self.repo.create_listing(
            product_id=primary.id,
            marketplace="ebay",
            listing_title="No primary product link bundle",
            listing_price=Decimal("50.00"),
            quantity_listed=3,
            marketplace_details=json.dumps(
                {
                    "bundle": {
                        "enabled": True,
                        "kind": "mixed_product_bundle",
                        "components": [
                            {"product_id": primary.id, "sku": primary.sku, "quantity_per_listing": 2},
                            {"product_id": extra.id, "sku": extra.sku, "quantity_per_listing": 3},
                        ],
                    }
                }
            ),
            actor="qa-user",
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("10.00"),
            shipping_cost=Decimal("0.00"),
            shipping_label_cost=Decimal("0.00"),
            quantity_sold=1,
            product_id=None,
            listing_id=listing.id,
            external_order_id="EBAY-BUNDLE-NOLINK",
            sold_at=datetime(2026, 3, 2, 15, 30, 0),
        )

        cost_maps = self.repo.report_sale_unit_cost_maps(
            end_dt=datetime(2026, 3, 3, 0, 0, 0),
            default_unit_cost_by_product={primary.id: 10.0, extra.id: 5.0},
        )
        exceptions = self.repo.report_accounting_exception_rows(
            start_dt=datetime(2026, 3, 1, 0, 0, 0),
            end_dt=datetime(2026, 3, 3, 0, 0, 0),
        )
        sale_exceptions = [row for row in exceptions if int(row.get("entity_id") or 0) == int(sale.id)]

        self.assertAlmostEqual(float(cost_maps["fifo_unit_cost_by_sale"][int(sale.id)]), 35.0, places=2)
        self.assertEqual(cost_maps["fifo_unit_cost_source_by_sale"][int(sale.id)], "product_default_landed_cost")
        self.assertFalse([row for row in sale_exceptions if row.get("exception_type") == "missing_product_link"])
        self.assertFalse([row for row in sale_exceptions if row.get("exception_type") == "missing_cost_basis"])
        sale_rows = self.repo.report_sales_rows(
            start_dt=datetime(2026, 3, 1, 0, 0, 0),
            end_dt=datetime(2026, 3, 3, 0, 0, 0),
        )
        sale_row = next(row for row in sale_rows if int(row.get("sale_id") or 0) == int(sale.id))
        self.assertEqual(sale_row["product_id"], primary.id)
        self.assertEqual(sale_row["sku"], primary.sku)
        self.assertEqual(sale_row["product_title"], primary.title)

    def test_report_sales_rows_exposes_bundle_inventory_basis(self) -> None:
        primary = self._create_product(sku="GS-BUNDLE-REPORT-PRIMARY", qty=20)
        extra = self._create_product(sku="GS-BUNDLE-REPORT-EXTRA", qty=20)
        sold_at = datetime(2026, 3, 2, 15, 30, 0)
        listing = self.repo.create_listing(
            product_id=primary.id,
            marketplace="ebay",
            listing_title="Report bundle",
            listing_price=Decimal("50.00"),
            quantity_listed=3,
            marketplace_details=json.dumps(
                {
                    "bundle": {
                        "enabled": True,
                        "kind": "mixed_product_bundle",
                        "components": [
                            {"product_id": primary.id, "sku": primary.sku, "quantity_per_listing": 2},
                            {"product_id": extra.id, "sku": extra.sku, "quantity_per_listing": 3},
                        ],
                    }
                }
            ),
            actor="qa-user",
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("10.00"),
            shipping_cost=Decimal("5.00"),
            quantity_sold=2,
            product_id=primary.id,
            listing_id=listing.id,
            sold_at=sold_at,
        )

        rows = self.repo.report_sales_rows(
            start_dt=sold_at - timedelta(days=1),
            end_dt=sold_at + timedelta(days=1),
        )
        row = next(r for r in rows if int(r.get("sale_id") or 0) == int(sale.id))

        self.assertTrue(row["listing_is_bundle"])
        self.assertEqual(row["listing_bundle_kind"], "mixed_product_bundle")
        self.assertEqual(row["listing_bundle_component_count"], 2)
        self.assertEqual(row["listing_bundle_units_per_listing"], 5)
        self.assertEqual(row["listing_bundle_inventory_units_sold"], 10)

    def test_dashboard_live_metrics_profit_uses_bundle_component_cogs(self) -> None:
        primary = self._create_product(sku="GS-BUNDLE-DASH-PRIMARY", qty=20)
        extra = self._create_product(sku="GS-BUNDLE-DASH-EXTRA", qty=20)
        now = datetime(2026, 4, 12, 12, 0, 0)
        listing = self.repo.create_listing(
            product_id=primary.id,
            marketplace="ebay",
            listing_title="Dashboard bundle",
            listing_price=Decimal("150.00"),
            quantity_listed=3,
            marketplace_details=json.dumps(
                {
                    "bundle": {
                        "enabled": True,
                        "kind": "mixed_product_bundle",
                        "components": [
                            {"product_id": primary.id, "sku": primary.sku, "quantity_per_listing": 2},
                            {"product_id": extra.id, "sku": extra.sku, "quantity_per_listing": 3},
                        ],
                    }
                }
            ),
            actor="qa-user",
        )
        self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("300.00"),
            fees=Decimal("30.00"),
            shipping_cost=Decimal("10.00"),
            shipping_label_cost=Decimal("5.00"),
            quantity_sold=2,
            product_id=primary.id,
            listing_id=listing.id,
            sold_at=now - timedelta(days=1),
        )

        metrics = self.repo.dashboard_live_metrics(now=now, include_fee_type_breakdown=False)

        self.assertAlmostEqual(metrics["sales_30d_est_cogs"], 250.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_est_profit"], 25.0, places=2)
        self.assertEqual(metrics["sales_30d_bundle_sale_count"], 1)
        self.assertEqual(metrics["sales_30d_bundle_inventory_units_sold"], 10)

    def test_multi_product_bundle_uses_component_fifo_lot_costs(self) -> None:
        primary = self._create_product(sku="GS-BUNDLE-FIFO-PRIMARY", qty=20)
        extra = self._create_product(sku="GS-BUNDLE-FIFO-EXTRA", qty=20)
        now = datetime(2026, 4, 12, 12, 0, 0)
        primary_lot = self.repo.create_purchase_lot(
            lot_code="LOT-BUNDLE-FIFO-PRIMARY",
            vendor="Dealer",
            purchase_date=now - timedelta(days=4),
            total_cost=Decimal("120.00"),
        )
        extra_lot = self.repo.create_purchase_lot(
            lot_code="LOT-BUNDLE-FIFO-EXTRA",
            vendor="Dealer",
            purchase_date=now - timedelta(days=4),
            total_cost=Decimal("40.00"),
        )
        self.repo.assign_product_to_lot(
            product_id=primary.id,
            lot_id=primary_lot.id,
            quantity_acquired=10,
            unit_cost=Decimal("12.00"),
            acquired_at=now - timedelta(days=4),
        )
        self.repo.assign_product_to_lot(
            product_id=extra.id,
            lot_id=extra_lot.id,
            quantity_acquired=10,
            unit_cost=Decimal("4.00"),
            acquired_at=now - timedelta(days=4),
        )
        listing = self.repo.create_listing(
            product_id=primary.id,
            marketplace="ebay",
            listing_title="Two product FIFO bundle",
            listing_price=Decimal("150.00"),
            quantity_listed=3,
            marketplace_details=json.dumps(
                {
                    "bundle": {
                        "enabled": True,
                        "kind": "mixed_product_bundle",
                        "components": [
                            {"product_id": primary.id, "sku": primary.sku, "quantity_per_listing": 2},
                            {"product_id": extra.id, "sku": extra.sku, "quantity_per_listing": 3},
                        ],
                    }
                }
            ),
            actor="qa-user",
        )

        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("300.00"),
            fees=Decimal("30.00"),
            shipping_cost=Decimal("10.00"),
            shipping_label_cost=Decimal("5.00"),
            quantity_sold=2,
            product_id=primary.id,
            listing_id=listing.id,
            sold_at=now - timedelta(days=1),
        )

        self.assertEqual(self.db.get(Product, primary.id).current_quantity, 16)
        self.assertEqual(self.db.get(Product, extra.id).current_quantity, 14)
        movements = [
            m for m in self.repo.list_inventory_movements(limit=100)
            if m.reference_type == "sale" and int(m.reference_id or 0) == int(sale.id)
        ]
        self.assertEqual(sorted((m.product_id, m.quantity_delta) for m in movements), [(primary.id, -4), (extra.id, -6)])

        cost_maps = self.repo.report_sale_unit_cost_maps(
            end_dt=now,
            default_unit_cost_by_product={primary.id: 0.0, extra.id: 0.0},
        )
        evidence = cost_maps["fifo_cogs_evidence_by_sale"][sale.id]
        by_product = {int(row["product_id"]): row for row in evidence}
        self.assertAlmostEqual(cost_maps["fifo_total_cost_by_sale"][sale.id], 72.0, places=2)
        self.assertAlmostEqual(cost_maps["fifo_unit_cost_by_sale"][sale.id], 36.0, places=2)
        self.assertEqual(cost_maps["fifo_unit_cost_source_by_sale"][sale.id], "assignment_unit_landed_cost")
        self.assertEqual(by_product[primary.id]["quantity"], 4)
        self.assertAlmostEqual(by_product[primary.id]["total_cost"], 48.0, places=2)
        self.assertEqual(by_product[extra.id]["quantity"], 6)
        self.assertAlmostEqual(by_product[extra.id]["total_cost"], 24.0, places=2)

        audit_row = next(
            row for row in self.repo.dashboard_profit_basis_rows(now=now, limit=10)
            if int(row["sale_id"]) == int(sale.id)
        )
        self.assertEqual(audit_row["quantity_sold"], 2)
        self.assertEqual(audit_row["listing_bundle_component_count"], 2)
        self.assertEqual(audit_row["listing_bundle_units_per_listing"], 5)
        self.assertEqual(audit_row["listing_bundle_inventory_units_sold"], 10)
        self.assertEqual(audit_row["inventory_movement_units_expected"], 10)
        self.assertEqual(audit_row["inventory_movement_units_recorded"], 10)
        self.assertAlmostEqual(audit_row["fifo_cogs"], 72.0, places=2)
        self.assertFalse(audit_row["basis_review_required"])
        self.assertEqual(audit_row["cogs_basis_bucket"], "ok")

        metrics = self.repo.dashboard_live_metrics(now=now, include_fee_type_breakdown=False)
        self.assertAlmostEqual(metrics["sales_30d_est_cogs"], 72.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_profit_before_returns"], 203.0, places=2)
        self.assertEqual(metrics["sales_30d_bundle_sale_count"], 1)
        self.assertEqual(metrics["sales_30d_bundle_inventory_units_sold"], 10)
        self.assertAlmostEqual(metrics["sales_30d_cogs_verified_amount"], 72.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_cogs_review_amount"], 0.0, places=2)

    def test_dashboard_live_metrics_profit_applies_return_impact(self) -> None:
        product = self._create_product(sku="GS-DASH-RETURN-PROFIT", qty=3)
        now = datetime(2026, 4, 12, 12, 0, 0)
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("10.00"),
            shipping_cost=Decimal("5.00"),
            shipping_label_cost=Decimal("4.00"),
            quantity_sold=1,
            product_id=product.id,
            sold_at=now - timedelta(days=1),
        )
        self.repo.create_return(
            marketplace="ebay",
            sale_id=sale.id,
            quantity=1,
            refund_amount=Decimal("100.00"),
            return_status="processed",
            disposition="restock",
            restocked=True,
            returned_at=now - timedelta(hours=12),
            actor="qa-user",
        )

        metrics = self.repo.dashboard_live_metrics(now=now, include_fee_type_breakdown=False)

        self.assertAlmostEqual(metrics["sales_30d_net"], 91.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_est_cogs"], 25.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_profit_before_returns"], 66.0, places=2)
        self.assertEqual(metrics["returns_30d_count"], 1)
        self.assertAlmostEqual(metrics["returns_30d_refund_total"], 100.0, places=2)
        self.assertAlmostEqual(metrics["returns_30d_cogs_reversal"], 25.0, places=2)
        self.assertAlmostEqual(metrics["returns_30d_profit_impact"], -75.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_net_after_returns"], -9.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_est_profit"], -9.0, places=2)

    def test_dashboard_return_impact_uses_bundle_cogs_without_sale_product_link(self) -> None:
        primary = self._create_product(sku="GS-DASH-BUNDLE-RETURN-PRIMARY", qty=20)
        extra = self._create_product(sku="GS-DASH-BUNDLE-RETURN-EXTRA", qty=20)
        now = datetime(2026, 4, 12, 12, 0, 0)
        listing = self.repo.create_listing(
            product_id=primary.id,
            marketplace="ebay",
            listing_title="Dashboard bundle return",
            listing_price=Decimal("150.00"),
            quantity_listed=3,
            marketplace_details=json.dumps(
                {
                    "bundle": {
                        "enabled": True,
                        "kind": "mixed_product_bundle",
                        "components": [
                            {"product_id": primary.id, "sku": primary.sku, "quantity_per_listing": 2},
                            {"product_id": extra.id, "sku": extra.sku, "quantity_per_listing": 3},
                        ],
                    }
                }
            ),
            actor="qa-user",
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("300.00"),
            fees=Decimal("30.00"),
            shipping_cost=Decimal("10.00"),
            shipping_label_cost=Decimal("5.00"),
            quantity_sold=2,
            product_id=None,
            listing_id=listing.id,
            sold_at=now - timedelta(days=1),
        )
        self.repo.create_return(
            marketplace="ebay",
            sale_id=sale.id,
            quantity=1,
            refund_amount=Decimal("150.00"),
            return_status="processed",
            disposition="restock",
            restocked=True,
            returned_at=now - timedelta(hours=12),
            actor="qa-user",
        )

        metrics = self.repo.dashboard_live_metrics(now=now, include_fee_type_breakdown=False)

        self.assertAlmostEqual(metrics["sales_30d_est_cogs"], 250.0, places=2)
        self.assertAlmostEqual(metrics["returns_30d_refund_total"], 150.0, places=2)
        self.assertAlmostEqual(metrics["returns_30d_cogs_reversal"], 125.0, places=2)
        self.assertAlmostEqual(metrics["returns_30d_profit_impact"], -25.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_est_profit"], 0.0, places=2)

    def test_bundle_return_reverses_one_listing_unit_of_component_fifo_cogs(self) -> None:
        primary = self._create_product(sku="GS-BUNDLE-RET-FIFO-PRIMARY", qty=20)
        extra = self._create_product(sku="GS-BUNDLE-RET-FIFO-EXTRA", qty=20)
        now = datetime(2026, 4, 12, 12, 0, 0)
        primary_lot = self.repo.create_purchase_lot(
            lot_code="LOT-BUNDLE-RET-FIFO-PRIMARY",
            vendor="Dealer",
            purchase_date=now - timedelta(days=4),
            total_cost=Decimal("120.00"),
        )
        extra_lot = self.repo.create_purchase_lot(
            lot_code="LOT-BUNDLE-RET-FIFO-EXTRA",
            vendor="Dealer",
            purchase_date=now - timedelta(days=4),
            total_cost=Decimal("40.00"),
        )
        self.repo.assign_product_to_lot(
            product_id=primary.id,
            lot_id=primary_lot.id,
            quantity_acquired=10,
            unit_cost=Decimal("12.00"),
            acquired_at=now - timedelta(days=4),
        )
        self.repo.assign_product_to_lot(
            product_id=extra.id,
            lot_id=extra_lot.id,
            quantity_acquired=10,
            unit_cost=Decimal("4.00"),
            acquired_at=now - timedelta(days=4),
        )
        listing = self.repo.create_listing(
            product_id=primary.id,
            marketplace="ebay",
            listing_title="Return FIFO bundle",
            listing_price=Decimal("150.00"),
            quantity_listed=3,
            marketplace_details=json.dumps(
                {
                    "bundle": {
                        "enabled": True,
                        "kind": "mixed_product_bundle",
                        "components": [
                            {"product_id": primary.id, "sku": primary.sku, "quantity_per_listing": 2},
                            {"product_id": extra.id, "sku": extra.sku, "quantity_per_listing": 3},
                        ],
                    }
                }
            ),
            actor="qa-user",
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("300.00"),
            fees=Decimal("30.00"),
            shipping_cost=Decimal("10.00"),
            shipping_label_cost=Decimal("5.00"),
            quantity_sold=2,
            product_id=primary.id,
            listing_id=listing.id,
            sold_at=now - timedelta(days=1),
        )
        self.repo.create_return(
            marketplace="ebay",
            sale_id=sale.id,
            quantity=1,
            refund_amount=Decimal("150.00"),
            return_status="processed",
            disposition="restock",
            restocked=True,
            returned_at=now - timedelta(hours=12),
            actor="qa-user",
        )

        metrics = self.repo.dashboard_live_metrics(now=now, include_fee_type_breakdown=False)

        self.assertAlmostEqual(metrics["sales_30d_est_cogs"], 72.0, places=2)
        self.assertAlmostEqual(metrics["returns_30d_cogs_reversal"], 36.0, places=2)
        self.assertAlmostEqual(metrics["returns_30d_profit_impact"], -114.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_est_profit"], 89.0, places=2)
        self.assertEqual(self.db.get(Product, primary.id).current_quantity, 18)
        self.assertEqual(self.db.get(Product, extra.id).current_quantity, 17)

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

    def test_create_order_links_customer_and_marks_repeat_buyer(self) -> None:
        p1 = self._create_product(sku="GS-CUST-001", qty=10)
        first = self.repo.create_order(
            marketplace="ebay",
            external_order_id="EBAY-CUST-1",
            order_status="paid",
            sold_at=datetime(2026, 3, 23, 10, 0, 0),
            buyer_username="coinbuyer42",
            buyer_name="Coin Buyer",
            buyer_email="coinbuyer@example.com",
            ship_to_city="Golden",
            ship_to_state="CO",
            ship_to_postal_code="80401",
            ship_to_country="US",
            marketplace_payload_json=json.dumps(
                {
                    "fulfillmentStartInstructions": [
                        {
                            "shippingStep": {
                                "shipTo": {
                                    "fullName": "Coin Buyer",
                                    "contactAddress": {
                                        "addressLine1": "15892 W 1st Dr",
                                        "city": "Golden",
                                        "stateOrProvince": "CO",
                                        "postalCode": "80401",
                                        "countryCode": "US",
                                    },
                                }
                            }
                        }
                    ]
                }
            ),
            items=[{"product_id": p1.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("30.00")}],
            actor="qa-user",
        )
        second = self.repo.create_order(
            marketplace="ebay",
            external_order_id="EBAY-CUST-2",
            order_status="paid",
            sold_at=datetime(2026, 3, 24, 10, 0, 0),
            buyer_username="coinbuyer42",
            buyer_name="Coin Buyer",
            buyer_email="coinbuyer@example.com",
            ship_to_city="Golden",
            ship_to_state="CO",
            ship_to_postal_code="80401",
            ship_to_country="US",
            items=[{"product_id": p1.id, "listing_id": None, "quantity": 2, "unit_price": Decimal("25.00")}],
            actor="qa-user",
        )

        self.assertEqual(first.customer_id, second.customer_id)
        customer = self.db.get(Customer, int(second.customer_id))
        self.assertIsNotNone(customer)
        self.assertEqual(customer.customer_key, "username:coinbuyer42")
        self.assertEqual(customer.order_count, 2)
        self.assertTrue(customer.is_repeat_buyer)
        self.assertEqual(float(customer.total_spend), 80.0)
        self.assertEqual(customer.shipping_address_line1, "15892 W 1st Dr")

    def test_backfill_customers_from_existing_orders_links_legacy_rows(self) -> None:
        p1 = self._create_product(sku="GS-CUST-BACKFILL-001", qty=10)
        first = self.repo.create_order(
            marketplace="ebay",
            external_order_id="EBAY-CUST-BACKFILL-1",
            order_status="paid",
            sold_at=datetime(2026, 3, 23, 10, 0, 0),
            buyer_username="legacybuyer",
            buyer_name="Legacy Buyer",
            ship_to_postal_code="80401",
            items=[{"product_id": p1.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("10.00")}],
            actor="qa-user",
        )
        second = self.repo.create_order(
            marketplace="ebay",
            external_order_id="EBAY-CUST-BACKFILL-2",
            order_status="paid",
            sold_at=datetime(2026, 3, 24, 10, 0, 0),
            buyer_username="legacybuyer",
            buyer_name="Legacy Buyer",
            ship_to_postal_code="80401",
            items=[{"product_id": p1.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("15.00")}],
            actor="qa-user",
        )
        self.db.query(type(first)).update({"customer_id": None})
        self.db.query(Customer).delete()
        self.db.commit()

        result = self.repo.backfill_customers_from_orders(actor="qa-user")

        self.assertEqual(result["created_customers"], 1)
        self.assertEqual(result["linked_orders"], 2)
        refreshed_first = self.db.get(type(first), first.id)
        refreshed_second = self.db.get(type(second), second.id)
        self.assertEqual(refreshed_first.customer_id, refreshed_second.customer_id)
        customer = self.db.get(Customer, int(refreshed_first.customer_id))
        self.assertEqual(customer.order_count, 2)
        self.assertTrue(customer.is_repeat_buyer)

    def test_backfill_ebay_order_listing_links_repairs_blank_sale_and_order_item_links(self) -> None:
        product = self._create_product(sku="GS-EBAY-LINK-BACKFILL-001", qty=30)
        product.acquisition_cost = Decimal("2.49")
        self.db.commit()
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Full Tube of 20 1 oz American Prospector Copper Tribute Coins",
            listing_price=Decimal("79.95"),
            quantity_listed=1,
            external_listing_id="137394544357",
            actor="qa-user",
        )
        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="EBAY-LINK-BACKFILL-1",
            order_status="paid",
            sold_at=datetime(2026, 6, 9, 7, 2, 21),
            marketplace_payload_json=json.dumps(
                {
                    "lineItems": [
                        {
                            "lineItemId": "LI-1",
                            "legacyItemId": "137394544357",
                            "sku": "GS-EBAY-LINK-BACKFILL-001-L206",
                            "quantity": 1,
                            "lineItemCost": {"value": "79.95"},
                            "title": "Full Tube of 20 1 oz American Prospector Copper Tribute Coins",
                        }
                    ]
                }
            ),
            items=[{"product_id": None, "listing_id": None, "quantity": 1, "unit_price": Decimal("79.95")}],
            actor="qa-user",
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("79.95"),
            fees=Decimal("7.00"),
            shipping_cost=Decimal("0.00"),
            quantity_sold=1,
            order_id=order.id,
            product_id=None,
            listing_id=None,
            external_order_id=order.external_order_id,
            sold_at=order.sold_at,
            actor="qa-user",
        )
        self.assertEqual(self.db.get(Product, product.id).current_quantity, 30)

        preview = self.repo.backfill_ebay_order_listing_links(actor="qa-user", dry_run=True)
        self.assertEqual(preview["order_items_backfilled"], 1)
        self.assertEqual(preview["sales_backfilled"], 1)
        self.assertIsNone(self.db.get(Sale, sale.id).listing_id)

        result = self.repo.backfill_ebay_order_listing_links(actor="qa-user")

        self.assertEqual(result["order_items_backfilled"], 1)
        self.assertEqual(result["sales_backfilled"], 1)
        refreshed_order_item = next(item for item in self.repo.list_order_items() if item.order_id == order.id)
        self.assertEqual(refreshed_order_item.listing_id, listing.id)
        self.assertEqual(refreshed_order_item.product_id, product.id)
        refreshed_sale = self.db.get(Sale, sale.id)
        self.assertEqual(refreshed_sale.listing_id, listing.id)
        self.assertEqual(refreshed_sale.product_id, product.id)
        self.assertEqual(self.db.get(Product, product.id).current_quantity, 10)
        profit_basis = next(
            row for row in self.repo.dashboard_profit_basis_rows(now=datetime(2026, 6, 10), limit=10)
            if int(row["sale_id"]) == int(sale.id)
        )
        self.assertEqual(profit_basis["sku"], product.sku)
        self.assertEqual(profit_basis["product_id"], product.id)
        self.assertTrue(profit_basis["listing_is_bundle"])
        self.assertEqual(profit_basis["listing_bundle_units_per_listing"], 20)
        self.assertEqual(profit_basis["listing_bundle_inventory_units_sold"], 20)
        self.assertEqual(profit_basis["inventory_movement_units_expected"], 20)
        self.assertEqual(profit_basis["inventory_movement_units_recorded"], 20)
        self.assertFalse(profit_basis["listing_lot_movement_mismatch"])

    def test_update_customer_notes_records_audit(self) -> None:
        product = self._create_product(sku="GS-CUST-NOTES-001", qty=10)
        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="EBAY-CUST-NOTES-1",
            order_status="paid",
            sold_at=datetime(2026, 3, 23, 10, 0, 0),
            buyer_username="notebuyer",
            items=[{"product_id": product.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("10.00")}],
            actor="qa-user",
        )

        updated = self.repo.update_customer(
            int(order.customer_id),
            {"notes": "Prefers combined shipping. Asked about Morgan dollars."},
            actor="qa-user",
        )

        self.assertEqual(updated.notes, "Prefers combined shipping. Asked about Morgan dollars.")
        audit = [
            row
            for row in self.repo.list_audit_logs(limit=50)
            if row.entity_type == "customer"
            and row.entity_id == int(order.customer_id)
            and row.action == "update"
        ]
        self.assertTrue(audit)
        self.assertIn("notes", audit[0].changes_json)

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

    def test_dashboard_live_metrics_rollup(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-DASH-001", qty=25)
        self.repo.update_product(
            product.id,
            {"acquisition_cost": Decimal("20.00")},
            actor="qa-user",
        )
        sale_recent = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("10.00"),
            shipping_cost=Decimal("5.00"),
            quantity_sold=2,
            product_id=product.id,
            sold_at=now - timedelta(days=1),
        )
        self.repo.update_sale(
            sale_recent.id,
            {"shipping_label_cost": Decimal("4.00")},
            actor="qa-user",
        )
        sale_older = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("80.00"),
            fees=Decimal("8.00"),
            shipping_cost=Decimal("4.00"),
            quantity_sold=1,
            product_id=product.id,
            sold_at=now - timedelta(days=20),
        )
        self.repo.update_sale(
            sale_older.id,
            {"shipping_label_cost": Decimal("3.00")},
            actor="qa-user",
        )
        self.repo.create_order(
            marketplace="ebay",
            external_order_id="DASH-ORD-1",
            order_status="shipped",
            sold_at=now - timedelta(days=3),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("0.00"),
            items=[{"product_id": product.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("10.00")}],
            actor="qa-user",
        )
        self.repo.create_order(
            marketplace="ebay",
            external_order_id="DASH-ORD-2",
            order_status="paid",
            sold_at=now - timedelta(days=2),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("0.00"),
            items=[{"product_id": product.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("11.00")}],
            actor="qa-user",
        )

        metrics = self.repo.dashboard_live_metrics(now=now)
        self.assertEqual(metrics["sales_7d_count"], 1)
        self.assertEqual(metrics["sales_30d_count"], 2)
        self.assertAlmostEqual(metrics["sales_30d_gross"], 180.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_net"], 164.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_shipping_charged"], 9.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_shipping_label_spend"], 7.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_shipping_delta"], 2.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_est_cogs"], 60.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_est_profit"], 104.0, places=2)
        self.assertEqual(metrics["orders_7d_count"], 2)
        self.assertEqual(metrics["orders_30d_count"], 2)
        self.assertEqual(metrics["orders_30d_shipped"], 1)
        self.assertEqual(metrics["orders_30d_not_shipped"], 1)
        self.assertAlmostEqual(metrics["ebay_fees_30d_total"], 18.0, places=2)
        self.assertEqual(metrics["ebay_fee_type_breakdown_30d"], {})

    def test_dashboard_live_metrics_ignores_unlinked_normalized_label_spend_rows(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-DASH-002", qty=25)
        self.repo.update_product(
            product.id,
            {"acquisition_cost": Decimal("20.00")},
            actor="qa-user",
        )
        sale_recent = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("10.00"),
            shipping_cost=Decimal("5.00"),
            quantity_sold=1,
            product_id=product.id,
            sold_at=now - timedelta(days=1),
        )
        self.repo.update_sale(
            sale_recent.id,
            {"shipping_label_cost": Decimal("4.00")},
            actor="qa-user",
        )
        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="DASH-ORD-NORM-1",
            order_status="shipped",
            sold_at=now - timedelta(days=1),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("5.00"),
            items=[{"product_id": product.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("10.00")}],
            actor="qa-user",
        )
        self.repo.replace_order_finance_entries(
            order.id,
            [
                {
                    "marketplace": "ebay",
                    "external_order_id": order.external_order_id,
                    "transaction_id": "TX-SALE-1",
                    "line_item_id": "",
                    "legacy_item_id": "",
                    "sku": product.sku,
                    "entry_kind": "marketplace_fee",
                    "fee_type": "FINAL_VALUE_FEE",
                    "amount": Decimal("7.00"),
                    "currency": "USD",
                    "booking_entry": "DEBIT",
                    "transaction_type": "SALE",
                    "transaction_status": "FUNDS_AVAILABLE_FOR_PAYOUT",
                    "transaction_date": now - timedelta(days=1),
                    "memo": "",
                    "source": "finance_transactions_orderLineItems",
                    "raw": {},
                },
                {
                    "marketplace": "ebay",
                    "external_order_id": order.external_order_id,
                    "transaction_id": "TX-LABEL-1",
                    "entry_kind": "shipping_label",
                    "fee_type": "SHIPPING_LABEL",
                    "amount": Decimal("8.97"),
                    "currency": "USD",
                    "transaction_type": "SHIPPING_LABEL",
                    "transaction_status": "FUNDS_AVAILABLE_FOR_PAYOUT",
                    "transaction_date": now - timedelta(days=1),
                    "source": "finance_transactions",
                    "raw": {},
                }
            ],
            actor="qa-user",
        )

        metrics = self.repo.dashboard_live_metrics(now=now)
        self.assertAlmostEqual(metrics["sales_30d_shipping_charged"], 5.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_shipping_label_spend"], 4.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_shipping_delta"], 1.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_net"], 91.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_est_profit"], 71.0, places=2)
        self.assertAlmostEqual(metrics["ebay_fees_30d_total"], 10.0, places=2)
        self.assertEqual(metrics["ebay_fee_type_breakdown_30d"], {})

    def test_dashboard_and_shipping_report_use_linked_normalized_label_spend_once(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-DASH-NORM-LINK", qty=25)
        self.repo.update_product(product.id, {"acquisition_cost": Decimal("20.00")}, actor="qa-user")
        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="DASH-ORD-NORM-LINK",
            order_status="shipped",
            sold_at=now - timedelta(days=1),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("5.00"),
            items=[{"product_id": product.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("100.00")}],
            actor="qa-user",
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("10.00"),
            shipping_cost=Decimal("5.00"),
            shipping_label_cost=Decimal("4.00"),
            quantity_sold=1,
            product_id=product.id,
            order_id=order.id,
            external_order_id=order.external_order_id,
            sold_at=now - timedelta(days=1),
        )
        self.repo.replace_order_finance_entries(
            order.id,
            [
                {
                    "marketplace": "ebay",
                    "external_order_id": order.external_order_id,
                    "transaction_id": "TX-LABEL-LINK",
                    "entry_kind": "shipping_label",
                    "fee_type": "SHIPPING_LABEL",
                    "amount": Decimal("8.97"),
                    "currency": "USD",
                    "transaction_type": "SHIPPING_LABEL",
                    "transaction_status": "FUNDS_AVAILABLE_FOR_PAYOUT",
                    "transaction_date": now - timedelta(days=1),
                    "source": "finance_transactions",
                    "raw": {},
                }
            ],
            actor="qa-user",
        )

        metrics = self.repo.dashboard_live_metrics(now=now)
        self.assertAlmostEqual(metrics["sales_30d_shipping_charged"], 5.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_shipping_label_spend"], 8.97, places=2)
        self.assertAlmostEqual(metrics["sales_30d_shipping_delta"], -3.97, places=2)
        self.assertAlmostEqual(metrics["sales_30d_net"], 86.03, places=2)
        self.assertAlmostEqual(metrics["sales_30d_est_profit"], 66.03, places=2)

        rows = self.repo.report_shipping_economics_rows(start_dt=now - timedelta(days=2), end_dt=now)
        row = next(r for r in rows if int(r.get("sale_id") or 0) == int(sale.id))
        self.assertAlmostEqual(float(row.get("shipping_label_spend") or 0.0), 8.97, places=2)
        self.assertEqual(row.get("shipping_label_spend_source"), "normalized_order_finance_entries_shipping_label_sum")

    def test_dashboard_profit_uses_linked_normalized_fee_and_label_actuals(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-DASH-NORM-ACTUALS", qty=25)
        self.repo.update_product(product.id, {"acquisition_cost": Decimal("20.00")}, actor="qa-user")
        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="DASH-ORD-NORM-ACTUALS",
            order_status="shipped",
            sold_at=now - timedelta(days=1),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("5.00"),
            items=[{"product_id": product.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("100.00")}],
            actor="qa-user",
        )
        self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("10.00"),
            shipping_cost=Decimal("5.00"),
            shipping_label_cost=Decimal("9.00"),
            quantity_sold=1,
            product_id=product.id,
            order_id=order.id,
            external_order_id=order.external_order_id,
            sold_at=now - timedelta(days=1),
        )
        self.repo.replace_order_finance_entries(
            order.id,
            [
                {
                    "marketplace": "ebay",
                    "external_order_id": order.external_order_id,
                    "transaction_id": "TX-FEE-ACTUALS",
                    "entry_kind": "marketplace_fee",
                    "fee_type": "FINAL_VALUE_FEE",
                    "amount": Decimal("7.50"),
                    "currency": "USD",
                    "transaction_type": "SALE",
                    "transaction_status": "FUNDS_AVAILABLE_FOR_PAYOUT",
                    "transaction_date": now - timedelta(days=1),
                    "source": "finance_transactions_orderLineItems",
                    "raw": {},
                },
                {
                    "marketplace": "ebay",
                    "external_order_id": order.external_order_id,
                    "transaction_id": "TX-LABEL-ACTUALS",
                    "entry_kind": "shipping_label",
                    "fee_type": "SHIPPING_LABEL",
                    "amount": Decimal("4.25"),
                    "currency": "USD",
                    "transaction_type": "SHIPPING_LABEL",
                    "transaction_status": "FUNDS_AVAILABLE_FOR_PAYOUT",
                    "transaction_date": now - timedelta(days=1),
                    "source": "finance_transactions",
                    "raw": {},
                },
            ],
            actor="qa-user",
        )

        metrics = self.repo.dashboard_live_metrics(now=now)

        self.assertAlmostEqual(metrics["sales_30d_shipping_charged"], 5.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_shipping_label_spend"], 4.25, places=2)
        self.assertAlmostEqual(metrics["ebay_fees_30d_total"], 7.5, places=2)
        self.assertAlmostEqual(metrics["ebay_fee_type_breakdown_30d"]["FINAL_VALUE_FEE"], 7.5, places=2)
        self.assertAlmostEqual(metrics["sales_7d_net"], 93.25, places=2)
        self.assertAlmostEqual(metrics["sales_30d_net"], 93.25, places=2)
        self.assertAlmostEqual(metrics["sales_30d_est_profit"], 73.25, places=2)

    def test_report_shipping_economics_rows_and_summary(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-SHIP-001", qty=10)
        sale_a = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("50.00"),
            fees=Decimal("5.00"),
            shipping_cost=Decimal("6.00"),
            quantity_sold=1,
            product_id=product.id,
            sold_at=now - timedelta(days=2),
        )
        self.repo.update_sale(sale_a.id, {"shipping_label_cost": Decimal("4.50")}, actor="qa-user")
        sale_b = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("60.00"),
            fees=Decimal("6.00"),
            shipping_cost=Decimal("7.00"),
            quantity_sold=1,
            product_id=product.id,
            sold_at=now - timedelta(days=1),
        )
        self.repo.update_sale(sale_b.id, {"shipping_label_cost": Decimal("0.00")}, actor="qa-user")
        sale_c = self.repo.create_sale(
            marketplace="local",
            sold_price=Decimal("30.00"),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("0.00"),
            quantity_sold=1,
            product_id=product.id,
            sold_at=now - timedelta(days=3),
        )
        self.repo.update_sale(sale_c.id, {"shipping_label_cost": Decimal("0.00")}, actor="qa-user")

        rows = self.repo.report_shipping_economics_rows(
            start_dt=now - timedelta(days=30),
            end_dt=now,
            marketplaces={"ebay"},
        )
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(str(r.get("marketplace") or "") == "ebay" for r in rows))
        self.assertAlmostEqual(sum(float(r.get("shipping_charged_to_buyer") or 0.0) for r in rows), 13.0, places=2)
        self.assertAlmostEqual(sum(float(r.get("shipping_label_spend") or 0.0) for r in rows), 4.5, places=2)

        summary = self.repo.report_shipping_economics_summary(
            start_dt=now - timedelta(days=30),
            end_dt=now,
            marketplaces={"ebay"},
        )
        self.assertEqual(len(summary), 1)
        row = summary[0]
        self.assertEqual(row.get("marketplace"), "ebay")
        self.assertEqual(int(row.get("sales_count") or 0), 2)
        self.assertAlmostEqual(float(row.get("total_shipping_charged") or 0.0), 13.0, places=2)
        self.assertAlmostEqual(float(row.get("total_label_spend") or 0.0), 4.5, places=2)
        self.assertAlmostEqual(float(row.get("shipping_delta_charged_minus_spend") or 0.0), 8.5, places=2)
        self.assertEqual(int(row.get("label_spend_covered_count") or 0), 1)

    def test_report_shipping_economics_uses_listing_product_for_bundle_sale_without_product_link(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-SHIP-BUNDLE-LISTING", qty=5)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Shipping bundle listing",
            listing_price=Decimal("50.00"),
            quantity_listed=1,
            marketplace_details=json.dumps(
                {
                    "bundle": {
                        "enabled": True,
                        "kind": "single_product_lot",
                        "components": [
                            {"product_id": product.id, "sku": product.sku, "quantity_per_listing": 2},
                        ],
                    }
                }
            ),
            actor="qa-user",
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("50.00"),
            fees=Decimal("5.00"),
            shipping_cost=Decimal("6.00"),
            shipping_label_cost=Decimal("4.00"),
            quantity_sold=1,
            product_id=None,
            listing_id=listing.id,
            external_order_id="EBAY-SHIP-BUNDLE-LISTING",
            sold_at=now - timedelta(days=1),
        )

        rows = self.repo.report_shipping_economics_rows(start_dt=now - timedelta(days=7), end_dt=now)
        row = next(r for r in rows if int(r.get("sale_id") or 0) == int(sale.id))

        self.assertEqual(row["sku"], product.sku)
        self.assertEqual(row["product_title"], product.title)
        self.assertAlmostEqual(float(row.get("shipping_delta_charged_minus_spend") or 0.0), 2.0, places=2)

    def test_report_shipping_economics_uses_actual_econ_weighted_order_allocation(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product_a = self._create_product(sku="GS-SHIP-WEIGHT-A", qty=10)
        product_b = self._create_product(sku="GS-SHIP-WEIGHT-B", qty=10)
        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="SHIP-WEIGHTED-ALLOC",
            order_status="shipped",
            sold_at=now - timedelta(days=1),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("10.00"),
            shipping_label_cost=Decimal("12.00"),
            items=[
                {"product_id": product_a.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("75.00")},
                {"product_id": product_b.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("25.00")},
            ],
            actor="qa-user",
        )
        sale_a = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("75.00"),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("0.00"),
            shipping_label_cost=Decimal("0.00"),
            quantity_sold=1,
            product_id=product_a.id,
            order_id=order.id,
            external_order_id=order.external_order_id,
            sold_at=now - timedelta(days=1),
        )
        sale_b = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("25.00"),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("0.00"),
            shipping_label_cost=Decimal("0.00"),
            quantity_sold=1,
            product_id=product_b.id,
            order_id=order.id,
            external_order_id=order.external_order_id,
            sold_at=now - timedelta(days=1),
        )
        self.repo.replace_order_finance_entries(
            order.id,
            [{"entry_kind": "shipping_label", "fee_type": "SHIPPING_LABEL", "amount": Decimal("8.00")}],
            actor="qa-user",
        )

        rows = self.repo.report_shipping_economics_rows(start_dt=now - timedelta(days=7), end_dt=now)
        by_sale_id = {int(row.get("sale_id") or 0): row for row in rows}

        self.assertAlmostEqual(float(by_sale_id[sale_a.id].get("shipping_charged_to_buyer") or 0.0), 7.5, places=2)
        self.assertAlmostEqual(float(by_sale_id[sale_a.id].get("shipping_label_spend") or 0.0), 6.0, places=2)
        self.assertAlmostEqual(
            float(by_sale_id[sale_a.id].get("shipping_delta_charged_minus_spend") or 0.0),
            1.5,
            places=2,
        )
        self.assertAlmostEqual(float(by_sale_id[sale_b.id].get("shipping_charged_to_buyer") or 0.0), 2.5, places=2)
        self.assertAlmostEqual(float(by_sale_id[sale_b.id].get("shipping_label_spend") or 0.0), 2.0, places=2)
        self.assertEqual(
            by_sale_id[sale_a.id].get("shipping_label_spend_source"),
            "normalized_order_finance_entries_shipping_label_sum",
        )

    def test_report_tax_estimate_detail_rows(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        taxable = self._create_product(sku="GS-TAX-001", qty=5)
        exempt = self._create_product(sku="GS-TAX-002", qty=5)
        self.repo.update_product(taxable.id, {"category": "collectible"}, actor="qa-user")
        self.repo.update_product(exempt.id, {"category": "bullion"}, actor="qa-user")

        self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("10.00"),
            shipping_cost=Decimal("5.00"),
            quantity_sold=1,
            product_id=taxable.id,
            sold_at=now - timedelta(days=1),
        )
        self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("80.00"),
            fees=Decimal("8.00"),
            shipping_cost=Decimal("4.00"),
            quantity_sold=1,
            product_id=exempt.id,
            sold_at=now - timedelta(days=1),
        )

        rows = self.repo.report_tax_estimate_detail_rows(
            start_dt=now - timedelta(days=30),
            end_dt=now,
            tax_rate_percent=10.0,
            shipping_taxable=True,
            tax_exempt_categories={"bullion"},
            marketplaces={"ebay"},
        )
        self.assertEqual(len(rows), 2)
        taxable_row = next(r for r in rows if str(r.get("category")) == "collectible")
        exempt_row = next(r for r in rows if str(r.get("category")) == "bullion")

        self.assertEqual(bool(taxable_row.get("is_tax_exempt_category")), False)
        self.assertAlmostEqual(float(taxable_row.get("taxable_item_subtotal") or 0.0), 100.0, places=2)
        self.assertAlmostEqual(float(taxable_row.get("taxable_shipping_subtotal") or 0.0), 5.0, places=2)
        self.assertAlmostEqual(float(taxable_row.get("taxable_subtotal") or 0.0), 105.0, places=2)
        self.assertAlmostEqual(float(taxable_row.get("estimated_tax_collected") or 0.0), 10.5, places=2)

        self.assertEqual(bool(exempt_row.get("is_tax_exempt_category")), True)
        self.assertAlmostEqual(float(exempt_row.get("taxable_item_subtotal") or 0.0), 0.0, places=2)
        self.assertAlmostEqual(float(exempt_row.get("taxable_shipping_subtotal") or 0.0), 4.0, places=2)
        self.assertAlmostEqual(float(exempt_row.get("taxable_subtotal") or 0.0), 4.0, places=2)
        self.assertAlmostEqual(float(exempt_row.get("estimated_tax_collected") or 0.0), 0.4, places=2)

    def test_report_tax_estimate_detail_rows_use_listing_product_for_bundle_sale_without_product_link(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-TAX-BUNDLE-LISTING", qty=5)
        self.repo.update_product(product.id, {"category": "bullion"}, actor="qa-user")
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Tax bundle listing",
            listing_price=Decimal("100.00"),
            quantity_listed=1,
            marketplace_details=json.dumps(
                {
                    "bundle": {
                        "enabled": True,
                        "kind": "single_product_lot",
                        "components": [
                            {"product_id": product.id, "sku": product.sku, "quantity_per_listing": 2},
                        ],
                    }
                }
            ),
            actor="qa-user",
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("10.00"),
            shipping_cost=Decimal("5.00"),
            quantity_sold=1,
            product_id=None,
            listing_id=listing.id,
            external_order_id="EBAY-TAX-BUNDLE-LISTING",
            sold_at=now - timedelta(days=1),
        )

        rows = self.repo.report_tax_estimate_detail_rows(
            start_dt=now - timedelta(days=30),
            end_dt=now,
            tax_rate_percent=10.0,
            shipping_taxable=True,
            tax_exempt_categories={"bullion"},
            marketplaces={"ebay"},
        )
        row = next(r for r in rows if int(r.get("sale_id") or 0) == int(sale.id))

        self.assertEqual(row["sku"], product.sku)
        self.assertEqual(row["product_title"], product.title)
        self.assertEqual(row["category"], "bullion")
        self.assertEqual(bool(row["is_tax_exempt_category"]), True)
        self.assertAlmostEqual(float(row.get("taxable_item_subtotal") or 0.0), 0.0, places=2)
        self.assertAlmostEqual(float(row.get("taxable_shipping_subtotal") or 0.0), 5.0, places=2)
        self.assertAlmostEqual(float(row.get("estimated_tax_collected") or 0.0), 0.5, places=2)

    def test_report_tax_estimate_detail_rows_use_actual_shipping_charged(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-TAX-NORM-SHIP", qty=5)
        self.repo.update_product(product.id, {"category": "collectible"}, actor="qa-user")
        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="TAX-NORM-SHIP",
            order_status="paid",
            sold_at=now - timedelta(days=1),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("5.00"),
            items=[{"product_id": product.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("100.00")}],
            actor="qa-user",
        )
        self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("10.00"),
            shipping_cost=Decimal("0.00"),
            quantity_sold=1,
            product_id=product.id,
            order_id=order.id,
            external_order_id=order.external_order_id,
            sold_at=now - timedelta(days=1),
        )

        rows = self.repo.report_tax_estimate_detail_rows(
            start_dt=now - timedelta(days=30),
            end_dt=now,
            tax_rate_percent=10.0,
            shipping_taxable=True,
            tax_exempt_categories=set(),
            marketplaces={"ebay"},
        )

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertAlmostEqual(float(row.get("field_shipping_cost") or 0.0), 0.0, places=2)
        self.assertAlmostEqual(float(row.get("shipping_cost") or 0.0), 5.0, places=2)
        self.assertEqual(row.get("shipping_cost_source"), "actual_economics_allocated_shipping_charged")
        self.assertAlmostEqual(float(row.get("taxable_shipping_subtotal") or 0.0), 5.0, places=2)
        self.assertAlmostEqual(float(row.get("taxable_subtotal") or 0.0), 105.0, places=2)
        self.assertAlmostEqual(float(row.get("estimated_tax_collected") or 0.0), 10.5, places=2)

    def test_report_ebay_fee_reconciliation_rows(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-FEE-001", qty=5)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Fee Listing",
            listing_price=Decimal("50.00"),
            quantity_listed=1,
            marketplace_details=json.dumps(
                {
                    "ebay_publish": {
                        "fee_estimate": {
                            "estimated_total_fees": 8.0,
                            "quantity": 1,
                            "final_value_rate_percent": 12.0,
                            "final_value_fixed_usd": 0.3,
                            "payment_rate_percent": 2.9,
                            "payment_fixed_usd": 0.3,
                            "promoted_rate_percent": 0.0,
                        }
                    }
                }
            ),
            actor="qa-user",
        )
        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="EBAY-FEE-ORDER-1",
            order_status="paid",
            sold_at=now - timedelta(days=1),
            fees=Decimal("9.00"),
            shipping_cost=Decimal("0.00"),
            notes='fee_breakdown_json={"total_marketplace_fee":9.00}',
            items=[{"product_id": product.id, "listing_id": listing.id, "quantity": 1, "unit_price": Decimal("50.00")}],
            actor="qa-user",
        )
        self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("50.00"),
            fees=Decimal("8.50"),
            shipping_cost=Decimal("0.00"),
            quantity_sold=1,
            order_id=order.id,
            product_id=product.id,
            listing_id=listing.id,
            external_order_id="EBAY-FEE-ORDER-1",
            sold_at=now - timedelta(days=1),
            actor="qa-user",
        )

        rows = self.repo.report_ebay_fee_reconciliation_rows(
            start_dt=now - timedelta(days=30),
            end_dt=now,
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.get("actual_fee_source"), "order_fee_breakdown_total_marketplace_fee")
        self.assertAlmostEqual(float(row.get("actual_fee") or 0.0), 9.0, places=2)
        self.assertAlmostEqual(float(row.get("estimated_fee_scaled") or 0.0), 8.0, places=2)
        self.assertAlmostEqual(float(row.get("variance_actual_minus_estimate") or 0.0), 1.0, places=2)
        self.assertEqual(bool(row.get("fee_estimate_present")), True)

    def test_report_ebay_fee_reconciliation_prefers_normalized_order_finance_fee(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-FEE-002", qty=5)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Fee Listing 2",
            listing_price=Decimal("40.00"),
            quantity_listed=1,
            marketplace_details=json.dumps(
                {
                    "ebay_publish": {
                        "fee_estimate": {
                            "estimated_total_fees": 6.0,
                            "quantity": 1,
                        }
                    }
                }
            ),
            actor="qa-user",
        )
        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="EBAY-FEE-ORDER-2",
            order_status="paid",
            sold_at=now - timedelta(days=1),
            fees=Decimal("9.00"),
            shipping_cost=Decimal("0.00"),
            notes='fee_breakdown_json={"total_marketplace_fee":9.00}',
            items=[{"product_id": product.id, "listing_id": listing.id, "quantity": 1, "unit_price": Decimal("40.00")}],
            actor="qa-user",
        )
        self.repo.replace_order_finance_entries(
            order.id,
            [
                {
                    "marketplace": "ebay",
                    "external_order_id": order.external_order_id,
                    "transaction_id": "TX-FEE-2",
                    "line_item_id": "",
                    "legacy_item_id": "",
                    "sku": product.sku,
                    "entry_kind": "marketplace_fee",
                    "fee_type": "FINAL_VALUE_FEE",
                    "amount": Decimal("7.25"),
                    "currency": "USD",
                    "booking_entry": "DEBIT",
                    "transaction_type": "SALE",
                    "transaction_status": "FUNDS_AVAILABLE_FOR_PAYOUT",
                    "transaction_date": now - timedelta(days=1),
                    "memo": "",
                    "source": "finance_transactions_orderLineItems",
                    "raw": {},
                }
            ],
            actor="qa-user",
        )
        self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("40.00"),
            fees=Decimal("8.50"),
            shipping_cost=Decimal("0.00"),
            quantity_sold=1,
            order_id=order.id,
            product_id=product.id,
            listing_id=listing.id,
            external_order_id="EBAY-FEE-ORDER-2",
            sold_at=now - timedelta(days=1),
            actor="qa-user",
        )

        rows = self.repo.report_ebay_fee_reconciliation_rows(
            start_dt=now - timedelta(days=30),
            end_dt=now,
        )
        row = next(r for r in rows if str(r.get("external_order_id")) == "EBAY-FEE-ORDER-2")
        self.assertEqual(row.get("actual_fee_source"), "normalized_order_finance_entries_marketplace_fee_sum")
        self.assertAlmostEqual(float(row.get("actual_fee") or 0.0), 7.25, places=2)
        self.assertTrue(bool(row.get("normalized_order_finance_marketplace_fee_present")))

    def test_collect_rollup_latency_baseline(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-BASE-001", qty=6)
        self.repo.update_product(product.id, {"category": "collectible"}, actor="qa-user")
        self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("42.00"),
            fees=Decimal("4.20"),
            shipping_cost=Decimal("3.00"),
            quantity_sold=1,
            product_id=product.id,
            sold_at=now - timedelta(days=1),
            actor="qa-user",
        )
        self.repo.create_order(
            marketplace="ebay",
            external_order_id="BASE-ORD-1",
            order_status="paid",
            sold_at=now - timedelta(days=1),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("0.00"),
            items=[{"product_id": product.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("42.00")}],
            actor="qa-user",
        )

        rows = self.repo.collect_rollup_latency_baseline(
            start_dt=now - timedelta(days=30),
            end_dt=now,
            tax_rate_percent=8.5,
            shipping_taxable=True,
            tax_exempt_categories={"bullion"},
            marketplaces={"ebay"},
        )
        names = {str(r.get("rollup_name") or "") for r in rows}
        self.assertEqual(
            names,
            {
                "dashboard_live_metrics",
                "report_shipping_economics_rows",
                "report_shipping_economics_summary",
                "report_tax_estimate_detail_rows",
                "report_ebay_fee_reconciliation_rows",
            },
        )
        for row in rows:
            self.assertIn("elapsed_ms", row)
            self.assertGreaterEqual(float(row.get("elapsed_ms") or 0.0), 0.0)
            self.assertIn("result_count", row)

    def test_collect_rollup_explain_baseline(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)

        class _FakeResult:
            def all(self):
                return [
                    ("Seq Scan on fake_table  (cost=0.00..1.00 rows=1 width=8)",),
                    ("Planning Time: 0.321 ms",),
                    ("Execution Time: 1.234 ms",),
                ]

        with patch.object(self.repo.db, "execute", return_value=_FakeResult()) as execute_mock:
            rows = self.repo.collect_rollup_explain_baseline(
                start_dt=now - timedelta(days=30),
                end_dt=now,
                sample_limit=1500,
            )

        self.assertEqual(len(rows), 18)
        names = {str(r.get("rollup_name") or "") for r in rows}
        self.assertEqual(
            names,
            {
                "dashboard_live_metrics",
                "report_shipping_economics_rows",
                "report_shipping_economics_summary",
                "report_tax_estimate_detail_rows",
                "report_ebay_fee_reconciliation_rows",
                "dashboard_ebay_fee_type_breakdown_30d",
                "notification_outbox_runner_activity_14d",
                "slack_ops_queue_health_rows",
                "slack_ops_events_24h",
                "report_orders_rows",
                "report_order_items_rows",
                "report_sales_rows",
                "report_returns_rows",
                "report_listings_rows",
                "report_products_rows",
                "report_lot_assignment_rows",
                "report_inventory_movement_rows",
                "report_marketplace_reconciliation_rows",
            },
        )
        self.assertEqual(execute_mock.call_count, 18)
        dashboard_call = execute_mock.call_args_list[0]
        dashboard_sql = str(dashboard_call.args[0])
        dashboard_params = dict(dashboard_call.args[1])
        self.assertIn("s.sold_at <= :end_dt", dashboard_sql)
        self.assertIn("sold_at <= :end_dt", dashboard_sql)
        self.assertEqual(dashboard_params.get("end_dt"), now)
        for row in rows:
            self.assertGreaterEqual(float(row.get("elapsed_ms") or 0.0), 0.0)
            self.assertAlmostEqual(float(row.get("planning_ms") or 0.0), 0.321, places=3)
            self.assertAlmostEqual(float(row.get("execution_ms") or 0.0), 1.234, places=3)
            self.assertGreater(int(row.get("plan_lines") or 0), 0)
            self.assertIn("plan_text", row)

    def test_collect_rollup_explain_baseline_continues_after_probe_failure(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)

        class _FakeResult:
            def all(self):
                return [
                    ("Seq Scan on fake_table  (cost=0.00..1.00 rows=1 width=8)",),
                    ("Planning Time: 0.200 ms",),
                    ("Execution Time: 0.900 ms",),
                ]

        calls = {"count": 0}

        def _execute_side_effect(*_args, **_kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("probe failed")
            return _FakeResult()

        with patch.object(self.repo.db, "execute", side_effect=_execute_side_effect):
            rows = self.repo.collect_rollup_explain_baseline(
                start_dt=now - timedelta(days=30),
                end_dt=now,
                sample_limit=1500,
            )

        self.assertEqual(len(rows), 18)
        error_rows = [row for row in rows if str(row.get("error") or "").strip()]
        self.assertTrue(error_rows)
        self.assertIn("probe failed", str(error_rows[0].get("error") or ""))

    def test_collect_rollup_explain_baseline_marks_missing_relation_as_skipped(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)

        class _FakeResult:
            def all(self):
                return [
                    ("Seq Scan on fake_table  (cost=0.00..1.00 rows=1 width=8)",),
                    ("Planning Time: 0.200 ms",),
                    ("Execution Time: 0.900 ms",),
                ]

        def _execute_side_effect(*args, **_kwargs):
            sql_text = str(args[0] if args else "")
            if "FROM returns r" in sql_text:
                raise RuntimeError('relation "returns" does not exist')
            return _FakeResult()

        with patch.object(self.repo.db, "execute", side_effect=_execute_side_effect):
            rows = self.repo.collect_rollup_explain_baseline(
                start_dt=now - timedelta(days=30),
                end_dt=now,
                sample_limit=1500,
            )

        self.assertEqual(len(rows), 18)
        returns_rows = [row for row in rows if str(row.get("rollup_name") or "") == "report_returns_rows"]
        self.assertEqual(len(returns_rows), 1)
        row = returns_rows[0]
        self.assertTrue(bool(row.get("skipped")))
        self.assertEqual(str(row.get("skip_reason") or ""), "table returns not present")
        self.assertEqual(str(row.get("error") or ""), "")

    def test_report_ebay_marketplace_fee_rows(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-FEE-ROWS-001", qty=5)

        ebay_order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="EBAY-FEE-ROWS-1",
            order_status="paid",
            sold_at=now - timedelta(days=1),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("0.00"),
            items=[{"product_id": product.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("20.00")}],
            actor="qa-user",
        )
        self.repo.replace_order_finance_entries(
            order_id=int(ebay_order.id),
            entries=[
                {
                    "entry_kind": "marketplace_fee",
                    "amount": 1.25,
                    "currency": "USD",
                    "fee_type": "FINAL_VALUE_FEE",
                    "line_item_id": "LI-1",
                    "sku": "GS-FEE-ROWS-001",
                    "source": "normalized_order_finance_entries",
                }
            ],
            actor="qa-user",
        )

        non_ebay_order = self.repo.create_order(
            marketplace="shopify",
            external_order_id="SHOP-FEE-ROWS-1",
            order_status="paid",
            sold_at=now - timedelta(days=1),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("0.00"),
            items=[{"product_id": product.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("20.00")}],
            actor="qa-user",
        )
        self.repo.replace_order_finance_entries(
            order_id=int(non_ebay_order.id),
            entries=[
                {
                    "entry_kind": "marketplace_fee",
                    "amount": 9.99,
                    "currency": "USD",
                    "fee_type": "OTHER",
                    "line_item_id": "LI-2",
                    "sku": "GS-FEE-ROWS-001",
                    "source": "normalized_order_finance_entries",
                }
            ],
            actor="qa-user",
        )

        rows = self.repo.report_ebay_marketplace_fee_rows(
            start_dt=now - timedelta(days=30),
            end_dt=now,
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(str(row.get("external_order_id") or ""), "EBAY-FEE-ROWS-1")
        self.assertEqual(str(row.get("fee_type") or ""), "FINAL_VALUE_FEE")
        self.assertAlmostEqual(float(row.get("fee_amount") or 0.0), 1.25, places=2)

    def test_report_marketplace_reconciliation_rows(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-RECON-001", qty=8)

        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="EBAY-RECON-1",
            order_status="paid",
            sold_at=now - timedelta(days=1),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("2.00"),
            items=[{"product_id": product.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("100.00")}],
            actor="qa-user",
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("10.00"),
            shipping_cost=Decimal("2.00"),
            quantity_sold=1,
            order_id=order.id,
            product_id=product.id,
            external_order_id="EBAY-RECON-1",
            sold_at=now - timedelta(days=1),
            actor="qa-user",
        )
        self.repo.create_return(
            marketplace="ebay",
            quantity=1,
            refund_amount=Decimal("5.00"),
            refund_fees=Decimal("1.00"),
            refund_shipping=Decimal("0.50"),
            sale_id=sale.id,
            order_id=order.id,
            product_id=product.id,
            returned_at=now - timedelta(hours=12),
            actor="qa-user",
        )

        rows = self.repo.report_marketplace_reconciliation_rows(
            start_dt=now - timedelta(days=30),
            end_dt=now,
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(str(row.get("marketplace") or ""), "ebay")
        self.assertEqual(int(row.get("sales_count") or 0), 1)
        self.assertEqual(int(row.get("orders_count") or 0), 1)
        self.assertEqual(int(row.get("returns_count") or 0), 1)
        self.assertAlmostEqual(float(row.get("sales_gross") or 0.0), 100.0, places=2)
        self.assertAlmostEqual(float(row.get("order_total_sum") or 0.0), 100.0, places=2)
        self.assertAlmostEqual(float(row.get("reconcile_delta") or 0.0), 0.0, places=2)
        self.assertEqual(row.get("reconcile_basis"), "order_total_sum - sales_gross")
        self.assertAlmostEqual(float(row.get("returns_refund_total") or 0.0), 6.5, places=2)
        self.assertFalse(bool(row.get("reconcile_flag")))

    def test_report_marketplace_reconciliation_uses_linked_normalized_actuals(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-RECON-NORM-ACTUALS", qty=8)
        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="EBAY-RECON-NORM-ACTUALS",
            order_status="paid",
            sold_at=now - timedelta(days=1),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("5.00"),
            items=[{"product_id": product.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("100.00")}],
            actor="qa-user",
        )
        self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("10.00"),
            shipping_cost=Decimal("5.00"),
            shipping_label_cost=Decimal("9.00"),
            quantity_sold=1,
            order_id=order.id,
            product_id=product.id,
            external_order_id=order.external_order_id,
            sold_at=now - timedelta(days=1),
            actor="qa-user",
        )
        self.repo.replace_order_finance_entries(
            order.id,
            [
                {"entry_kind": "marketplace_fee", "fee_type": "FINAL_VALUE_FEE", "amount": Decimal("7.50")},
                {"entry_kind": "shipping_label", "fee_type": "SHIPPING_LABEL", "amount": Decimal("4.25")},
            ],
            actor="qa-user",
        )

        rows = self.repo.report_marketplace_reconciliation_rows(
            start_dt=now - timedelta(days=30),
            end_dt=now,
        )

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertAlmostEqual(float(row.get("sales_fees") or 0.0), 7.5, places=2)
        self.assertAlmostEqual(float(row.get("sales_shipping_cost") or 0.0), 5.0, places=2)
        self.assertAlmostEqual(float(row.get("sales_shipping_label_cost") or 0.0), 4.25, places=2)
        self.assertAlmostEqual(float(row.get("sales_net_before_returns") or 0.0), 93.25, places=2)
        self.assertAlmostEqual(float(row.get("net_after_returns") or 0.0), 93.25, places=2)

    def test_report_marketplace_reconciliation_accepts_order_total_including_shipping(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-RECON-SHIP-INCL", qty=8)
        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="EBAY-RECON-SHIP-INCL",
            order_status="paid",
            sold_at=now - timedelta(days=1),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("7.50"),
            items=[{"product_id": product.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("100.00")}],
            actor="qa-user",
        )
        self.repo.update_order(order.id, {"total_amount": Decimal("107.50")}, actor="qa-user")
        self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("10.00"),
            shipping_cost=Decimal("7.50"),
            shipping_label_cost=Decimal("4.25"),
            quantity_sold=1,
            order_id=order.id,
            product_id=product.id,
            external_order_id=order.external_order_id,
            sold_at=now - timedelta(days=1),
            actor="qa-user",
        )

        rows = self.repo.report_marketplace_reconciliation_rows(
            start_dt=now - timedelta(days=30),
            end_dt=now,
        )

        row = rows[0]
        self.assertAlmostEqual(float(row.get("delta_order_total_vs_sales_gross") or 0.0), 7.5, places=2)
        self.assertAlmostEqual(
            float(row.get("delta_order_total_vs_sales_gross_plus_shipping") or 0.0),
            0.0,
            places=2,
        )
        self.assertAlmostEqual(float(row.get("reconcile_delta") or 0.0), 0.0, places=2)
        self.assertEqual(row.get("reconcile_basis"), "order_total_sum - (sales_gross + sales_shipping_cost)")
        self.assertFalse(bool(row.get("reconcile_flag")))

    def test_report_marketplace_reconciliation_treats_immaterial_period_delta_as_noise(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-RECON-IMMATERIAL", qty=8)
        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="EBAY-RECON-IMMATERIAL",
            order_status="paid",
            sold_at=now - timedelta(days=1),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("0.00"),
            items=[{"product_id": product.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("1000.00")}],
            actor="qa-user",
        )
        self.repo.update_order(order.id, {"total_amount": Decimal("1000.50")}, actor="qa-user")
        self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("1000.00"),
            fees=Decimal("10.00"),
            shipping_cost=Decimal("0.00"),
            shipping_label_cost=Decimal("4.25"),
            quantity_sold=1,
            order_id=order.id,
            product_id=product.id,
            external_order_id=order.external_order_id,
            sold_at=now - timedelta(days=1),
            actor="qa-user",
        )

        rows = self.repo.report_marketplace_reconciliation_rows(
            start_dt=now - timedelta(days=30),
            end_dt=now,
        )

        row = rows[0]
        self.assertAlmostEqual(float(row.get("reconcile_delta") or 0.0), 0.5, places=2)
        self.assertAlmostEqual(float(row.get("reconcile_tolerance") or 0.0), 1.0, places=2)
        self.assertLess(float(row.get("reconcile_materiality_pct") or 0.0), 0.1)
        self.assertFalse(bool(row.get("reconcile_flag")))

    def test_report_sales_actual_econ_rows(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-ACTUAL-ECON-001", qty=10)

        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="EBAY-ACTUAL-ECON-1",
            order_status="paid",
            sold_at=now - timedelta(days=1),
            fees=Decimal("10.00"),
            shipping_cost=Decimal("4.00"),
            shipping_label_cost=Decimal("5.00"),
            items=[
                {"product_id": product.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("60.00")},
                {"product_id": product.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("40.00")},
            ],
            actor="qa-user",
        )
        sale1 = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("60.00"),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("0.00"),
            quantity_sold=1,
            order_id=order.id,
            product_id=product.id,
            external_order_id="EBAY-ACTUAL-ECON-1",
            sold_at=now - timedelta(days=1),
            actor="qa-user",
        )
        sale2 = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("40.00"),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("0.00"),
            quantity_sold=1,
            order_id=order.id,
            product_id=product.id,
            external_order_id="EBAY-ACTUAL-ECON-1",
            sold_at=now - timedelta(days=1),
            actor="qa-user",
        )

        rows = self.repo.report_sales_actual_econ_rows(
            start_dt=now - timedelta(days=30),
            end_dt=now,
        )
        row_by_sale_id = {int(r.get("sale_id") or 0): r for r in rows}
        self.assertIn(int(sale1.id), row_by_sale_id)
        self.assertIn(int(sale2.id), row_by_sale_id)

        row1 = row_by_sale_id[int(sale1.id)]
        row2 = row_by_sale_id[int(sale2.id)]
        self.assertAlmostEqual(float(row1.get("allocation_weight") or 0.0), 0.6, places=3)
        self.assertAlmostEqual(float(row2.get("allocation_weight") or 0.0), 0.4, places=3)
        self.assertAlmostEqual(float(row1.get("allocated_fee_actual") or 0.0), 6.0, places=2)
        self.assertAlmostEqual(float(row2.get("allocated_fee_actual") or 0.0), 4.0, places=2)
        self.assertAlmostEqual(float(row1.get("allocated_shipping_actual") or 0.0), 3.0, places=2)
        self.assertAlmostEqual(float(row2.get("allocated_shipping_actual") or 0.0), 2.0, places=2)
        self.assertAlmostEqual(float(row1.get("net_before_cogs_actual") or 0.0), 53.4, places=2)
        self.assertAlmostEqual(float(row2.get("net_before_cogs_actual") or 0.0), 35.6, places=2)

    def test_report_sales_actual_econ_rows_prefers_normalized_finance_entries(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-ACTUAL-ECON-NORM-001", qty=10)
        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="EBAY-ACTUAL-ECON-NORM-1",
            order_status="paid",
            sold_at=now - timedelta(days=1),
            fees=Decimal("10.00"),
            shipping_cost=Decimal("4.00"),
            shipping_label_cost=Decimal("5.00"),
            items=[{"product_id": product.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("100.00")}],
            actor="qa-user",
        )
        self.repo.replace_order_finance_entries(
            order.id,
            [
                {"entry_kind": "marketplace_fee", "amount": Decimal("7.50")},
                {"entry_kind": "shipping_label", "amount": Decimal("4.25")},
            ],
            actor="qa-user",
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("0.00"),
            quantity_sold=1,
            order_id=order.id,
            product_id=product.id,
            external_order_id="EBAY-ACTUAL-ECON-NORM-1",
            sold_at=now - timedelta(days=1),
            actor="qa-user",
        )

        rows = self.repo.report_sales_actual_econ_rows(start_dt=now - timedelta(days=30), end_dt=now)
        row = next(r for r in rows if int(r.get("sale_id") or 0) == int(sale.id))
        self.assertEqual(row.get("actual_fee_source"), "normalized_order_finance_entries_marketplace_fee_sum")
        self.assertEqual(row.get("actual_shipping_source"), "normalized_order_finance_entries_shipping_label_sum")
        self.assertAlmostEqual(float(row.get("allocated_fee_actual") or 0.0), 7.5, places=2)
        self.assertAlmostEqual(float(row.get("allocated_shipping_actual") or 0.0), 4.25, places=2)
        self.assertAlmostEqual(float(row.get("net_before_cogs_actual") or 0.0), 92.25, places=2)

    def test_economics_reports_use_listing_product_for_bundle_sale_without_product_link(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-ECON-BUNDLE-LISTING", qty=10)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Economics bundle listing",
            listing_price=Decimal("100.00"),
            quantity_listed=2,
            external_listing_id="EBAY-ECON-BUNDLE-LISTING",
            marketplace_details=json.dumps(
                {
                    "bundle": {
                        "enabled": True,
                        "kind": "single_product_lot",
                        "components": [
                            {"product_id": product.id, "sku": product.sku, "quantity_per_listing": 2},
                        ],
                    },
                    "ebay_publish": {
                        "fee_estimate": {
                            "estimated_total_fees": 8.0,
                            "quantity": 2,
                        }
                    },
                }
            ),
            actor="qa-user",
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("10.00"),
            shipping_cost=Decimal("0.00"),
            quantity_sold=1,
            listing_id=listing.id,
            product_id=None,
            external_order_id="EBAY-ECON-BUNDLE-LISTING-ORDER",
            sold_at=now - timedelta(days=1),
            actor="qa-user",
        )

        actual_rows = self.repo.report_sales_actual_econ_rows(
            start_dt=now - timedelta(days=30),
            end_dt=now,
        )
        fact_rows = self.repo.report_economics_intelligence_fact_rows(
            start_dt=now - timedelta(days=30),
            end_dt=now,
            marketplaces={"ebay"},
        )
        fee_rows = self.repo.report_ebay_fee_reconciliation_rows(
            start_dt=now - timedelta(days=30),
            end_dt=now,
        )

        actual = next(row for row in actual_rows if int(row.get("sale_id") or 0) == int(sale.id))
        fact = next(row for row in fact_rows if int(row.get("sale_id") or 0) == int(sale.id))
        fee = next(row for row in fee_rows if int(row.get("sale_id") or 0) == int(sale.id))
        self.assertEqual(actual["sku"], product.sku)
        self.assertEqual(actual["product_title"], product.title)
        self.assertEqual(fact["product_id"], product.id)
        self.assertEqual(fact["sku"], product.sku)
        self.assertEqual(fact["product_title"], product.title)
        self.assertEqual(fee["sku"], product.sku)
        self.assertEqual(fee["product_title"], product.title)

    def test_report_economics_intelligence_fact_rows(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-ECON-FACT-001", qty=10)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Economics Fact Listing",
            listing_price=Decimal("100.00"),
            quantity_listed=2,
            marketplace_details=json.dumps(
                {
                    "ebay_publish": {
                        "fee_estimate": {
                            "estimated_total_fees": 8.0,
                            "quantity": 2,
                        }
                    }
                }
            ),
            actor="qa-user",
        )
        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="EBAY-ECON-FACT-1",
            order_status="paid",
            sold_at=now - timedelta(days=1),
            fees=Decimal("10.00"),
            shipping_cost=Decimal("4.00"),
            shipping_label_cost=Decimal("5.00"),
            items=[{"product_id": product.id, "listing_id": listing.id, "quantity": 1, "unit_price": Decimal("100.00")}],
            actor="qa-user",
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("0.00"),
            quantity_sold=1,
            order_id=order.id,
            listing_id=listing.id,
            product_id=product.id,
            external_order_id="EBAY-ECON-FACT-1",
            sold_at=now - timedelta(days=1),
            actor="qa-user",
        )

        rows = self.repo.report_economics_intelligence_fact_rows(
            start_dt=now - timedelta(days=30),
            end_dt=now,
            marketplaces={"ebay"},
        )
        row_by_sale_id = {int(r.get("sale_id") or 0): r for r in rows}
        self.assertIn(int(sale.id), row_by_sale_id)
        row = row_by_sale_id[int(sale.id)]

        self.assertTrue(bool(row.get("estimate_available")))
        self.assertAlmostEqual(float(row.get("estimated_fee_alloc") or 0.0), 4.0, places=2)
        self.assertAlmostEqual(float(row.get("actual_fee_alloc") or 0.0), 10.0, places=2)
        self.assertAlmostEqual(float(row.get("expected_shipping_alloc") or 0.0), 4.0, places=2)
        self.assertAlmostEqual(float(row.get("actual_shipping_alloc") or 0.0), 5.0, places=2)
        self.assertAlmostEqual(float(row.get("estimated_net_before_cogs") or 0.0), 95.0, places=2)
        self.assertAlmostEqual(float(row.get("actual_net_before_cogs") or 0.0), 89.0, places=2)
        self.assertAlmostEqual(float(row.get("fee_variance_actual_minus_estimated") or 0.0), 6.0, places=2)
        self.assertAlmostEqual(float(row.get("shipping_delta_expected_minus_actual") or 0.0), -1.0, places=2)
        self.assertAlmostEqual(float(row.get("net_variance_actual_minus_estimated") or 0.0), -6.0, places=2)

    def test_report_economics_intelligence_fact_rows_prefers_normalized_finance_entries(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-ECON-FACT-NORM-001", qty=10)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Economics Fact Normalized",
            listing_price=Decimal("100.00"),
            quantity_listed=1,
            marketplace_details=json.dumps({"ebay_publish": {"fee_estimate": {"estimated_total_fees": 8.0, "quantity": 1}}}),
            actor="qa-user",
        )
        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="EBAY-ECON-FACT-NORM-1",
            order_status="paid",
            sold_at=now - timedelta(days=1),
            fees=Decimal("10.00"),
            shipping_cost=Decimal("4.00"),
            shipping_label_cost=Decimal("5.00"),
            items=[{"product_id": product.id, "listing_id": listing.id, "quantity": 1, "unit_price": Decimal("100.00")}],
            actor="qa-user",
        )
        self.repo.replace_order_finance_entries(
            order.id,
            [
                {"entry_kind": "marketplace_fee", "amount": Decimal("7.50")},
                {"entry_kind": "shipping_label", "amount": Decimal("4.25")},
            ],
            actor="qa-user",
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("0.00"),
            quantity_sold=1,
            order_id=order.id,
            listing_id=listing.id,
            product_id=product.id,
            external_order_id="EBAY-ECON-FACT-NORM-1",
            sold_at=now - timedelta(days=1),
            actor="qa-user",
        )

        rows = self.repo.report_economics_intelligence_fact_rows(
            start_dt=now - timedelta(days=30),
            end_dt=now,
            marketplaces={"ebay"},
        )
        row = next(r for r in rows if int(r.get("sale_id") or 0) == int(sale.id))
        self.assertEqual(row.get("actual_fee_source"), "normalized_order_finance_entries_marketplace_fee_sum")
        self.assertEqual(row.get("actual_shipping_source"), "normalized_order_finance_entries_shipping_label_sum")
        self.assertAlmostEqual(float(row.get("actual_fee_alloc") or 0.0), 7.5, places=2)
        self.assertAlmostEqual(float(row.get("actual_shipping_alloc") or 0.0), 4.25, places=2)
        self.assertAlmostEqual(float(row.get("actual_net_before_cogs") or 0.0), 92.25, places=2)
        self.assertAlmostEqual(float(row.get("net_variance_actual_minus_estimated") or 0.0), 0.5, places=2)

    def test_report_listing_review_activity_rows(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-REVIEW-001", qty=3)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Review Listing",
            listing_price=Decimal("25.00"),
            quantity_listed=1,
            actor="qa-user",
        )
        review_payload = {
            "review_history": [
                {
                    "reviewed_at": (now - timedelta(days=2)).isoformat(),
                    "decision": "approved",
                    "actor": "admin",
                    "notes": "Looks good",
                },
                {
                    "reviewed_at": (now - timedelta(days=40)).isoformat(),
                    "decision": "rejected",
                    "actor": "admin",
                    "notes": "Old event",
                },
            ]
        }
        self.repo.update_listing(
            listing.id,
            {"marketplace_details": json.dumps(review_payload)},
            actor="qa-user",
        )

        rows = self.repo.report_listing_review_activity_rows(
            start_dt=now - timedelta(days=30),
            end_dt=now,
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(int(row.get("listing_id") or 0), int(listing.id))
        self.assertEqual(str(row.get("review_decision") or ""), "approved")
        self.assertEqual(str(row.get("reviewed_by") or ""), "admin")

    def test_report_inventory_movement_rows_date_bounded(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-MOVE-RPT-001", qty=5)
        sale_in_range = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("20.00"),
            fees=Decimal("1.00"),
            shipping_cost=Decimal("2.00"),
            quantity_sold=1,
            product_id=product.id,
            sold_at=now - timedelta(days=1),
        )
        self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("21.00"),
            fees=Decimal("1.00"),
            shipping_cost=Decimal("2.00"),
            quantity_sold=1,
            product_id=product.id,
            sold_at=now - timedelta(days=40),
        )

        rows = self.repo.report_inventory_movement_rows(
            start_dt=now - timedelta(days=7),
            end_dt=now,
        )
        sales_rows = [r for r in rows if str(r.get("movement_type") or "").strip().lower() == "sale"]
        self.assertEqual(len(sales_rows), 1)
        sale_row = sales_rows[0]
        self.assertEqual(int(sale_row.get("product_id") or 0), int(product.id))
        self.assertEqual(str(sale_row.get("sku") or ""), str(product.sku or ""))
        self.assertEqual(int(sale_row.get("reference_id") or 0), int(sale_in_range.id))

    def test_report_orders_and_order_items_rows_date_bounded(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-ORDER-RPT-001", qty=6)
        order_in = self.repo.create_order(
            marketplace="ebay",
            external_order_id="EBAY-ORDER-RPT-IN",
            order_status="paid",
            sold_at=now - timedelta(days=1),
            fees=Decimal("3.00"),
            shipping_cost=Decimal("4.50"),
            items=[{"product_id": product.id, "listing_id": None, "quantity": 2, "unit_price": Decimal("15.00")}],
            actor="qa-user",
        )
        self.repo.create_order(
            marketplace="ebay",
            external_order_id="EBAY-ORDER-RPT-OUT",
            order_status="paid",
            sold_at=now - timedelta(days=40),
            fees=Decimal("2.00"),
            shipping_cost=Decimal("3.50"),
            items=[{"product_id": product.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("14.00")}],
            actor="qa-user",
        )

        order_rows = self.repo.report_orders_rows(
            start_dt=now - timedelta(days=7),
            end_dt=now,
        )
        self.assertEqual(len(order_rows), 1)
        order_row = order_rows[0]
        self.assertEqual(int(order_row.get("order_id") or 0), int(order_in.id))
        self.assertEqual(str(order_row.get("external_order_id") or ""), "EBAY-ORDER-RPT-IN")
        self.assertEqual(int(order_row.get("item_count") or 0), 1)

        item_rows = self.repo.report_order_items_rows(
            start_dt=now - timedelta(days=7),
            end_dt=now,
        )
        self.assertEqual(len(item_rows), 1)
        item_row = item_rows[0]
        self.assertEqual(int(item_row.get("order_id") or 0), int(order_in.id))
        self.assertEqual(str(item_row.get("sku") or ""), str(product.sku or ""))
        self.assertAlmostEqual(float(item_row.get("line_total") or 0.0), 30.0, places=2)

    def test_report_orders_rows_include_linked_normalized_actuals(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-ORDER-RPT-NORM", qty=6)
        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="EBAY-ORDER-RPT-NORM",
            order_status="paid",
            sold_at=now - timedelta(days=1),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("5.00"),
            shipping_label_cost=Decimal("9.00"),
            items=[{"product_id": product.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("100.00")}],
            actor="qa-user",
        )
        self.repo.replace_order_finance_entries(
            order.id,
            [
                {"entry_kind": "marketplace_fee", "fee_type": "FINAL_VALUE_FEE", "amount": Decimal("7.50")},
                {"entry_kind": "shipping_label", "fee_type": "SHIPPING_LABEL", "amount": Decimal("4.25")},
            ],
            actor="qa-user",
        )

        rows = self.repo.report_orders_rows(start_dt=now - timedelta(days=7), end_dt=now)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertAlmostEqual(float(row.get("field_fees") or 0.0), 0.0, places=2)
        self.assertAlmostEqual(float(row.get("field_shipping_label_cost") or 0.0), 9.0, places=2)
        self.assertAlmostEqual(float(row.get("actual_fee") or 0.0), 7.5, places=2)
        self.assertAlmostEqual(float(row.get("actual_shipping_label_cost") or 0.0), 4.25, places=2)
        self.assertAlmostEqual(float(row.get("shipping_delta_charged_minus_actual") or 0.0), -4.0, places=2)
        self.assertAlmostEqual(float(row.get("actual_shipping_delta_charged_minus_label") or 0.0), 0.75, places=2)
        self.assertAlmostEqual(float(row.get("actual_net_before_cogs") or 0.0), 93.25, places=2)
        self.assertEqual(row.get("actual_fee_source"), "normalized_order_finance_entries_marketplace_fee_sum")
        self.assertEqual(row.get("actual_shipping_source"), "normalized_order_finance_entries_shipping_label_sum")

    def test_lot_profitability_snapshot_uses_linked_normalized_actuals(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-LOT-PNL-NORM", qty=1)
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-PNL-NORM",
            vendor="Dealer",
            purchase_date=now - timedelta(days=3),
            total_cost=Decimal("20.00"),
        )
        self.repo.assign_product_to_lot(
            product_id=product.id,
            lot_id=lot.id,
            quantity_acquired=1,
            unit_cost=None,
            acquired_at=now - timedelta(days=3),
        )
        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="LOT-PNL-NORM-ORDER",
            order_status="paid",
            sold_at=now - timedelta(days=1),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("5.00"),
            shipping_label_cost=Decimal("9.00"),
            items=[{"product_id": product.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("100.00")}],
            actor="qa-user",
        )
        self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("10.00"),
            shipping_cost=Decimal("5.00"),
            shipping_label_cost=Decimal("9.00"),
            quantity_sold=1,
            product_id=product.id,
            order_id=order.id,
            external_order_id=order.external_order_id,
            sold_at=now - timedelta(days=1),
        )
        self.repo.replace_order_finance_entries(
            order.id,
            [
                {"entry_kind": "marketplace_fee", "fee_type": "FINAL_VALUE_FEE", "amount": Decimal("7.50")},
                {"entry_kind": "shipping_label", "fee_type": "SHIPPING_LABEL", "amount": Decimal("4.25")},
            ],
            actor="qa-user",
        )

        snapshot = self.repo.lot_profitability_snapshot(lot.id)
        row = snapshot["rows"][0]

        self.assertAlmostEqual(float(row.get("estimated_net_before_cogs_from_lot") or 0.0), 93.25, places=2)
        self.assertAlmostEqual(float(row.get("estimated_lot_cogs") or 0.0), 20.0, places=2)
        self.assertAlmostEqual(float(row.get("estimated_lot_profit") or 0.0), 73.25, places=2)
        self.assertEqual(row.get("cost_source"), "lot_equal_quantity_fallback")
        self.assertEqual(snapshot["summary"].get("cost_source"), "lot_equal_quantity_fallback")
        self.assertAlmostEqual(
            float((snapshot["summary"].get("cost_source_totals") or {}).get("lot_equal_quantity_fallback") or 0.0),
            20.0,
            places=2,
        )
        self.assertAlmostEqual(
            float(snapshot["summary"].get("estimated_net_before_cogs") or 0.0),
            93.25,
            places=2,
        )

    def test_lot_profitability_snapshot_applies_return_impact(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-LOT-PNL-RETURN", qty=1)
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-PNL-RETURN",
            vendor="Dealer",
            purchase_date=now - timedelta(days=3),
            total_cost=Decimal("25.00"),
        )
        self.repo.assign_product_to_lot(
            product_id=product.id,
            lot_id=lot.id,
            quantity_acquired=1,
            unit_cost=Decimal("25.00"),
            acquired_at=now - timedelta(days=3),
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("10.00"),
            shipping_cost=Decimal("5.00"),
            shipping_label_cost=Decimal("4.00"),
            quantity_sold=1,
            product_id=product.id,
            sold_at=now - timedelta(days=1),
        )
        self.repo.create_return(
            marketplace="ebay",
            sale_id=sale.id,
            quantity=1,
            refund_amount=Decimal("100.00"),
            return_status="processed",
            disposition="restock",
            restocked=True,
            returned_at=now - timedelta(hours=12),
            actor="qa-user",
        )

        snapshot = self.repo.lot_profitability_snapshot(lot.id)
        row = snapshot["rows"][0]
        summary = snapshot["summary"]

        self.assertEqual(int(row.get("qty_returned_from_lot") or 0), 1)
        self.assertAlmostEqual(float(row.get("returns_refund_total") or 0.0), 100.0, places=2)
        self.assertAlmostEqual(float(row.get("returns_cogs_reversal") or 0.0), 25.0, places=2)
        self.assertAlmostEqual(float(row.get("returns_profit_impact") or 0.0), -75.0, places=2)
        self.assertAlmostEqual(float(row.get("estimated_net_before_cogs_before_returns") or 0.0), 91.0, places=2)
        self.assertAlmostEqual(float(row.get("estimated_lot_cogs_before_returns") or 0.0), 25.0, places=2)
        self.assertAlmostEqual(float(row.get("estimated_lot_profit_before_returns") or 0.0), 66.0, places=2)
        self.assertAlmostEqual(float(row.get("estimated_net_before_cogs_from_lot") or 0.0), -9.0, places=2)
        self.assertAlmostEqual(float(row.get("estimated_lot_cogs") or 0.0), 0.0, places=2)
        self.assertAlmostEqual(float(row.get("estimated_lot_profit") or 0.0), -9.0, places=2)
        self.assertAlmostEqual(float(summary.get("returns_refund_total") or 0.0), 100.0, places=2)
        self.assertAlmostEqual(float(summary.get("returns_cogs_reversal") or 0.0), 25.0, places=2)
        self.assertAlmostEqual(float(summary.get("estimated_lot_profit_before_returns") or 0.0), 66.0, places=2)
        self.assertAlmostEqual(float(summary.get("estimated_lot_profit") or 0.0), -9.0, places=2)

    def test_lot_profitability_snapshot_uses_bundle_components_without_sale_product_link(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        primary = self._create_product(sku="GS-LOT-PNL-BUNDLE-PRIMARY", qty=10)
        extra = self._create_product(sku="GS-LOT-PNL-BUNDLE-EXTRA", qty=10)
        primary_lot = self.repo.create_purchase_lot(
            lot_code="LOT-PNL-BUNDLE-PRIMARY",
            vendor="Dealer",
            purchase_date=now - timedelta(days=3),
            total_cost=Decimal("20.00"),
        )
        extra_lot = self.repo.create_purchase_lot(
            lot_code="LOT-PNL-BUNDLE-EXTRA",
            vendor="Dealer",
            purchase_date=now - timedelta(days=3),
            total_cost=Decimal("60.00"),
        )
        self.repo.assign_product_to_lot(
            product_id=primary.id,
            lot_id=primary_lot.id,
            quantity_acquired=2,
            unit_cost=Decimal("10.00"),
            acquired_at=now - timedelta(days=3),
        )
        self.repo.assign_product_to_lot(
            product_id=extra.id,
            lot_id=extra_lot.id,
            quantity_acquired=3,
            unit_cost=Decimal("20.00"),
            acquired_at=now - timedelta(days=3),
        )
        listing = self.repo.create_listing(
            product_id=primary.id,
            marketplace="ebay",
            listing_title="Lot P/L bundle",
            listing_price=Decimal("150.00"),
            quantity_listed=1,
            marketplace_details=json.dumps(
                {
                    "bundle": {
                        "enabled": True,
                        "kind": "mixed_product_bundle",
                        "components": [
                            {"product_id": primary.id, "sku": primary.sku, "quantity_per_listing": 2},
                            {"product_id": extra.id, "sku": extra.sku, "quantity_per_listing": 3},
                        ],
                    }
                }
            ),
            actor="qa-user",
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("150.00"),
            fees=Decimal("15.00"),
            shipping_cost=Decimal("5.00"),
            shipping_label_cost=Decimal("5.00"),
            quantity_sold=1,
            product_id=None,
            listing_id=listing.id,
            sold_at=now - timedelta(days=1),
        )
        self.repo.create_return(
            marketplace="ebay",
            sale_id=sale.id,
            quantity=1,
            refund_amount=Decimal("150.00"),
            return_status="processed",
            disposition="restock",
            restocked=True,
            returned_at=now - timedelta(hours=12),
            actor="qa-user",
        )

        primary_snapshot = self.repo.lot_profitability_snapshot(primary_lot.id)
        primary_row = primary_snapshot["rows"][0]
        extra_snapshot = self.repo.lot_profitability_snapshot(extra_lot.id)
        extra_row = extra_snapshot["rows"][0]

        self.assertEqual(int(primary_row.get("qty_sold_est_from_lot") or 0), 2)
        self.assertEqual(int(extra_row.get("qty_sold_est_from_lot") or 0), 3)
        self.assertAlmostEqual(float(primary_row.get("estimated_gross_sales_from_lot") or 0.0), 60.0, places=2)
        self.assertAlmostEqual(float(extra_row.get("estimated_gross_sales_from_lot") or 0.0), 90.0, places=2)
        self.assertAlmostEqual(float(primary_row.get("returns_refund_total") or 0.0), 60.0, places=2)
        self.assertAlmostEqual(float(extra_row.get("returns_refund_total") or 0.0), 90.0, places=2)
        self.assertAlmostEqual(float(primary_row.get("returns_cogs_reversal") or 0.0), 20.0, places=2)
        self.assertAlmostEqual(float(extra_row.get("returns_cogs_reversal") or 0.0), 60.0, places=2)
        self.assertAlmostEqual(float(primary_row.get("estimated_lot_profit") or 0.0), -6.0, places=2)
        self.assertAlmostEqual(float(extra_row.get("estimated_lot_profit") or 0.0), -9.0, places=2)

    def test_lot_profitability_snapshot_uses_fifo_lot_consumption_for_repurchase_products(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self.repo.create_product(
            sku="GS-LOT-PNL-FIFO",
            title="Lot P/L FIFO Product",
            category="bullion",
            description="",
            metal_type="silver",
            weight_oz=None,
            acquisition_cost=None,
            product_cost=None,
            current_quantity=2,
            acquired_at=now - timedelta(days=20),
        )
        lot1 = self.repo.create_purchase_lot(
            lot_code="LOT-PNL-FIFO-OLD",
            vendor="Dealer",
            purchase_date=now - timedelta(days=10),
            total_cost=Decimal("10.00"),
        )
        lot2 = self.repo.create_purchase_lot(
            lot_code="LOT-PNL-FIFO-NEW",
            vendor="Dealer",
            purchase_date=now - timedelta(days=5),
            total_cost=Decimal("60.00"),
        )
        self.repo.assign_product_to_lot(
            product_id=product.id,
            lot_id=lot1.id,
            quantity_acquired=1,
            unit_cost=Decimal("10.00"),
            acquired_at=now - timedelta(days=10),
        )
        self.repo.assign_product_to_lot(
            product_id=product.id,
            lot_id=lot2.id,
            quantity_acquired=3,
            unit_cost=Decimal("20.00"),
            acquired_at=now - timedelta(days=5),
        )
        self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("0.00"),
            shipping_label_cost=Decimal("0.00"),
            quantity_sold=2,
            product_id=product.id,
            sold_at=now - timedelta(days=7),
        )
        self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("120.00"),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("0.00"),
            shipping_label_cost=Decimal("0.00"),
            quantity_sold=1,
            product_id=product.id,
            sold_at=now - timedelta(days=1),
        )

        old_snapshot = self.repo.lot_profitability_snapshot(lot1.id)
        old_row = old_snapshot["rows"][0]
        self.assertEqual(int(old_row.get("qty_sold_est_from_lot") or 0), 1)
        self.assertAlmostEqual(float(old_row.get("estimated_gross_sales_from_lot") or 0.0), 50.0, places=2)
        self.assertAlmostEqual(float(old_row.get("estimated_lot_cogs") or 0.0), 10.0, places=2)

        new_snapshot = self.repo.lot_profitability_snapshot(lot2.id)
        new_row = new_snapshot["rows"][0]
        self.assertEqual(int(new_row.get("qty_sold_est_from_lot") or 0), 1)
        self.assertAlmostEqual(float(new_row.get("estimated_gross_sales_from_lot") or 0.0), 120.0, places=2)
        self.assertAlmostEqual(float(new_row.get("estimated_lot_cogs") or 0.0), 20.0, places=2)
        self.assertAlmostEqual(float(new_row.get("estimated_lot_profit") or 0.0), 100.0, places=2)
        self.assertEqual(new_row.get("cost_source"), "assignment_unit_landed_cost")
        self.assertAlmostEqual(
            float((new_snapshot["summary"].get("cost_source_totals") or {}).get("assignment_unit_landed_cost") or 0.0),
            20.0,
            places=2,
        )

    def test_report_products_and_listings_rows_date_bounded(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product_in = self._create_product(sku="GS-PROD-RPT-IN", qty=4)
        product_out = self._create_product(sku="GS-PROD-RPT-OUT", qty=2)
        product_in.acquired_at = now - timedelta(days=1)
        product_in.category = "coin"
        product_out.acquired_at = now - timedelta(days=40)
        self.db.flush()

        self.repo.create_listing(
            product_id=product_in.id,
            marketplace="ebay",
            listing_title="Listing In",
            listing_price=Decimal("25.00"),
            quantity_listed=1,
            listed_at=now - timedelta(days=1),
            listing_status="active",
            actor="qa-user",
        )
        self.repo.create_listing(
            product_id=product_out.id,
            marketplace="ebay",
            listing_title="Listing Out",
            listing_price=Decimal("26.00"),
            quantity_listed=1,
            listed_at=now - timedelta(days=40),
            listing_status="active",
            actor="qa-user",
        )

        product_rows = self.repo.report_products_rows(
            start_dt=now - timedelta(days=7),
            end_dt=now,
        )
        listing_rows = self.repo.report_listings_rows(
            start_dt=now - timedelta(days=7),
            end_dt=now,
        )

        self.assertEqual(len(product_rows), 1)
        self.assertEqual(int(product_rows[0].get("product_id") or 0), int(product_in.id))
        self.assertEqual(str(product_rows[0].get("sku") or ""), str(product_in.sku or ""))
        self.assertEqual(str(product_rows[0].get("category") or ""), "coin")

        self.assertEqual(len(listing_rows), 1)
        self.assertEqual(str(listing_rows[0].get("listing_title") or ""), "Listing In")
        self.assertEqual(str(listing_rows[0].get("sku") or ""), str(product_in.sku or ""))

    def test_report_sales_rows_date_bounded(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-SALE-RPT-001", qty=10)
        self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("50.00"),
            fees=Decimal("4.00"),
            shipping_cost=Decimal("3.00"),
            quantity_sold=1,
            product_id=product.id,
            external_order_id="SALE-IN",
            shipping_provider="USPS",
            shipping_service="Ground",
            tracking_status="shipped",
            sold_at=now - timedelta(days=1),
        )
        self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("60.00"),
            fees=Decimal("5.00"),
            shipping_cost=Decimal("3.50"),
            quantity_sold=1,
            product_id=product.id,
            external_order_id="SALE-OUT",
            sold_at=now - timedelta(days=40),
        )

        rows = self.repo.report_sales_rows(
            start_dt=now - timedelta(days=7),
            end_dt=now,
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(str(row.get("external_order_id") or ""), "SALE-IN")
        self.assertEqual(str(row.get("sku") or ""), str(product.sku or ""))
        self.assertAlmostEqual(float(row.get("sold_price") or 0.0), 50.0, places=2)
        self.assertEqual(str(row.get("shipping_provider") or ""), "USPS")

    def test_report_sales_rows_include_linked_normalized_actuals(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-SALE-RPT-NORM", qty=10)
        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="SALE-RPT-NORM",
            order_status="paid",
            sold_at=now - timedelta(days=1),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("5.00"),
            items=[{"product_id": product.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("100.00")}],
            actor="qa-user",
        )
        self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("10.00"),
            shipping_cost=Decimal("5.00"),
            shipping_label_cost=Decimal("9.00"),
            quantity_sold=1,
            product_id=product.id,
            order_id=order.id,
            external_order_id=order.external_order_id,
            sold_at=now - timedelta(days=1),
        )
        self.repo.replace_order_finance_entries(
            order.id,
            [
                {"entry_kind": "marketplace_fee", "fee_type": "FINAL_VALUE_FEE", "amount": Decimal("7.50")},
                {"entry_kind": "shipping_label", "fee_type": "SHIPPING_LABEL", "amount": Decimal("4.25")},
            ],
            actor="qa-user",
        )

        rows = self.repo.report_sales_rows(start_dt=now - timedelta(days=7), end_dt=now)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertAlmostEqual(float(row.get("field_net_before_cogs") or 0.0), 86.0, places=2)
        self.assertAlmostEqual(float(row.get("actual_fee") or 0.0), 7.5, places=2)
        self.assertAlmostEqual(float(row.get("actual_shipping_charged") or 0.0), 5.0, places=2)
        self.assertAlmostEqual(float(row.get("actual_shipping_label_cost") or 0.0), 4.25, places=2)
        self.assertAlmostEqual(float(row.get("actual_net_before_cogs") or 0.0), 93.25, places=2)
        self.assertEqual(row.get("actual_fee_source"), "normalized_order_finance_entries_marketplace_fee_sum")
        self.assertEqual(row.get("actual_shipping_source"), "normalized_order_finance_entries_shipping_label_sum")

    def test_report_returns_rows_date_bounded(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-RET-RPT-001", qty=8)
        sale_in = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("30.00"),
            fees=Decimal("2.00"),
            shipping_cost=Decimal("3.00"),
            quantity_sold=1,
            product_id=product.id,
            sold_at=now - timedelta(days=1),
        )
        sale_out = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("31.00"),
            fees=Decimal("2.00"),
            shipping_cost=Decimal("3.00"),
            quantity_sold=1,
            product_id=product.id,
            sold_at=now - timedelta(days=35),
        )
        self.repo.create_return(
            marketplace="ebay",
            external_return_id="RET-IN",
            sale_id=sale_in.id,
            product_id=product.id,
            return_status="requested",
            reason="Changed mind",
            quantity=1,
            refund_amount=Decimal("30.00"),
            refund_fees=Decimal("2.00"),
            refund_shipping=Decimal("1.00"),
            returned_at=now - timedelta(days=1),
            actor="qa-user",
        )
        self.repo.create_return(
            marketplace="ebay",
            external_return_id="RET-OUT",
            sale_id=sale_out.id,
            product_id=product.id,
            return_status="requested",
            reason="Damaged",
            quantity=1,
            refund_amount=Decimal("31.00"),
            refund_fees=Decimal("2.00"),
            refund_shipping=Decimal("1.00"),
            returned_at=now - timedelta(days=35),
            actor="qa-user",
        )

        rows = self.repo.report_returns_rows(
            start_dt=now - timedelta(days=7),
            end_dt=now,
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(str(row.get("external_return_id") or ""), "RET-IN")
        self.assertEqual(int(row.get("sale_id") or 0), int(sale_in.id))
        self.assertEqual(str(row.get("sku") or ""), str(product.sku or ""))
        self.assertAlmostEqual(float(row.get("refund_amount") or 0.0), 30.0, places=2)

    def test_report_lot_assignment_rows_date_bounded(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-LOT-RPT-001", qty=0)
        lot_in = self.repo.create_purchase_lot(
            lot_code="LOT-RPT-IN",
            vendor="Vendor In",
            purchase_date=now - timedelta(days=2),
            total_cost=Decimal("100.00"),
        )
        lot_out = self.repo.create_purchase_lot(
            lot_code="LOT-RPT-OUT",
            vendor="Vendor Out",
            purchase_date=now - timedelta(days=40),
            total_cost=Decimal("90.00"),
        )
        self.repo.assign_product_to_lot(
            product_id=product.id,
            lot_id=lot_in.id,
            quantity_acquired=2,
            unit_cost=Decimal("10.00"),
            acquired_at=now - timedelta(days=2),
        )
        self.repo.assign_product_to_lot(
            product_id=product.id,
            lot_id=lot_out.id,
            quantity_acquired=1,
            unit_cost=Decimal("9.00"),
            acquired_at=now - timedelta(days=40),
        )

        rows = self.repo.report_lot_assignment_rows(
            start_dt=now - timedelta(days=7),
            end_dt=now,
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(str(row.get("lot_code") or ""), "LOT-RPT-IN")
        self.assertEqual(str(row.get("sku") or ""), str(product.sku or ""))
        self.assertEqual(int(row.get("quantity_acquired") or 0), 2)
        self.assertAlmostEqual(float(row.get("unit_cost") or 0.0), 10.0, places=2)
        self.assertAlmostEqual(float(row.get("resolved_landed_unit_cost") or 0.0), 10.0, places=2)
        self.assertAlmostEqual(float(row.get("resolved_landed_total_cost") or 0.0), 20.0, places=2)
        self.assertEqual(str(row.get("cost_source") or ""), "assignment_unit_landed_cost")

        all_rows = self.repo.report_lot_assignment_rows(
            start_dt=None,
            end_dt=now,
        )
        lot_codes = {str(row.get("lot_code") or "") for row in all_rows}
        self.assertIn("LOT-RPT-IN", lot_codes)
        self.assertIn("LOT-RPT-OUT", lot_codes)

    def test_report_sale_unit_cost_maps(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-COSTMAP-RPT-001", qty=10)
        lot1 = self.repo.create_purchase_lot(
            lot_code="LOT-COSTMAP-1",
            vendor="Vendor",
            purchase_date=now - timedelta(days=10),
            total_cost=Decimal("30.00"),
        )
        lot2 = self.repo.create_purchase_lot(
            lot_code="LOT-COSTMAP-2",
            vendor="Vendor",
            purchase_date=now - timedelta(days=5),
            total_cost=Decimal("28.00"),
        )
        self.repo.assign_product_to_lot(
            product_id=product.id,
            lot_id=lot1.id,
            quantity_acquired=3,
            unit_cost=Decimal("10.00"),
            acquired_at=now - timedelta(days=10),
        )
        self.repo.assign_product_to_lot(
            product_id=product.id,
            lot_id=lot2.id,
            quantity_acquired=2,
            unit_cost=Decimal("14.00"),
            acquired_at=now - timedelta(days=5),
        )
        sale1 = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("40.00"),
            fees=Decimal("3.00"),
            shipping_cost=Decimal("2.00"),
            quantity_sold=2,
            product_id=product.id,
            sold_at=now - timedelta(days=2),
        )
        sale2 = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("60.00"),
            fees=Decimal("4.00"),
            shipping_cost=Decimal("3.00"),
            quantity_sold=3,
            product_id=product.id,
            sold_at=now - timedelta(days=1),
        )

        payload = self.repo.report_sale_unit_cost_maps(
            end_dt=now,
            default_unit_cost_by_product={int(product.id): 9.0},
        )
        fifo_map = payload.get("fifo_unit_cost_by_sale") or {}
        total_map = payload.get("fifo_total_cost_by_sale") or {}
        evidence_map = payload.get("fifo_cogs_evidence_by_sale") or {}
        weighted_map = payload.get("lot_weighted_unit_cost_by_product") or {}
        self.assertAlmostEqual(float(fifo_map.get(int(sale1.id)) or 0.0), 10.0, places=3)
        self.assertAlmostEqual(float(fifo_map.get(int(sale2.id)) or 0.0), (38.0 / 3.0), places=3)
        self.assertAlmostEqual(float(total_map.get(int(sale2.id)) or 0.0), 38.0, places=3)
        sale2_evidence = evidence_map.get(int(sale2.id)) or []
        self.assertEqual(len(sale2_evidence), 2)
        self.assertEqual([int(row.get("quantity") or 0) for row in sale2_evidence], [1, 2])
        self.assertEqual([int(row.get("lot_id") or 0) for row in sale2_evidence], [int(lot1.id), int(lot2.id)])
        self.assertEqual(
            [str(row.get("cost_source") or "") for row in sale2_evidence],
            ["assignment_unit_landed_cost", "assignment_unit_landed_cost"],
        )
        self.assertAlmostEqual(float(sale2_evidence[0].get("unit_cost") or 0.0), 10.0, places=3)
        self.assertAlmostEqual(float(sale2_evidence[1].get("unit_cost") or 0.0), 14.0, places=3)
        self.assertAlmostEqual(float(weighted_map.get(int(product.id)) or 0.0), 11.6, places=3)

        metrics = self.repo.dashboard_live_metrics(now=now)
        self.assertEqual(metrics["sales_30d_profit_basis_status"], "ok")
        self.assertEqual(metrics["sales_30d_cogs_review_count"], 0)
        self.assertAlmostEqual(metrics["sales_30d_cogs_review_amount"], 0.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_cogs_verified_amount"], 58.0, places=2)
        self.assertEqual(metrics["sales_30d_cogs_source_counts"], {"assignment_unit_landed_cost": 2})

    def test_report_sale_unit_cost_maps_do_not_consume_future_repurchase_lots(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self.repo.create_product(
            sku="GS-COSTMAP-FUTURE-LOT",
            title="Future Lot Guard",
            category="bullion",
            description="",
            metal_type="silver",
            weight_oz=None,
            acquisition_cost=None,
            product_cost=None,
            current_quantity=10,
            acquired_at=now - timedelta(days=20),
        )
        lot1 = self.repo.create_purchase_lot(
            lot_code="LOT-FIFO-OLD",
            vendor="Vendor",
            purchase_date=now - timedelta(days=10),
            total_cost=Decimal("10.00"),
        )
        lot2 = self.repo.create_purchase_lot(
            lot_code="LOT-FIFO-FUTURE",
            vendor="Vendor",
            purchase_date=now - timedelta(days=5),
            total_cost=Decimal("60.00"),
        )
        self.repo.assign_product_to_lot(
            product_id=product.id,
            lot_id=lot1.id,
            quantity_acquired=1,
            unit_cost=Decimal("10.00"),
            acquired_at=now - timedelta(days=10),
        )
        self.repo.assign_product_to_lot(
            product_id=product.id,
            lot_id=lot2.id,
            quantity_acquired=3,
            unit_cost=Decimal("20.00"),
            acquired_at=now - timedelta(days=5),
        )
        sale_before_lot2 = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("0.00"),
            quantity_sold=2,
            product_id=product.id,
            sold_at=now - timedelta(days=7),
        )
        sale_after_lot2 = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("0.00"),
            quantity_sold=1,
            product_id=product.id,
            sold_at=now - timedelta(days=1),
        )

        payload = self.repo.report_sale_unit_cost_maps(
            end_dt=now,
            default_unit_cost_by_product={int(product.id): 0.0},
        )
        fifo_map = payload.get("fifo_unit_cost_by_sale") or {}
        self.assertAlmostEqual(float(fifo_map.get(int(sale_before_lot2.id)) or 0.0), 5.0, places=3)
        self.assertAlmostEqual(float(fifo_map.get(int(sale_after_lot2.id)) or 0.0), 20.0, places=3)

    def test_dashboard_inventory_cost_uses_fifo_remaining_cost_after_multiple_repurchase_lots(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self.repo.create_product(
            sku="GS-DASH-MULTI-LOT-REPURCHASE",
            title="Multi Lot Repurchase",
            category="bullion",
            description="",
            metal_type="silver",
            weight_oz=None,
            acquisition_cost=None,
            product_cost=None,
            current_quantity=0,
            acquired_at=now - timedelta(days=12),
        )
        lot1 = self.repo.create_purchase_lot(
            lot_code="LOT-REPURCHASE-OLD",
            vendor="Vendor",
            purchase_date=now - timedelta(days=10),
            total_cost=Decimal("20.00"),
        )
        lot2 = self.repo.create_purchase_lot(
            lot_code="LOT-REPURCHASE-NEW",
            vendor="Vendor",
            purchase_date=now - timedelta(days=5),
            total_cost=Decimal("60.00"),
        )
        self.repo.record_product_repurchase(
            product_id=product.id,
            quantity_acquired=2,
            unit_cost=Decimal("10.00"),
            acquired_at=now - timedelta(days=10),
            lot_id=lot1.id,
            actor="qa-user",
        )
        sale1 = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("50.00"),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("0.00"),
            quantity_sold=1,
            product_id=product.id,
            sold_at=now - timedelta(days=8),
        )
        self.repo.record_product_repurchase(
            product_id=product.id,
            quantity_acquired=3,
            unit_cost=Decimal("20.00"),
            acquired_at=now - timedelta(days=5),
            lot_id=lot2.id,
            actor="qa-user",
        )
        sale2 = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("150.00"),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("0.00"),
            quantity_sold=3,
            product_id=product.id,
            sold_at=now - timedelta(days=1),
        )

        payload = self.repo.report_sale_unit_cost_maps(
            end_dt=now,
            default_unit_cost_by_product={int(product.id): 0.0},
        )
        fifo_map = payload.get("fifo_unit_cost_by_sale") or {}
        remaining_map = payload.get("fifo_remaining_unit_cost_by_product") or {}
        self.assertAlmostEqual(float(fifo_map.get(int(sale1.id)) or 0.0), 10.0, places=3)
        self.assertAlmostEqual(float(fifo_map.get(int(sale2.id)) or 0.0), (50.0 / 3.0), places=3)
        self.assertAlmostEqual(float(remaining_map.get(int(product.id)) or 0.0), 20.0, places=3)

        metrics = self.repo.dashboard_metrics()
        self.assertAlmostEqual(metrics["inventory_cost"], 20.0, places=2)

    def test_report_accounting_exception_rows_flags_cost_shipping_fee_and_lot_issues(self) -> None:
        now = datetime(2026, 4, 26, 12, 0, 0)
        missing_cost_product = self.repo.create_product(
            sku="GS-ACCT-EX-MISSING",
            title="Missing Cost Product",
            category="coin",
            description="",
            metal_type="silver",
            weight_oz=None,
            acquisition_cost=None,
            product_cost=None,
            current_quantity=2,
            acquired_at=now - timedelta(days=5),
        )
        missing_cost_sale = self.repo.create_sale(
            marketplace="ebay",
            external_order_id="EBAY-ACCT-MISSING",
            sold_price=Decimal("20.00"),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("5.00"),
            shipping_label_cost=None,
            quantity_sold=1,
            product_id=missing_cost_product.id,
            sold_at=now - timedelta(days=1),
        )
        negative_margin_product = self.repo.create_product(
            sku="GS-ACCT-EX-MARGIN",
            title="Negative Margin Product",
            category="coin",
            description="",
            metal_type="silver",
            weight_oz=None,
            acquisition_cost=None,
            product_cost=Decimal("20.00"),
            current_quantity=2,
            acquired_at=now - timedelta(days=5),
        )
        negative_margin_sale = self.repo.create_sale(
            marketplace="local",
            external_order_id="LOCAL-ACCT-MARGIN",
            sold_price=Decimal("10.00"),
            fees=Decimal("1.00"),
            shipping_cost=Decimal("0.00"),
            shipping_label_cost=Decimal("0.00"),
            quantity_sold=1,
            product_id=negative_margin_product.id,
            sold_at=now - timedelta(hours=12),
        )
        p1 = self.repo.create_product(
            sku="GS-ACCT-EX-LOT-1",
            title="Lot Over Product 1",
            category="coin",
            description="",
            metal_type="silver",
            weight_oz=None,
            acquisition_cost=None,
            current_quantity=1,
            acquired_at=now - timedelta(days=5),
        )
        p2 = self.repo.create_product(
            sku="GS-ACCT-EX-LOT-2",
            title="Lot Over Product 2",
            category="coin",
            description="",
            metal_type="silver",
            weight_oz=None,
            acquisition_cost=None,
            current_quantity=1,
            acquired_at=now - timedelta(days=5),
        )
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-ACCT-OVER",
            vendor="Vendor",
            purchase_date=now - timedelta(days=4),
            total_cost=Decimal("10.00"),
        )
        self.repo.assign_product_to_lot(
            product_id=p1.id,
            lot_id=lot.id,
            quantity_acquired=1,
            unit_cost=Decimal("7.00"),
            acquired_at=now - timedelta(days=4),
        )
        self.repo.assign_product_to_lot(
            product_id=p2.id,
            lot_id=lot.id,
            quantity_acquired=1,
            unit_cost=Decimal("7.00"),
            acquired_at=now - timedelta(days=4),
        )

        rows = self.repo.report_accounting_exception_rows(
            start_dt=now - timedelta(days=7),
            end_dt=now,
        )
        by_type = {}
        for row in rows:
            by_type.setdefault(str(row.get("exception_type")), []).append(row)

        self.assertIn("missing_cost_basis", by_type)
        self.assertIn("missing_shipping_label_spend", by_type)
        self.assertIn("missing_fee_evidence", by_type)
        self.assertIn("fee_source_fallback", by_type)
        self.assertIn("nonpositive_margin", by_type)
        self.assertIn("lot_overallocated", by_type)
        missing_cost_exception = next(
            row
            for row in by_type["missing_cost_basis"]
            if int(row.get("entity_id") or 0) == int(missing_cost_sale.id)
        )
        negative_margin_exception = next(
            row
            for row in by_type["nonpositive_margin"]
            if int(row.get("entity_id") or 0) == int(negative_margin_sale.id)
        )
        self.assertIn("Source=missing_cost_basis", str(missing_cost_exception.get("details") or ""))
        self.assertIn("Sale evidence:", str(missing_cost_exception.get("details") or ""))
        self.assertIn(f"product_id={missing_cost_product.id}", str(missing_cost_exception.get("details") or ""))
        self.assertIn("Correction path:", str(missing_cost_exception.get("details") or ""))
        self.assertIn("COGS source=product_default_landed_cost", str(negative_margin_exception.get("details") or ""))
        self.assertIn("Gross 10.00 + shipping charged 0.00 - fees 1.00", str(negative_margin_exception.get("details") or ""))
        self.assertIn("Review focus:", str(negative_margin_exception.get("details") or ""))
        self.assertIn("Evidence:", str(negative_margin_exception.get("details") or ""))
        self.assertAlmostEqual(float(by_type["lot_overallocated"][0].get("amount") or 0.0), 4.0, places=2)
        self.assertEqual(str(by_type["lot_overallocated"][0].get("severity") or ""), "P0")
        self.assertIn("exceeds lot landed total", str(by_type["lot_overallocated"][0].get("details") or ""))
        self.assertIn("Correct assignment", str(by_type["lot_overallocated"][0].get("details") or ""))

    def test_accounting_exception_ignores_immaterial_lot_reconciliation_deltas(self) -> None:
        now = datetime(2026, 4, 26, 12, 0, 0)
        product = self.repo.create_product(
            sku="GS-ACCT-IMMATERIAL-DELTA",
            title="Immaterial Delta Product",
            category="coin",
            description="",
            metal_type="silver",
            weight_oz=None,
            acquisition_cost=None,
            current_quantity=2,
            acquired_at=now - timedelta(days=2),
        )
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-ACCT-IMMATERIAL",
            vendor="Vendor",
            purchase_date=now - timedelta(days=2),
            total_cost=Decimal("100.00"),
        )
        self.repo.assign_product_to_lot(
            product_id=product.id,
            lot_id=lot.id,
            quantity_acquired=2,
            unit_cost=Decimal("50.10"),
            acquired_at=now - timedelta(days=2),
        )

        rows = self.repo.report_accounting_exception_rows(
            start_dt=now - timedelta(days=7),
            end_dt=now,
        )

        lot_rows = [row for row in rows if int(row.get("entity_id") or 0) == int(lot.id)]
        self.assertFalse(
            [
                row
                for row in lot_rows
                if str(row.get("exception_type") or "") in {"lot_overallocated", "lot_underallocated"}
            ]
        )

    def test_accounting_exception_marks_small_lot_underallocation_as_cleanup(self) -> None:
        now = datetime(2026, 4, 26, 12, 0, 0)
        product = self.repo.create_product(
            sku="GS-ACCT-SMALL-UNDER",
            title="Small Underallocation Product",
            category="coin",
            description="",
            metal_type="silver",
            weight_oz=None,
            acquisition_cost=None,
            current_quantity=1,
            acquired_at=now - timedelta(days=2),
        )
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-ACCT-SMALL-UNDER",
            vendor="Vendor",
            purchase_date=now - timedelta(days=2),
            total_cost=Decimal("50.93"),
        )
        self.repo.assign_product_to_lot(
            product_id=product.id,
            lot_id=lot.id,
            quantity_acquired=1,
            unit_cost=Decimal("48.00"),
            acquired_at=now - timedelta(days=2),
        )

        rows = self.repo.report_accounting_exception_rows(
            start_dt=now - timedelta(days=7),
            end_dt=now,
        )
        row = next(
            row
            for row in rows
            if int(row.get("entity_id") or 0) == int(lot.id)
            and str(row.get("exception_type") or "") == "lot_underallocated"
        )

        self.assertEqual(str(row.get("severity") or ""), "P2")
        self.assertAlmostEqual(float(row.get("amount") or 0.0), 2.93, places=2)
        self.assertIn("5.8%", str(row.get("details") or ""))

    def test_accounting_exception_keeps_material_lot_underallocation_actionable(self) -> None:
        now = datetime(2026, 4, 26, 12, 0, 0)
        product = self.repo.create_product(
            sku="GS-ACCT-MATERIAL-UNDER",
            title="Material Underallocation Product",
            category="coin",
            description="",
            metal_type="silver",
            weight_oz=None,
            acquisition_cost=None,
            current_quantity=1,
            acquired_at=now - timedelta(days=2),
        )
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-ACCT-MATERIAL-UNDER",
            vendor="Vendor",
            purchase_date=now - timedelta(days=2),
            total_cost=Decimal("50.00"),
        )
        self.repo.assign_product_to_lot(
            product_id=product.id,
            lot_id=lot.id,
            quantity_acquired=1,
            unit_cost=Decimal("40.00"),
            acquired_at=now - timedelta(days=2),
        )

        rows = self.repo.report_accounting_exception_rows(
            start_dt=now - timedelta(days=7),
            end_dt=now,
        )
        row = next(
            row
            for row in rows
            if int(row.get("entity_id") or 0) == int(lot.id)
            and str(row.get("exception_type") or "") == "lot_underallocated"
        )

        self.assertEqual(str(row.get("severity") or ""), "P1")
        self.assertAlmostEqual(float(row.get("amount") or 0.0), 10.0, places=2)
        self.assertIn("20.0%", str(row.get("details") or ""))

    def test_accounting_exception_suppression_hides_and_restores_row(self) -> None:
        now = datetime(2026, 4, 26, 12, 0, 0)
        product = self.repo.create_product(
            sku="GS-ACCT-SUPPRESS-UNDER",
            title="Suppressible Underallocation Product",
            category="coin",
            description="",
            metal_type="silver",
            weight_oz=None,
            acquisition_cost=None,
            current_quantity=1,
            acquired_at=now - timedelta(days=2),
        )
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-ACCT-SUPPRESS-UNDER",
            vendor="Vendor",
            purchase_date=now - timedelta(days=2),
            total_cost=Decimal("50.00"),
        )
        self.repo.assign_product_to_lot(
            product_id=product.id,
            lot_id=lot.id,
            quantity_acquired=1,
            unit_cost=Decimal("40.00"),
            acquired_at=now - timedelta(days=2),
        )

        rows = self.repo.report_accounting_exception_rows(
            start_dt=now - timedelta(days=7),
            end_dt=now,
        )
        self.assertTrue(
            [
                row
                for row in rows
                if int(row.get("entity_id") or 0) == int(lot.id)
                and str(row.get("entity_type") or "") == "purchase_lot"
                and str(row.get("exception_type") or "") == "lot_underallocated"
            ]
        )

        self.repo.suppress_accounting_exception(
            exception_type="lot_underallocated",
            target_entity_type="purchase_lot",
            target_entity_id=lot.id,
            actor="qa-user",
            reason="False positive for imported legacy lot.",
            details="Accepted as no repair needed.",
        )
        self.assertIn(
            ("lot_underallocated", "purchase_lot", int(lot.id)),
            self.repo.accounting_exception_suppression_keys(),
        )
        suppressed_rows = self.repo.report_accounting_exception_rows(
            start_dt=now - timedelta(days=7),
            end_dt=now,
        )
        self.assertFalse(
            [
                row
                for row in suppressed_rows
                if int(row.get("entity_id") or 0) == int(lot.id)
                and str(row.get("entity_type") or "") == "purchase_lot"
                and str(row.get("exception_type") or "") == "lot_underallocated"
            ]
        )

        self.repo.unsuppress_accounting_exception(
            exception_type="lot_underallocated",
            target_entity_type="purchase_lot",
            target_entity_id=lot.id,
            actor="qa-user",
            reason="Needs review again.",
        )
        self.assertNotIn(
            ("lot_underallocated", "purchase_lot", int(lot.id)),
            self.repo.accounting_exception_suppression_keys(),
        )
        restored_rows = self.repo.report_accounting_exception_rows(
            start_dt=now - timedelta(days=7),
            end_dt=now,
        )
        self.assertTrue(
            [
                row
                for row in restored_rows
                if int(row.get("entity_id") or 0) == int(lot.id)
                and str(row.get("entity_type") or "") == "purchase_lot"
                and str(row.get("exception_type") or "") == "lot_underallocated"
            ]
        )
        audit_actions = [
            row.action
            for row in self.db.scalars(
                select(AuditLog)
                .where(AuditLog.entity_type == "accounting_exception_suppress")
                .order_by(AuditLog.id.asc())
            ).all()
        ]
        self.assertEqual(audit_actions, ["suppress", "unsuppress"])

    def test_repair_purchase_lot_assignment_allocations_clears_underallocation(self) -> None:
        now = datetime(2026, 4, 26, 12, 0, 0)
        p1 = self._create_product(sku="GS-REPAIR-LOT-UNDER-1", qty=1)
        p2 = self._create_product(sku="GS-REPAIR-LOT-UNDER-2", qty=1)
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-REPAIR-UNDER",
            vendor="Vendor",
            purchase_date=now - timedelta(days=2),
            total_cost=Decimal("50.00"),
            total_shipping_paid=Decimal("5.00"),
        )
        a1 = self.repo.assign_product_to_lot(
            product_id=p1.id,
            lot_id=lot.id,
            quantity_acquired=1,
            unit_cost=Decimal("20.00"),
            acquired_at=now - timedelta(days=2),
        )
        a2 = self.repo.assign_product_to_lot(
            product_id=p2.id,
            lot_id=lot.id,
            quantity_acquired=1,
            unit_cost=Decimal("25.00"),
            acquired_at=now - timedelta(days=2),
        )

        preview = self.repo.repair_purchase_lot_assignment_allocations(lot.id, actor="qa-user", dry_run=True)
        self.assertTrue(preview["dry_run"])
        self.assertAlmostEqual(float(preview["target_landed_total"]), 55.0, places=2)
        self.assertAlmostEqual(float(preview["current_assignment_landed_total"]), 45.0, places=2)

        result = self.repo.repair_purchase_lot_assignment_allocations(lot.id, actor="qa-user")
        self.assertFalse(result["dry_run"])
        self.assertAlmostEqual(float(result["delta"]), 10.0, places=2)

        refreshed_a1 = self.db.get(type(a1), a1.id)
        refreshed_a2 = self.db.get(type(a2), a2.id)
        assigned_landed = (
            Decimal(refreshed_a1.allocated_cost or 0)
            + Decimal(refreshed_a1.allocated_shipping_paid or 0)
            + Decimal(refreshed_a2.allocated_cost or 0)
            + Decimal(refreshed_a2.allocated_shipping_paid or 0)
        )
        self.assertEqual(assigned_landed, Decimal("55.00"))
        rows = self.repo.report_accounting_exception_rows(start_dt=now - timedelta(days=7), end_dt=now)
        self.assertFalse(
            [
                row
                for row in rows
                if int(row.get("entity_id") or 0) == int(lot.id)
                and str(row.get("exception_type") or "") == "lot_underallocated"
            ]
        )

    def test_repair_purchase_lot_assignment_allocations_clears_overallocation(self) -> None:
        now = datetime(2026, 4, 26, 12, 0, 0)
        p1 = self._create_product(sku="GS-REPAIR-LOT-OVER-1", qty=1)
        p2 = self._create_product(sku="GS-REPAIR-LOT-OVER-2", qty=1)
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-REPAIR-OVER",
            vendor="Vendor",
            purchase_date=now - timedelta(days=2),
            total_cost=Decimal("40.00"),
        )
        self.repo.assign_product_to_lot(
            product_id=p1.id,
            lot_id=lot.id,
            quantity_acquired=1,
            unit_cost=Decimal("30.00"),
            acquired_at=now - timedelta(days=2),
        )
        self.repo.assign_product_to_lot(
            product_id=p2.id,
            lot_id=lot.id,
            quantity_acquired=1,
            unit_cost=Decimal("30.00"),
            acquired_at=now - timedelta(days=2),
        )

        result = self.repo.repair_purchase_lot_assignment_allocations(lot.id, actor="qa-user")
        self.assertAlmostEqual(float(result["target_landed_total"]), 40.0, places=2)
        self.assertAlmostEqual(float(result["current_assignment_landed_total"]), 60.0, places=2)
        self.assertAlmostEqual(float(result["delta"]), -20.0, places=2)

        assignments = [
            assignment
            for assignment in self.repo.list_product_lot_assignments()
            if int(assignment.lot_id) == int(lot.id)
        ]
        self.assertEqual(
            sum(Decimal(assignment.allocated_cost or 0) for assignment in assignments),
            Decimal("40.00"),
        )
        rows = self.repo.report_accounting_exception_rows(start_dt=now - timedelta(days=7), end_dt=now)
        self.assertFalse(
            [
                row
                for row in rows
                if int(row.get("entity_id") or 0) == int(lot.id)
                and str(row.get("exception_type") or "") == "lot_overallocated"
            ]
        )

    def test_accounting_exception_flags_lot_total_cost_with_embedded_landed_components(self) -> None:
        now = datetime(2026, 4, 26, 12, 0, 0)
        product = self._create_product(sku="GS-LOT-EMBEDDED-COMPONENTS", qty=1)
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-EMBEDDED-COMPONENTS",
            vendor="Vendor",
            purchase_date=now - timedelta(days=2),
            total_cost=Decimal("290.52"),
            total_tax_paid=Decimal("20.25"),
            total_handling_paid=Decimal("0.28"),
        )
        self.repo.assign_product_to_lot(
            product_id=product.id,
            lot_id=lot.id,
            quantity_acquired=1,
            unit_cost=Decimal("269.99"),
            acquired_at=now - timedelta(days=2),
        )

        rows = self.repo.report_accounting_exception_rows(start_dt=now - timedelta(days=7), end_dt=now)
        embedded_row = next(
            row
            for row in rows
            if int(row.get("entity_id") or 0) == int(lot.id)
            and str(row.get("exception_type") or "") == "lot_total_cost_includes_landed_components"
        )

        self.assertEqual(str(embedded_row.get("severity") or ""), "P1")
        self.assertAlmostEqual(float(embedded_row.get("amount") or 0.0), 20.53, places=2)
        self.assertIn(
            "total_cost 290.52 appears to include separate landed components 20.53",
            str(embedded_row.get("details") or ""),
        )
        self.assertIn("matches explicit assignment landed total 269.99", str(embedded_row.get("details") or ""))
        underallocated_row = next(
            row
            for row in rows
            if int(row.get("entity_id") or 0) == int(lot.id)
            and str(row.get("exception_type") or "") == "lot_underallocated"
        )
        self.assertIn(
            "Repair `lot_total_cost_includes_landed_components` first",
            str(underallocated_row.get("details") or ""),
        )

    def test_repair_purchase_lot_embedded_landed_components_corrects_total_cost(self) -> None:
        now = datetime(2026, 4, 26, 12, 0, 0)
        product = self._create_product(sku="GS-REPAIR-LOT-EMBEDDED", qty=1)
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-REPAIR-EMBEDDED",
            vendor="Vendor",
            purchase_date=now - timedelta(days=2),
            total_cost=Decimal("290.52"),
            total_tax_paid=Decimal("20.25"),
            total_handling_paid=Decimal("0.28"),
        )
        self.repo.assign_product_to_lot(
            product_id=product.id,
            lot_id=lot.id,
            quantity_acquired=1,
            unit_cost=Decimal("269.99"),
            acquired_at=now - timedelta(days=2),
        )

        preview = self.repo.repair_purchase_lot_embedded_landed_components(lot.id, actor="qa-user", dry_run=True)
        self.assertTrue(preview["dry_run"])
        self.assertAlmostEqual(float(preview["current_total_cost"]), 290.52, places=2)
        self.assertAlmostEqual(float(preview["separate_landed_components"]), 20.53, places=2)
        self.assertAlmostEqual(float(preview["adjusted_total_cost"]), 269.99, places=2)
        self.assertAlmostEqual(float(preview["current_landed_total"]), 311.05, places=2)
        self.assertAlmostEqual(float(preview["adjusted_landed_total"]), 290.52, places=2)

        result = self.repo.repair_purchase_lot_embedded_landed_components(lot.id, actor="qa-user")
        self.assertFalse(result["dry_run"])
        refreshed_lot = self.db.get(type(lot), lot.id)
        self.assertEqual(Decimal(refreshed_lot.total_cost or 0), Decimal("269.99"))
        rows = self.repo.report_accounting_exception_rows(start_dt=now - timedelta(days=7), end_dt=now)
        self.assertFalse(
            [
                row
                for row in rows
                if int(row.get("entity_id") or 0) == int(lot.id)
                and str(row.get("exception_type") or "") == "lot_total_cost_includes_landed_components"
            ]
        )
        underallocated = next(
            row
            for row in rows
            if int(row.get("entity_id") or 0) == int(lot.id)
            and str(row.get("exception_type") or "") == "lot_underallocated"
        )
        self.assertAlmostEqual(float(underallocated.get("amount") or 0.0), 20.53, places=2)

        self.repo.repair_purchase_lot_assignment_allocations(lot.id, actor="qa-user")
        final_rows = self.repo.report_accounting_exception_rows(start_dt=now - timedelta(days=7), end_dt=now)
        self.assertFalse(
            [
                row
                for row in final_rows
                if int(row.get("entity_id") or 0) == int(lot.id)
                and str(row.get("exception_type") or "") in {
                    "lot_total_cost_includes_landed_components",
                    "lot_underallocated",
                }
            ]
        )

    def test_repair_lot_allocation_blocks_when_lot_total_embeds_landed_components(self) -> None:
        now = datetime(2026, 4, 26, 12, 0, 0)
        product = self._create_product(sku="GS-REPAIR-LOT-EMBEDDED-BLOCK", qty=1)
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-REPAIR-EMBEDDED-BLOCK",
            vendor="Vendor",
            purchase_date=now - timedelta(days=2),
            total_cost=Decimal("290.52"),
            total_tax_paid=Decimal("20.25"),
            total_handling_paid=Decimal("0.28"),
        )
        self.repo.assign_product_to_lot(
            product_id=product.id,
            lot_id=lot.id,
            quantity_acquired=1,
            unit_cost=Decimal("269.99"),
            acquired_at=now - timedelta(days=2),
        )

        with self.assertRaisesRegex(ValueError, "repair before lot allocation repair"):
            self.repo.repair_purchase_lot_assignment_allocations(lot.id, actor="qa-user")

    def test_accounting_exception_flags_unmatched_shipping_label_finance_entry(self) -> None:
        now = datetime(2026, 4, 26, 12, 0, 0)
        product = self._create_product(sku="GS-UNMATCHED-LABEL", qty=5)
        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="EBAY-UNMATCHED-LABEL",
            order_status="shipped",
            sold_at=now - timedelta(days=1),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("5.00"),
            items=[{"product_id": product.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("25.00")}],
            actor="qa-user",
        )
        self.repo.replace_order_finance_entries(
            order.id,
            [
                {
                    "marketplace": "ebay",
                    "external_order_id": order.external_order_id,
                    "transaction_id": "TX-UNMATCHED-LABEL",
                    "entry_kind": "shipping_label",
                    "fee_type": "SHIPPING_LABEL",
                    "amount": Decimal("8.97"),
                    "currency": "USD",
                    "transaction_type": "SHIPPING_LABEL",
                    "transaction_status": "FUNDS_AVAILABLE_FOR_PAYOUT",
                    "transaction_date": now - timedelta(days=1),
                    "source": "finance_transactions",
                    "raw": {},
                }
            ],
            actor="qa-user",
        )

        rows = self.repo.report_accounting_exception_rows(start_dt=now - timedelta(days=7), end_dt=now)
        matches = [
            row
            for row in rows
            if row.get("exception_type") == "unmatched_shipping_label_finance_entry"
            and row.get("reference") == order.external_order_id
        ]

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["severity"], "P2")
        self.assertAlmostEqual(float(matches[0].get("amount") or 0.0), 8.97, places=2)

    def test_repair_unmatched_shipping_label_finance_entry_backfills_single_sale_label_spend(self) -> None:
        now = datetime(2026, 4, 26, 12, 0, 0)
        product = self._create_product(sku="GS-REPAIR-UNMATCHED-LABEL", qty=5)
        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="EBAY-REPAIR-UNMATCHED-LABEL",
            order_status="shipped",
            sold_at=now - timedelta(days=1),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("5.00"),
            items=[{"product_id": product.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("25.00")}],
            actor="qa-user",
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            external_order_id=order.external_order_id,
            order_id=order.id,
            product_id=product.id,
            sold_price=Decimal("25.00"),
            fees=Decimal("3.00"),
            shipping_cost=Decimal("5.00"),
            shipping_label_cost=Decimal("0.00"),
            quantity_sold=1,
            sold_at=now - timedelta(days=1),
        )
        self.repo.replace_order_finance_entries(
            order.id,
            [
                {
                    "marketplace": "ebay",
                    "external_order_id": order.external_order_id,
                    "transaction_id": "TX-REPAIR-UNMATCHED-LABEL",
                    "entry_kind": "shipping_label",
                    "fee_type": "SHIPPING_LABEL",
                    "amount": Decimal("8.97"),
                    "currency": "USD",
                    "transaction_type": "SHIPPING_LABEL",
                    "transaction_status": "FUNDS_AVAILABLE_FOR_PAYOUT",
                    "transaction_date": now - timedelta(days=1),
                    "source": "finance_transactions",
                    "raw": {},
                }
            ],
            actor="qa-user",
        )
        finance_entry = self.db.scalars(
            select(OrderFinanceEntry).where(OrderFinanceEntry.order_id == order.id)
        ).one()

        preview = self.repo.repair_unmatched_shipping_label_finance_entry(
            finance_entry.id,
            actor="qa-user",
            dry_run=True,
        )
        self.assertTrue(preview["dry_run"])
        self.assertEqual(preview["sale_id"], sale.id)
        self.assertAlmostEqual(float(preview["after_shipping_label_cost"]), 8.97, places=2)
        self.assertAlmostEqual(float(self.db.get(Sale, sale.id).shipping_label_cost or 0), 0.0, places=2)

        result = self.repo.repair_unmatched_shipping_label_finance_entry(
            finance_entry.id,
            actor="qa-user",
            dry_run=False,
        )

        self.assertFalse(result["dry_run"])
        repaired_sale = self.db.get(Sale, sale.id)
        self.assertAlmostEqual(float(repaired_sale.shipping_label_cost or 0), 8.97, places=2)
        self.assertEqual(repaired_sale.shipping_label_currency, "USD")
        audit = self.db.scalars(
            select(AuditLog)
            .where(AuditLog.entity_type == "sale", AuditLog.entity_id == sale.id)
            .order_by(AuditLog.id.desc())
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.action, "repair_label_spend")

    def test_repair_unmatched_shipping_label_finance_entry_rejects_multi_sale_order(self) -> None:
        now = datetime(2026, 4, 26, 12, 0, 0)
        product = self._create_product(sku="GS-REPAIR-LABEL-MULTI", qty=5)
        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="EBAY-REPAIR-LABEL-MULTI",
            order_status="shipped",
            sold_at=now - timedelta(days=1),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("5.00"),
            items=[{"product_id": product.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("25.00")}],
            actor="qa-user",
        )
        for suffix in ("A", "B"):
            self.repo.create_sale(
                marketplace="ebay",
                external_order_id=f"{order.external_order_id}-{suffix}",
                order_id=order.id,
                product_id=product.id,
                sold_price=Decimal("25.00"),
                fees=Decimal("3.00"),
                shipping_cost=Decimal("5.00"),
                shipping_label_cost=Decimal("0.00"),
                quantity_sold=1,
                sold_at=now - timedelta(days=1),
            )
        self.repo.replace_order_finance_entries(
            order.id,
            [
                {
                    "marketplace": "ebay",
                    "external_order_id": order.external_order_id,
                    "transaction_id": "TX-REPAIR-LABEL-MULTI",
                    "entry_kind": "shipping_label",
                    "fee_type": "SHIPPING_LABEL",
                    "amount": Decimal("8.97"),
                    "currency": "USD",
                    "transaction_date": now - timedelta(days=1),
                    "source": "finance_transactions",
                    "raw": {},
                }
            ],
            actor="qa-user",
        )
        finance_entry = self.db.scalars(
            select(OrderFinanceEntry).where(OrderFinanceEntry.order_id == order.id)
        ).one()

        with self.assertRaisesRegex(ValueError, "exactly one sale"):
            self.repo.repair_unmatched_shipping_label_finance_entry(finance_entry.id, actor="qa-user")

    def test_accounting_exception_flags_active_bundle_listing_stock_shortage(self) -> None:
        now = datetime(2026, 4, 26, 12, 0, 0)
        primary = self._create_product(sku="GS-BUNDLE-SHORT-PRIMARY", qty=20)
        extra = self._create_product(sku="GS-BUNDLE-SHORT-EXTRA", qty=5)
        listing = self.repo.create_listing(
            product_id=primary.id,
            marketplace="ebay",
            listing_title="Short mixed bundle",
            listing_price=Decimal("75.00"),
            quantity_listed=3,
            external_listing_id="EBAY-BUNDLE-SHORT",
            marketplace_details=json.dumps(
                {
                    "bundle": {
                        "enabled": True,
                        "kind": "mixed_product_bundle",
                        "primary_product_id": primary.id,
                        "components": [
                            {
                                "product_id": primary.id,
                                "sku": primary.sku,
                                "quantity_per_listing": 2,
                            },
                            {
                                "product_id": extra.id,
                                "sku": extra.sku,
                                "quantity_per_listing": 3,
                            },
                        ],
                    }
                }
            ),
            listed_at=now - timedelta(days=1),
        )
        self.repo.update_listing(
            listing.id,
            {
                "review_status": "approved",
                "reviewed_by": "reviewer",
                "reviewed_at": now - timedelta(hours=2),
                "listing_status": "active",
            },
            actor="publisher",
        )

        rows = self.repo.report_accounting_exception_rows(start_dt=now - timedelta(days=7), end_dt=now)
        matches = [
            row
            for row in rows
            if row.get("exception_type") == "active_bundle_listing_stock_shortage"
            and int(row.get("entity_id") or 0) == int(listing.id)
        ]

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["severity"], "P1")
        self.assertEqual(matches[0]["entity_type"], "listing")
        self.assertEqual(matches[0]["reference"], "EBAY-BUNDLE-SHORT")
        self.assertAlmostEqual(float(matches[0].get("amount") or 0.0), 2.0, places=2)
        self.assertIn("Available complete bundle listings from current stock: 1", matches[0]["details"])
        self.assertIn(f"{extra.sku}: needs 9, stock 5", matches[0]["details"])

    def test_repair_active_bundle_listing_stock_shortage_reduces_local_quantity(self) -> None:
        now = datetime(2026, 4, 26, 12, 0, 0)
        primary = self._create_product(sku="GS-BUNDLE-REPAIR-PRIMARY", qty=20)
        extra = self._create_product(sku="GS-BUNDLE-REPAIR-EXTRA", qty=5)
        listing = self.repo.create_listing(
            product_id=primary.id,
            marketplace="ebay",
            listing_title="Repair mixed bundle",
            listing_price=Decimal("75.00"),
            quantity_listed=3,
            external_listing_id="EBAY-BUNDLE-REPAIR",
            marketplace_details=json.dumps(
                {
                    "bundle": {
                        "enabled": True,
                        "kind": "mixed_product_bundle",
                        "primary_product_id": primary.id,
                        "components": [
                            {
                                "product_id": primary.id,
                                "sku": primary.sku,
                                "quantity_per_listing": 2,
                            },
                            {
                                "product_id": extra.id,
                                "sku": extra.sku,
                                "quantity_per_listing": 3,
                            },
                        ],
                    }
                }
            ),
            listed_at=now - timedelta(days=1),
        )
        self.repo.update_listing(
            listing.id,
            {
                "review_status": "approved",
                "reviewed_by": "reviewer",
                "reviewed_at": now - timedelta(hours=2),
                "listing_status": "active",
            },
            actor="publisher",
        )

        preview = self.repo.repair_active_bundle_listing_stock_shortage(listing.id, actor="qa-user", dry_run=True)
        self.assertTrue(preview["dry_run"])
        self.assertEqual(preview["remaining_before"], 3)
        self.assertEqual(preview["available_complete_listings"], 1)
        self.assertEqual(preview["quantity_listed_after"], 1)

        result = self.repo.repair_active_bundle_listing_stock_shortage(listing.id, actor="qa-user")
        self.assertFalse(result["dry_run"])
        refreshed = self.db.get(type(listing), listing.id)
        self.assertEqual(refreshed.quantity_listed, 1)
        self.assertEqual(refreshed.listing_status, "active")

        rows = self.repo.report_accounting_exception_rows(start_dt=now - timedelta(days=7), end_dt=now)
        self.assertFalse(
            [
                row
                for row in rows
                if row.get("exception_type") == "active_bundle_listing_stock_shortage"
                and int(row.get("entity_id") or 0) == int(listing.id)
            ]
        )

    def test_accounting_exception_does_not_flag_active_title_only_lot_listing_stock_shortage(self) -> None:
        now = datetime(2026, 4, 26, 12, 0, 0)
        product = self._create_product(sku="GS-INFERRED-LOT-SHORT", qty=10)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Lot of 20 Mercury Silver Dimes 90% Silver Coins",
            listing_price=Decimal("69.95"),
            quantity_listed=1,
            external_listing_id="EBAY-INFERRED-LOT-SHORT",
            marketplace_details="",
            listed_at=now - timedelta(days=1),
        )
        self.repo.update_listing(
            listing.id,
            {
                "review_status": "approved",
                "reviewed_by": "reviewer",
                "reviewed_at": now - timedelta(hours=2),
                "listing_status": "active",
            },
            actor="publisher",
        )

        rows = self.repo.report_accounting_exception_rows(start_dt=now - timedelta(days=7), end_dt=now)
        self.assertFalse(
            [
                row
                for row in rows
                if row.get("exception_type") == "active_bundle_listing_stock_shortage"
                and int(row.get("entity_id") or 0) == int(listing.id)
            ]
        )

    def test_accounting_exception_does_not_flag_roll_weight_title_as_bundle_stock_shortage(self) -> None:
        now = datetime(2026, 4, 26, 12, 0, 0)
        product = self._create_product(sku="GS-ROLL-WEIGHT-STOCK", qty=1)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="George Washington Ballistic Roll 425 Net Grams 50 $1 Coins Uncirculated Roll",
            listing_price=Decimal("79.95"),
            quantity_listed=1,
            external_listing_id="EBAY-ROLL-WEIGHT",
            marketplace_details="",
            listed_at=now - timedelta(days=1),
        )
        self.repo.update_listing(
            listing.id,
            {
                "review_status": "approved",
                "reviewed_by": "reviewer",
                "reviewed_at": now - timedelta(hours=2),
                "listing_status": "active",
            },
            actor="publisher",
        )

        rows = self.repo.report_accounting_exception_rows(start_dt=now - timedelta(days=7), end_dt=now)
        self.assertFalse(
            [
                row
                for row in rows
                if row.get("exception_type") == "active_bundle_listing_stock_shortage"
                and int(row.get("entity_id") or 0) == int(listing.id)
            ]
        )

    def test_accounting_exception_uses_remaining_bundle_listing_quantity_after_sales(self) -> None:
        now = datetime(2026, 4, 26, 12, 0, 0)
        primary = self._create_product(sku="GS-BUNDLE-REMAIN-PRIMARY", qty=20)
        extra = self._create_product(sku="GS-BUNDLE-REMAIN-EXTRA", qty=9)
        listing = self.repo.create_listing(
            product_id=primary.id,
            marketplace="ebay",
            listing_title="Remaining mixed bundle",
            listing_price=Decimal("75.00"),
            quantity_listed=3,
            external_listing_id="EBAY-BUNDLE-REMAIN",
            marketplace_details=json.dumps(
                {
                    "bundle": {
                        "enabled": True,
                        "kind": "mixed_product_bundle",
                        "primary_product_id": primary.id,
                        "components": [
                            {
                                "product_id": primary.id,
                                "sku": primary.sku,
                                "quantity_per_listing": 2,
                            },
                            {
                                "product_id": extra.id,
                                "sku": extra.sku,
                                "quantity_per_listing": 3,
                            },
                        ],
                    }
                }
            ),
            listed_at=now - timedelta(days=2),
        )
        self.repo.update_listing(
            listing.id,
            {
                "review_status": "approved",
                "reviewed_by": "reviewer",
                "reviewed_at": now - timedelta(hours=2),
                "listing_status": "active",
            },
            actor="publisher",
        )
        self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("150.00"),
            fees=Decimal("15.00"),
            shipping_cost=Decimal("0.00"),
            shipping_label_cost=Decimal("0.00"),
            quantity_sold=2,
            product_id=primary.id,
            listing_id=listing.id,
            external_order_id="EBAY-BUNDLE-REMAIN-SALE",
            sold_at=now - timedelta(days=1),
        )

        rows = self.repo.report_accounting_exception_rows(start_dt=now - timedelta(days=7), end_dt=now)
        matches = [
            row
            for row in rows
            if row.get("exception_type")
            in {"active_bundle_listing_stock_shortage", "active_bundle_component_overcommitted"}
            and str(row.get("reference") or "").find("EBAY-BUNDLE-REMAIN") >= 0
        ]

        self.assertEqual(matches, [])
        self.assertEqual(self.db.get(Product, extra.id).current_quantity, 3)

    def test_accounting_exception_flags_aggregate_bundle_component_overcommit(self) -> None:
        now = datetime(2026, 4, 26, 12, 0, 0)
        product = self._create_product(sku="GS-BUNDLE-AGG-SHORT", qty=5)

        for index in range(2):
            listing = self.repo.create_listing(
                product_id=product.id,
                marketplace="ebay",
                listing_title=f"Aggregate bundle {index + 1}",
                listing_price=Decimal("60.00"),
                quantity_listed=1,
                external_listing_id=f"EBAY-BUNDLE-AGG-{index + 1}",
                marketplace_details=json.dumps(
                    {
                        "bundle": {
                            "enabled": True,
                            "kind": "single_product_lot",
                            "primary_product_id": product.id,
                            "components": [
                                {
                                    "product_id": product.id,
                                    "sku": product.sku,
                                    "quantity_per_listing": 3,
                                },
                            ],
                        }
                    }
                ),
                listed_at=now - timedelta(days=1, minutes=index),
            )
            self.repo.update_listing(
                listing.id,
                {
                    "review_status": "approved",
                    "reviewed_by": "reviewer",
                    "reviewed_at": now - timedelta(hours=2),
                    "listing_status": "active",
                },
                actor="publisher",
            )

        rows = self.repo.report_accounting_exception_rows(start_dt=now - timedelta(days=7), end_dt=now)
        matches = [
            row
            for row in rows
            if row.get("exception_type") == "active_bundle_component_overcommitted"
            and int(row.get("entity_id") or 0) == int(product.id)
        ]
        per_listing_matches = [
            row for row in rows if row.get("exception_type") == "active_bundle_listing_stock_shortage"
        ]

        self.assertEqual(per_listing_matches, [])
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["severity"], "P1")
        self.assertEqual(matches[0]["entity_type"], "product")
        self.assertEqual(matches[0]["sku"], product.sku)
        self.assertAlmostEqual(float(matches[0].get("amount") or 0.0), 1.0, places=2)
        self.assertIn("collectively require 6 unit(s), but current stock is 5", matches[0]["details"])
        self.assertIn("EBAY-BUNDLE-AGG-1", matches[0]["reference"])
        self.assertIn("EBAY-BUNDLE-AGG-2", matches[0]["reference"])

    def test_repair_active_bundle_component_overcommit_reduces_newest_listing(self) -> None:
        now = datetime(2026, 4, 26, 12, 0, 0)
        product = self._create_product(sku="GS-BUNDLE-AGG-REPAIR", qty=5)
        listings = []

        for index in range(2):
            listing = self.repo.create_listing(
                product_id=product.id,
                marketplace="ebay",
                listing_title=f"Aggregate repair bundle {index + 1}",
                listing_price=Decimal("60.00"),
                quantity_listed=1,
                external_listing_id=f"EBAY-BUNDLE-AGG-REPAIR-{index + 1}",
                marketplace_details=json.dumps(
                    {
                        "bundle": {
                            "enabled": True,
                            "kind": "single_product_lot",
                            "primary_product_id": product.id,
                            "components": [
                                {
                                    "product_id": product.id,
                                    "sku": product.sku,
                                    "quantity_per_listing": 3,
                                },
                            ],
                        }
                    }
                ),
                listed_at=now - timedelta(days=1, minutes=index),
            )
            self.repo.update_listing(
                listing.id,
                {
                    "review_status": "approved",
                    "reviewed_by": "reviewer",
                    "reviewed_at": now - timedelta(hours=2),
                    "listing_status": "active",
                },
                actor="publisher",
            )
            listings.append(listing)

        preview = self.repo.repair_active_bundle_component_overcommit(product.id, actor="qa-user", dry_run=True)
        self.assertTrue(preview["dry_run"])
        self.assertEqual(preview["overcommitted_units"], 1)
        self.assertEqual(len(preview["repairs"]), 1)

        result = self.repo.repair_active_bundle_component_overcommit(product.id, actor="qa-user")
        self.assertFalse(result["dry_run"])
        repaired_listing = self.db.get(type(listings[-1]), int(result["repairs"][0]["listing_id"]))
        self.assertEqual(repaired_listing.listing_status, "ended")

        rows = self.repo.report_accounting_exception_rows(start_dt=now - timedelta(days=7), end_dt=now)
        self.assertFalse(
            [
                row
                for row in rows
                if row.get("exception_type") == "active_bundle_component_overcommitted"
                and int(row.get("entity_id") or 0) == int(product.id)
            ]
        )

    def test_accounting_exception_uses_normalized_fee_evidence_for_linked_sales(self) -> None:
        now = datetime(2026, 4, 26, 12, 0, 0)
        product = self._create_product(sku="GS-FEE-EVIDENCE", qty=5)
        self.repo.update_product(product.id, {"acquisition_cost": Decimal("5.00")}, actor="qa-user")
        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="EBAY-FEE-EVIDENCE",
            order_status="shipped",
            sold_at=now - timedelta(days=1),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("0.00"),
            items=[{"product_id": product.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("50.00")}],
            actor="qa-user",
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            external_order_id=order.external_order_id,
            sold_price=Decimal("50.00"),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("0.00"),
            shipping_label_cost=Decimal("0.00"),
            quantity_sold=1,
            product_id=product.id,
            order_id=order.id,
            sold_at=now - timedelta(days=1),
        )
        self.repo.replace_order_finance_entries(
            order.id,
            [
                {
                    "marketplace": "ebay",
                    "external_order_id": order.external_order_id,
                    "transaction_id": "TX-FEE-EVIDENCE",
                    "entry_kind": "marketplace_fee",
                    "fee_type": "FINAL_VALUE_FEE",
                    "amount": Decimal("7.50"),
                    "currency": "USD",
                    "transaction_type": "SALE",
                    "transaction_status": "FUNDS_AVAILABLE_FOR_PAYOUT",
                    "transaction_date": now - timedelta(days=1),
                    "source": "finance_transactions_orderLineItems",
                    "raw": {},
                }
            ],
            actor="qa-user",
        )

        rows = self.repo.report_accounting_exception_rows(start_dt=now - timedelta(days=7), end_dt=now)
        sale_exceptions = [row for row in rows if int(row.get("entity_id") or 0) == int(sale.id)]

        self.assertFalse(
            [row for row in sale_exceptions if row.get("exception_type") == "missing_fee_evidence"],
            sale_exceptions,
        )
        self.assertFalse(
            [row for row in sale_exceptions if row.get("exception_type") == "nonpositive_margin"],
            sale_exceptions,
        )

    def test_accounting_exception_uses_listing_product_for_bundle_sale_without_product_link(self) -> None:
        now = datetime(2026, 4, 26, 12, 0, 0)
        product = self._create_product(sku="GS-ACCT-BUNDLE-LISTING", qty=5)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Accounting bundle listing",
            listing_price=Decimal("50.00"),
            quantity_listed=1,
            marketplace_details=json.dumps(
                {
                    "bundle": {
                        "enabled": True,
                        "kind": "single_product_lot",
                        "components": [
                            {"product_id": product.id, "sku": product.sku, "quantity_per_listing": 2},
                        ],
                    }
                }
            ),
            actor="qa-user",
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            external_order_id="EBAY-ACCT-BUNDLE-LISTING",
            sold_price=Decimal("50.00"),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("5.00"),
            shipping_label_cost=Decimal("0.00"),
            quantity_sold=1,
            product_id=None,
            listing_id=listing.id,
            sold_at=now - timedelta(days=1),
        )

        rows = self.repo.report_accounting_exception_rows(start_dt=now - timedelta(days=7), end_dt=now)
        sale_exceptions = [row for row in rows if int(row.get("entity_id") or 0) == int(sale.id)]
        by_type = {str(row.get("exception_type") or ""): row for row in sale_exceptions}

        self.assertEqual(by_type["missing_shipping_label_spend"]["sku"], product.sku)
        self.assertEqual(by_type["missing_fee_evidence"]["sku"], product.sku)
        self.assertNotIn("missing_product_link", by_type)

    def test_report_listing_format_outcome_rows(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-FORMAT-001", qty=4)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Format Listing",
            listing_price=Decimal("35.00"),
            quantity_listed=1,
            listed_at=now - timedelta(days=1),
            listing_status="active",
            external_listing_id="1234567890",
            marketplace_details=json.dumps(
                {
                    "ebay_publish": {
                        "format_type": "AUCTION",
                        "listing_duration": "DURATION_7_DAYS",
                        "best_offer_enabled": True,
                        "auction_start_price": 29.99,
                        "history": [{"status": "published"}],
                        "published_at": (now - timedelta(days=1)).isoformat(),
                    }
                }
            ),
            actor="qa-user",
        )

        rows = self.repo.report_listing_format_outcome_rows(
            start_dt=now - timedelta(days=30),
            end_dt=now,
        )
        self.assertGreaterEqual(len(rows), 1)
        row = next(r for r in rows if int(r.get("listing_id") or 0) == int(listing.id))
        self.assertEqual(str(row.get("intent_format") or ""), "AUCTION")
        self.assertEqual(int(row.get("publish_success_count") or 0), 1)

    def test_report_rebuy_cost_trend_rows(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-REBUY-001", qty=0)
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-REBUY-1",
            vendor="Vendor",
            purchase_date=now - timedelta(days=2),
            total_cost=Decimal("40.00"),
        )
        self.repo.assign_product_to_lot(
            product_id=product.id,
            lot_id=lot.id,
            quantity_acquired=2,
            unit_cost=Decimal("20.00"),
            acquired_at=now - timedelta(days=2),
        )
        # Duplicate acquisition signature via movement should be de-duplicated.
        self.repo.record_product_repurchase(
            product_id=product.id,
            quantity_acquired=2,
            unit_cost=Decimal("20.00"),
            acquired_at=now - timedelta(days=2),
            actor="qa-user",
        )
        # Distinct event should remain.
        self.repo.record_product_repurchase(
            product_id=product.id,
            quantity_acquired=1,
            unit_cost=Decimal("30.00"),
            acquired_at=now - timedelta(days=1),
            actor="qa-user",
        )

        rows = self.repo.report_rebuy_cost_trend_rows(end_dt=now)
        rebuy_rows = [r for r in rows if int(r.get("product_id") or 0) == int(product.id)]
        self.assertEqual(len(rebuy_rows), 2)
        first, second = rebuy_rows[0], rebuy_rows[1]
        self.assertEqual(int(first.get("qty_in") or 0), 2)
        self.assertAlmostEqual(float(first.get("weighted_unit_cost") or 0.0), 20.0, places=2)
        self.assertEqual(int(second.get("qty_in") or 0), 1)
        self.assertAlmostEqual(float(second.get("weighted_unit_cost") or 0.0), (70.0 / 3.0), places=2)

    def test_report_inventory_cycle_rows_closed_cycle(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-CYCLE-001", qty=2)
        self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("50.00"),
            fees=Decimal("5.00"),
            shipping_cost=Decimal("3.00"),
            shipping_label_cost=Decimal("1.25"),
            quantity_sold=2,
            product_id=product.id,
            sold_at=now - timedelta(hours=1),
        )

        rows = self.repo.report_inventory_cycle_rows(end_dt=now)
        row = next(r for r in rows if int(r.get("product_id") or 0) == int(product.id))
        self.assertEqual(str(row.get("cycle_status") or ""), "closed")
        self.assertEqual(int(row.get("cycle_number") or 0), 1)
        self.assertEqual(int(row.get("qty_in") or 0), 2)
        self.assertEqual(int(row.get("qty_out_movements") or 0), 2)
        self.assertEqual(int(row.get("qty_sold_sales") or 0), 2)
        self.assertEqual(int(row.get("sale_count") or 0), 1)
        self.assertAlmostEqual(float(row.get("gross_sales") or 0.0), 50.0, places=2)
        self.assertAlmostEqual(float(row.get("fees") or 0.0), 5.0, places=2)
        self.assertAlmostEqual(float(row.get("shipping_cost") or 0.0), 3.0, places=2)
        self.assertAlmostEqual(float(row.get("shipping_label_cost") or 0.0), 1.25, places=2)
        self.assertAlmostEqual(float(row.get("net_sales") or 0.0), 46.75, places=2)
        self.assertTrue(str(row.get("cycle_start") or "").strip())
        self.assertTrue(str(row.get("cycle_end") or "").strip())

    def test_report_inventory_cycle_rows_use_linked_normalized_actuals(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-CYCLE-NORM-ACTUALS", qty=2)
        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="EBAY-CYCLE-NORM-ACTUALS",
            order_status="paid",
            sold_at=now - timedelta(hours=1),
            fees=Decimal("0.00"),
            shipping_cost=Decimal("3.00"),
            items=[{"product_id": product.id, "listing_id": None, "quantity": 2, "unit_price": Decimal("25.00")}],
            actor="qa-user",
        )
        self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("50.00"),
            fees=Decimal("5.00"),
            shipping_cost=Decimal("3.00"),
            shipping_label_cost=Decimal("1.25"),
            quantity_sold=2,
            product_id=product.id,
            order_id=order.id,
            external_order_id=order.external_order_id,
            sold_at=now - timedelta(hours=1),
        )
        self.repo.replace_order_finance_entries(
            order.id,
            [
                {"entry_kind": "marketplace_fee", "fee_type": "FINAL_VALUE_FEE", "amount": Decimal("4.00")},
                {"entry_kind": "shipping_label", "fee_type": "SHIPPING_LABEL", "amount": Decimal("2.00")},
            ],
            actor="qa-user",
        )

        rows = self.repo.report_inventory_cycle_rows(end_dt=now)
        row = next(r for r in rows if int(r.get("product_id") or 0) == int(product.id))

        self.assertEqual(str(row.get("cycle_status") or ""), "closed")
        self.assertAlmostEqual(float(row.get("fees") or 0.0), 4.0, places=2)
        self.assertAlmostEqual(float(row.get("shipping_cost") or 0.0), 3.0, places=2)
        self.assertAlmostEqual(float(row.get("shipping_label_cost") or 0.0), 2.0, places=2)
        self.assertAlmostEqual(float(row.get("net_sales") or 0.0), 47.0, places=2)

    def test_report_inventory_cycle_rows_allocates_bundle_sale_to_component_products(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        primary = self._create_product(sku="GS-CYCLE-BUNDLE-PRIMARY", qty=4)
        extra = self._create_product(sku="GS-CYCLE-BUNDLE-EXTRA", qty=6)
        listing = self.repo.create_listing(
            product_id=primary.id,
            marketplace="ebay",
            listing_title="Cycle bundle",
            listing_price=Decimal("100.00"),
            quantity_listed=1,
            marketplace_details=json.dumps(
                {
                    "bundle": {
                        "enabled": True,
                        "kind": "mixed_product_bundle",
                        "components": [
                            {"product_id": primary.id, "sku": primary.sku, "quantity_per_listing": 1},
                            {"product_id": extra.id, "sku": extra.sku, "quantity_per_listing": 3},
                        ],
                    }
                }
            ),
            actor="qa-user",
        )
        self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("8.00"),
            shipping_cost=Decimal("4.00"),
            shipping_label_cost=Decimal("2.00"),
            quantity_sold=1,
            product_id=None,
            listing_id=listing.id,
            external_order_id="EBAY-CYCLE-BUNDLE",
            sold_at=now - timedelta(hours=1),
        )

        rows = self.repo.report_inventory_cycle_rows(end_dt=now)
        primary_row = next(r for r in rows if int(r.get("product_id") or 0) == int(primary.id))
        extra_row = next(r for r in rows if int(r.get("product_id") or 0) == int(extra.id))

        self.assertEqual(int(primary_row.get("qty_out_movements") or 0), 1)
        self.assertEqual(int(primary_row.get("qty_sold_sales") or 0), 1)
        self.assertAlmostEqual(float(primary_row.get("gross_sales") or 0.0), 25.0, places=2)
        self.assertAlmostEqual(float(primary_row.get("fees") or 0.0), 2.0, places=2)
        self.assertAlmostEqual(float(primary_row.get("shipping_cost") or 0.0), 1.0, places=2)
        self.assertAlmostEqual(float(primary_row.get("shipping_label_cost") or 0.0), 0.5, places=2)
        self.assertAlmostEqual(float(primary_row.get("net_sales") or 0.0), 23.5, places=2)

        self.assertEqual(int(extra_row.get("qty_out_movements") or 0), 3)
        self.assertEqual(int(extra_row.get("qty_sold_sales") or 0), 3)
        self.assertAlmostEqual(float(extra_row.get("gross_sales") or 0.0), 75.0, places=2)
        self.assertAlmostEqual(float(extra_row.get("fees") or 0.0), 6.0, places=2)
        self.assertAlmostEqual(float(extra_row.get("shipping_cost") or 0.0), 3.0, places=2)
        self.assertAlmostEqual(float(extra_row.get("shipping_label_cost") or 0.0), 1.5, places=2)
        self.assertAlmostEqual(float(extra_row.get("net_sales") or 0.0), 70.5, places=2)

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

    def test_create_return_restock_for_bundle_sale_restores_each_component(self) -> None:
        primary = self._create_product(sku="GS-BUNDLE-RET-PRIMARY", qty=20)
        extra = self._create_product(sku="GS-BUNDLE-RET-EXTRA", qty=20)
        listing = self.repo.create_listing(
            product_id=primary.id,
            marketplace="ebay",
            listing_title="Return bundle",
            listing_price=Decimal("50.00"),
            quantity_listed=3,
            marketplace_details=json.dumps(
                {
                    "bundle": {
                        "enabled": True,
                        "kind": "mixed_product_bundle",
                        "components": [
                            {"product_id": primary.id, "sku": primary.sku, "quantity_per_listing": 2},
                            {"product_id": extra.id, "sku": extra.sku, "quantity_per_listing": 3},
                        ],
                    }
                }
            ),
            actor="qa-user",
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("10.00"),
            shipping_cost=Decimal("5.00"),
            quantity_sold=2,
            product_id=primary.id,
            listing_id=listing.id,
            sold_at=datetime(2026, 3, 23, 12, 0, 0),
        )
        self.assertEqual(self.db.get(Product, primary.id).current_quantity, 16)
        self.assertEqual(self.db.get(Product, extra.id).current_quantity, 14)

        ret = self.repo.create_return(
            marketplace="ebay",
            sale_id=sale.id,
            quantity=1,
            refund_amount=Decimal("50.00"),
            return_status="processed",
            disposition="restock",
            restocked=True,
            returned_at=datetime(2026, 3, 24, 9, 0, 0),
            actor="qa-user",
        )

        self.assertEqual(self.db.get(Product, primary.id).current_quantity, 18)
        self.assertEqual(self.db.get(Product, extra.id).current_quantity, 17)
        movements = [
            m for m in self.repo.list_inventory_movements(limit=200)
            if m.reference_type == "return" and m.reference_id == ret.id
        ]
        self.assertEqual(
            sorted((m.product_id, m.quantity_delta, m.movement_type) for m in movements),
            [
                (primary.id, 2, "return_bundle_component_restock"),
                (extra.id, 3, "return_bundle_component_restock"),
            ],
        )
        return_rows = self.repo.report_returns_rows(
            start_dt=datetime(2026, 3, 23, 0, 0, 0),
            end_dt=datetime(2026, 3, 25, 0, 0, 0),
        )
        return_row = next(row for row in return_rows if int(row.get("return_id") or 0) == int(ret.id))
        self.assertTrue(return_row["listing_is_bundle"])
        self.assertEqual(return_row["listing_bundle_kind"], "mixed_product_bundle")
        self.assertEqual(return_row["listing_bundle_component_count"], 2)
        self.assertEqual(return_row["listing_bundle_units_per_return"], 5)
        self.assertEqual(return_row["listing_bundle_inventory_units_returned"], 5)

    def test_bundle_return_uses_listing_product_when_sale_has_no_product_link(self) -> None:
        primary = self._create_product(sku="GS-BUNDLE-RET-LINK-PRIMARY", qty=20)
        extra = self._create_product(sku="GS-BUNDLE-RET-LINK-EXTRA", qty=20)
        listing = self.repo.create_listing(
            product_id=primary.id,
            marketplace="ebay",
            listing_title="Return bundle product fallback",
            listing_price=Decimal("50.00"),
            quantity_listed=2,
            marketplace_details=json.dumps(
                {
                    "bundle": {
                        "enabled": True,
                        "kind": "mixed_product_bundle",
                        "components": [
                            {"product_id": primary.id, "sku": primary.sku, "quantity_per_listing": 1},
                            {"product_id": extra.id, "sku": extra.sku, "quantity_per_listing": 2},
                        ],
                    }
                }
            ),
            actor="qa-user",
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("50.00"),
            fees=Decimal("5.00"),
            shipping_cost=Decimal("4.00"),
            quantity_sold=1,
            product_id=None,
            listing_id=listing.id,
            sold_at=datetime(2026, 3, 23, 12, 0, 0),
        )

        ret = self.repo.create_return(
            marketplace="ebay",
            sale_id=sale.id,
            quantity=1,
            refund_amount=Decimal("50.00"),
            return_status="processed",
            disposition="restock",
            restocked=True,
            returned_at=datetime(2026, 3, 24, 9, 0, 0),
            actor="qa-user",
        )

        self.assertEqual(ret.product_id, primary.id)
        self.assertEqual(self.db.get(Product, primary.id).current_quantity, 20)
        self.assertEqual(self.db.get(Product, extra.id).current_quantity, 20)
        return_rows = self.repo.report_returns_rows(
            start_dt=datetime(2026, 3, 23, 0, 0, 0),
            end_dt=datetime(2026, 3, 25, 0, 0, 0),
        )
        return_row = next(row for row in return_rows if int(row.get("return_id") or 0) == int(ret.id))
        self.assertEqual(return_row["product_id"], primary.id)
        self.assertEqual(return_row["sku"], primary.sku)
        self.assertEqual(return_row["product_title"], primary.title)
        self.assertTrue(return_row["listing_is_bundle"])
        self.assertEqual(return_row["listing_bundle_inventory_units_returned"], 3)

    def test_update_return_restock_for_bundle_sale_reconciles_components(self) -> None:
        primary = self._create_product(sku="GS-BUNDLE-RET-UPD-PRIMARY", qty=20)
        extra = self._create_product(sku="GS-BUNDLE-RET-UPD-EXTRA", qty=20)
        listing = self.repo.create_listing(
            product_id=primary.id,
            marketplace="ebay",
            listing_title="Return update bundle",
            listing_price=Decimal("50.00"),
            quantity_listed=3,
            marketplace_details=json.dumps(
                {
                    "bundle": {
                        "enabled": True,
                        "components": [
                            {"product_id": primary.id, "sku": primary.sku, "quantity_per_listing": 1},
                            {"product_id": extra.id, "sku": extra.sku, "quantity_per_listing": 2},
                        ],
                    }
                }
            ),
            actor="qa-user",
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("10.00"),
            shipping_cost=Decimal("5.00"),
            quantity_sold=3,
            product_id=primary.id,
            listing_id=listing.id,
            sold_at=datetime(2026, 3, 23, 12, 0, 0),
        )
        ret = self.repo.create_return(
            marketplace="ebay",
            sale_id=sale.id,
            quantity=1,
            refund_amount=Decimal("50.00"),
            return_status="processed",
            disposition="damaged",
            restocked=False,
            returned_at=datetime(2026, 3, 24, 9, 0, 0),
            actor="qa-user",
        )
        self.assertEqual(self.db.get(Product, primary.id).current_quantity, 17)
        self.assertEqual(self.db.get(Product, extra.id).current_quantity, 14)

        self.repo.update_return(ret.id, {"restocked": True, "quantity": 2, "disposition": "restock"}, actor="qa-user")
        self.assertEqual(self.db.get(Product, primary.id).current_quantity, 19)
        self.assertEqual(self.db.get(Product, extra.id).current_quantity, 18)

        self.repo.update_return(ret.id, {"restocked": False, "disposition": "scrap"}, actor="qa-user")
        self.assertEqual(self.db.get(Product, primary.id).current_quantity, 17)
        self.assertEqual(self.db.get(Product, extra.id).current_quantity, 14)

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

    def test_list_listings_supports_recent_limit(self) -> None:
        product = self._create_product(sku="GS-LST-LIMIT", qty=3)
        first = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Old Listing",
            listing_price=Decimal("10.00"),
            quantity_listed=1,
        )
        second = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="New Listing",
            listing_price=Decimal("11.00"),
            quantity_listed=1,
        )

        rows = self.repo.list_listings(limit=1)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].id, second.id)
        self.assertNotEqual(rows[0].id, first.id)

    def test_listing_lookup_helpers_avoid_full_listing_scans(self) -> None:
        product = self._create_product(sku="GS-LST-LOOKUP", qty=3)
        other = self._create_product(sku="GS-LST-LOOKUP-OTHER", qty=1)
        ebay_listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Lookup eBay",
            listing_price=Decimal("10.00"),
            quantity_listed=1,
            external_listing_id="EBAY-LOOKUP-1",
        )
        self.repo.create_listing(
            product_id=product.id,
            marketplace="local",
            listing_title="Lookup Local",
            listing_price=Decimal("11.00"),
            quantity_listed=1,
            external_listing_id="LOCAL-LOOKUP-1",
        )
        self.repo.create_listing(
            product_id=other.id,
            marketplace="ebay",
            listing_title="Other eBay",
            listing_price=Decimal("12.00"),
            quantity_listed=1,
            external_listing_id="EBAY-OTHER-1",
        )

        self.assertEqual(self.repo.get_listing(ebay_listing.id).id, ebay_listing.id)
        ebay_rows = self.repo.list_listings_for_product(product.id, marketplace="ebay")
        self.assertEqual([row.id for row in ebay_rows], [ebay_listing.id])
        self.assertEqual(
            self.repo.find_listing_owner_by_external_id(
                marketplace="ebay",
                external_listing_id="EBAY-LOOKUP-1",
            ),
            ebay_listing.id,
        )
        self.assertIsNone(
            self.repo.find_listing_owner_by_external_id(
                marketplace="ebay",
                external_listing_id="EBAY-LOOKUP-1",
                exclude_listing_id=ebay_listing.id,
            )
        )

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

    def test_duplicate_listing_creates_clean_draft_copy(self) -> None:
        product = self._create_product(sku="GS-LST-DUP", qty=5)
        details = {
            "notes": "Original listing copy.",
            "bundle": {"components": [{"product_id": product.id, "quantity": 2}]},
            "lifecycle": {"archived": True, "reason": "old row"},
            "review_history": [{"decision": "approved"}],
            "last_direct_post_error": "old publish error",
            "direct_post_last_error": "old wizard publish error",
            "direct_post_last_context": {"offer_id": "1234567890"},
            "ebay_publish": {
                "format_type": "AUCTION",
                "category_id": "166679",
                "payment_policy_id": "PAY-1",
                "fulfillment_policy_id": "FUL-1",
                "return_policy_id": "RET-1",
                "inventory_sku": "GS-LST-DUP-LISTING-OLD",
                "offer_id": "1234567890",
                "published_listing_id": "137000000000",
                "listing_url": "https://www.ebay.com/itm/137000000000",
            },
        }
        source = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Original Listing",
            listing_price=Decimal("42.50"),
            quantity_listed=2,
            external_listing_id="137000000000",
            marketplace_url="https://www.ebay.com/itm/137000000000",
            marketplace_details=json.dumps(details),
        )

        duplicate = self.repo.duplicate_listing(source.id, actor="qa-user")

        self.assertNotEqual(source.id, duplicate.id)
        self.assertEqual(duplicate.product_id, source.product_id)
        self.assertEqual(duplicate.marketplace, "ebay")
        self.assertEqual(duplicate.listing_title, "Original Listing (copy)")
        self.assertEqual(duplicate.listing_price, Decimal("42.50"))
        self.assertEqual(duplicate.quantity_listed, 2)
        self.assertEqual(duplicate.external_listing_id, "")
        self.assertEqual(duplicate.marketplace_url, "")
        self.assertEqual(duplicate.listing_status, "draft")
        self.assertEqual(duplicate.review_status, "pending")
        self.assertEqual(duplicate.reviewed_by, "")

        copied_details = json.loads(duplicate.marketplace_details)
        self.assertEqual(copied_details["notes"], "Original listing copy.")
        self.assertEqual(copied_details["bundle"]["components"][0]["quantity"], 2)
        self.assertEqual(copied_details["duplicated_from_listing_id"], source.id)
        self.assertEqual(copied_details["duplicated_by"], "qa-user")
        self.assertNotIn("lifecycle", copied_details)
        self.assertNotIn("review_history", copied_details)
        self.assertNotIn("last_direct_post_error", copied_details)
        self.assertNotIn("direct_post_last_error", copied_details)
        self.assertNotIn("direct_post_last_context", copied_details)
        copied_publish = copied_details["ebay_publish"]
        self.assertEqual(copied_publish["format_type"], "AUCTION")
        self.assertEqual(copied_publish["category_id"], "166679")
        self.assertEqual(copied_publish["payment_policy_id"], "PAY-1")
        self.assertNotIn("inventory_sku", copied_publish)
        self.assertNotIn("offer_id", copied_publish)
        self.assertNotIn("published_listing_id", copied_publish)
        self.assertNotIn("listing_url", copied_publish)

        refreshed_source = self.repo.get_listing(source.id)
        self.assertEqual(refreshed_source.external_listing_id, "137000000000")
        self.assertIn("offer_id", json.loads(refreshed_source.marketplace_details)["ebay_publish"])
        duplicate_audit = self.db.scalars(
            select(AuditLog).where(
                AuditLog.entity_type == "listing",
                AuditLog.entity_id == duplicate.id,
                AuditLog.action == "duplicate",
            )
        ).first()
        self.assertIsNotNone(duplicate_audit)
        self.assertEqual(duplicate_audit.changes["after"]["source_listing_id"], source.id)

    def test_duplicate_listing_supports_overrides_and_empty_details(self) -> None:
        product = self._create_product(sku="GS-LST-DUP-OVERRIDE", qty=5)
        source = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Original Listing",
            listing_price=Decimal("42.50"),
            quantity_listed=2,
            marketplace_details='{"notes": "copy me"}',
        )

        duplicate = self.repo.duplicate_listing(
            source.id,
            listing_title="New Draft Title",
            listing_price=Decimal("44.00"),
            quantity_listed=3,
            copy_marketplace_details=False,
            actor="qa-user",
        )

        self.assertEqual(duplicate.listing_title, "New Draft Title")
        self.assertEqual(duplicate.listing_price, Decimal("44.00"))
        self.assertEqual(duplicate.quantity_listed, 3)
        self.assertEqual(duplicate.marketplace_details, "")

    def test_duplicate_listing_keeps_long_titles_within_storage_limit(self) -> None:
        product = self._create_product(sku="GS-LST-DUP-LONG", qty=5)
        source_title = "A" * 255
        source = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title=source_title,
            listing_price=Decimal("42.50"),
            quantity_listed=2,
        )

        duplicate = self.repo.duplicate_listing(source.id, actor="qa-user")

        self.assertLessEqual(len(duplicate.listing_title), 255)
        self.assertTrue(duplicate.listing_title.endswith("(copy)"))

        override_duplicate = self.repo.duplicate_listing(
            source.id,
            listing_title="B" * 300,
            actor="qa-user",
        )

        self.assertEqual(len(override_duplicate.listing_title), 255)
        self.assertEqual(override_duplicate.listing_title, "B" * 255)

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

    def test_delete_listing_detaches_related_records_and_writes_audit(self) -> None:
        product = self._create_product(sku="GS-LST-DEL-001", qty=4)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Delete Candidate",
            listing_price=Decimal("25.00"),
            quantity_listed=1,
            external_listing_id="DEL-CANDIDATE-001",
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("25.00"),
            fees=Decimal("3.00"),
            shipping_cost=Decimal("1.00"),
            quantity_sold=1,
            product_id=product.id,
            listing_id=listing.id,
            sold_at=datetime(2026, 4, 10, 12, 0, 0),
        )
        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="ORDER-LIST-DEL-001",
            order_status="paid",
            sold_at=datetime(2026, 4, 10, 12, 5, 0),
            fees=Decimal("3.00"),
            shipping_cost=Decimal("1.00"),
            items=[
                {
                    "product_id": product.id,
                    "listing_id": listing.id,
                    "quantity": 1,
                    "unit_price": Decimal("25.00"),
                }
            ],
            actor="qa-user",
        )
        self.assertIsNotNone(order.id)
        media = self.repo.create_media_asset(
            media_type="image",
            original_filename="delete-candidate.jpg",
            content_type="image/jpeg",
            size_bytes=1024,
            s3_bucket="test-bucket",
            s3_key="listings/delete-candidate.jpg",
            s3_url="https://example.invalid/listings/delete-candidate.jpg",
            product_id=product.id,
            listing_id=listing.id,
            uploaded_by="qa-user",
        )
        self.assertIsNotNone(media.id)

        deleted = self.repo.delete_listing(int(listing.id), actor="qa-user")
        self.assertTrue(deleted)
        self.assertIsNone(self.db.get(type(listing), int(listing.id)))

        refreshed_sale = self.db.get(Sale, int(sale.id))
        self.assertIsNotNone(refreshed_sale)
        self.assertIsNone(refreshed_sale.listing_id)

        refreshed_items = [row for row in self.repo.list_order_items() if int(row.order_id or 0) == int(order.id)]
        self.assertEqual(len(refreshed_items), 1)
        self.assertIsNone(refreshed_items[0].listing_id)

        refreshed_media = self.db.get(MediaAsset, int(media.id))
        self.assertIsNotNone(refreshed_media)
        self.assertIsNone(refreshed_media.listing_id)

        audit_rows = (
            self.db.query(AuditLog)
            .filter(AuditLog.entity_type == "listing", AuditLog.entity_id == int(listing.id), AuditLog.action == "delete")
            .all()
        )
        self.assertGreaterEqual(len(audit_rows), 1)
        payload = json.loads(audit_rows[-1].changes_json or "{}")
        self.assertEqual(payload.get("linked_sales_count", {}).get("before"), 1)
        self.assertEqual(payload.get("linked_order_items_count", {}).get("before"), 1)

    def test_delete_listing_missing_returns_false(self) -> None:
        self.assertFalse(self.repo.delete_listing(999999, actor="qa-user"))

    def test_archive_and_restore_listing_updates_lifecycle_metadata(self) -> None:
        product = self._create_product(sku="GS-LST-ARCH-001", qty=2)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Archive Candidate",
            listing_price=Decimal("9.99"),
            quantity_listed=1,
            marketplace_details="legacy free-text details",
        )
        archived = self.repo.archive_listing(
            int(listing.id),
            actor="qa-user",
            reason="duplicate draft",
        )
        archived_payload = json.loads(str(archived.marketplace_details or "{}"))
        self.assertTrue(bool(archived_payload.get("lifecycle", {}).get("archived")))
        self.assertEqual(str(archived_payload.get("lifecycle", {}).get("archive_reason") or ""), "duplicate draft")

        restored = self.repo.restore_listing(int(listing.id), actor="qa-user")
        restored_payload = json.loads(str(restored.marketplace_details or "{}"))
        self.assertFalse(bool(restored_payload.get("lifecycle", {}).get("archived")))

    def test_archive_listing_not_found_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "Listing 999 not found"):
            self.repo.archive_listing(999, actor="qa-user", reason="none")

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

    def test_dashboard_profit_basis_uses_listing_product_when_sale_product_missing(self) -> None:
        product = self._create_product(sku="GS-LISTING-FALLBACK-001", qty=3)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Listing product fallback",
            listing_price=Decimal("25.00"),
            quantity_listed=1,
            external_listing_id="137394544357",
            actor="qa-user",
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("25.00"),
            fees=Decimal("2.00"),
            shipping_cost=Decimal("0.00"),
            quantity_sold=1,
            product_id=None,
            listing_id=listing.id,
            external_order_id="ORDER-LISTING-FALLBACK",
            sold_at=datetime(2026, 6, 9, 7, 2, 21),
            actor="qa-user",
        )

        rows = self.repo.dashboard_profit_basis_rows(now=datetime(2026, 6, 10, 0, 0, 0), limit=10)
        row = next(r for r in rows if int(r["sale_id"]) == int(sale.id))
        self.assertEqual(row["product_id"], product.id)
        self.assertEqual(row["sku"], product.sku)
        self.assertEqual(row["product_title"], product.title)
        self.assertEqual(row["inventory_movement_units_expected"], 1)
        self.assertEqual(row["inventory_movement_units_recorded"], 0)
        self.assertEqual(row["inventory_movement_mismatch_units"], 1)
        self.assertTrue(row["inventory_movement_mismatch"])
        self.assertEqual(row["listing_lot_movement_mismatch_units"], 0)
        self.assertFalse(row["listing_lot_movement_mismatch"])

    def test_report_order_items_rows_uses_listing_product_when_item_product_missing(self) -> None:
        product = self._create_product(sku="GS-ORDER-ITEM-FALLBACK-001", qty=3)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Order item listing product fallback",
            listing_price=Decimal("25.00"),
            quantity_listed=1,
            external_listing_id="137394544358",
            actor="qa-user",
        )
        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="ORDER-ITEM-LISTING-FALLBACK",
            order_status="paid",
            sold_at=datetime(2026, 6, 9, 7, 2, 21),
            items=[{"product_id": None, "listing_id": listing.id, "quantity": 1, "unit_price": Decimal("25.00")}],
            actor="qa-user",
        )

        rows = self.repo.report_order_items_rows(
            start_dt=datetime(2026, 6, 9, 0, 0, 0),
            end_dt=datetime(2026, 6, 10, 0, 0, 0),
        )
        row = next(r for r in rows if int(r["order_id"]) == int(order.id))
        self.assertEqual(row["product_id"], product.id)
        self.assertEqual(row["sku"], product.sku)
        self.assertEqual(row["product_title"], product.title)

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
        self.assertEqual(len(self.repo.list_media_assets(limit=1)), 1)
        self.assertEqual(len(self.repo.list_media_assets_for_product(product.id)), 1)
        self.assertEqual(len(self.repo.list_media_assets_for_listing(listing.id)), 1)
        self.assertEqual(self.repo.count_media_assets_for_listing(listing.id), 1)
        by_ids_rows = self.repo.list_media_assets_by_ids([media.id])
        self.assertEqual(len(by_ids_rows), 1)
        self.assertEqual(int(by_ids_rows[0].id), int(media.id))
        unlinked_ids = self.repo.list_unlinked_product_media_ids(product.id)
        self.assertNotIn(int(media.id), unlinked_ids)
        media2 = self.repo.create_media_asset(
            media_type="image",
            original_filename="coin2.jpg",
            content_type="image/jpeg",
            size_bytes=54321,
            s3_bucket="bucket",
            s3_key="media/coin2.jpg",
            s3_url="https://example/media/coin2.jpg",
            product_id=product.id,
            listing_id=None,
            uploaded_by="qa-user",
        )
        bulk_result = self.repo.bulk_update_media_assets(
            [media2.id, 999999],
            {"listing_id": listing.id},
            actor="qa-user",
        )
        self.assertIn(int(media2.id), list(bulk_result.get("updated_ids") or []))
        self.assertIn(999999, list(bulk_result.get("missing_ids") or []))
        refreshed_media2 = self.db.get(type(media2), media2.id)
        self.assertEqual(int(refreshed_media2.listing_id or 0), int(listing.id))

        updated = self.repo.update_media_asset(media.id, {"original_filename": "coin_new.jpg"}, actor="qa-user")
        self.assertEqual(updated.original_filename, "coin_new.jpg")

        unchanged = self.repo.update_media_asset(media.id, {"original_filename": "coin_new.jpg"}, actor="qa-user")
        self.assertEqual(unchanged.original_filename, "coin_new.jpg")

        with self.assertRaisesRegex(ValueError, "Media asset 999 not found"):
            self.repo.update_media_asset(999, {"original_filename": "x.jpg"}, actor="qa-user")

    def test_listing_media_count_map_filters_archived_by_default(self) -> None:
        product = self._create_product(sku="GS-MEDIA-COUNT-001", qty=2)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Count Listing",
            listing_price=Decimal("19.99"),
            quantity_listed=1,
        )
        active_media = self.repo.create_media_asset(
            media_type="image",
            original_filename="active.jpg",
            content_type="image/jpeg",
            size_bytes=100,
            s3_bucket="bucket",
            s3_key="media/active.jpg",
            s3_url="https://example/media/active.jpg",
            product_id=product.id,
            listing_id=listing.id,
            uploaded_by="qa-user",
        )
        archived_media = self.repo.create_media_asset(
            media_type="image",
            original_filename="archived.jpg",
            content_type="image/jpeg",
            size_bytes=100,
            s3_bucket="bucket",
            s3_key="media/archived.jpg",
            s3_url="https://example/media/archived.jpg",
            product_id=product.id,
            listing_id=listing.id,
            uploaded_by="qa-user",
        )
        self.repo.update_media_asset(
            archived_media.id,
            {"is_archived": True},
            actor="qa-user",
        )
        active_counts = self.repo.listing_media_count_map(listing_ids=[listing.id])
        self.assertEqual(active_counts.get(listing.id), 1)
        all_counts = self.repo.listing_media_count_map(
            listing_ids=[listing.id],
            include_archived=True,
        )
        self.assertEqual(all_counts.get(listing.id), 2)
        self.assertIsNotNone(active_media.id)

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

    def test_archive_and_restore_media_asset(self) -> None:
        product = self._create_product(sku="GS-MEDIA-ARC-001", qty=1)
        media = self.repo.create_media_asset(
            media_type="image",
            original_filename="archive_me.jpg",
            content_type="image/jpeg",
            size_bytes=10,
            s3_bucket="bucket",
            s3_key="media/archive_me.jpg",
            s3_url="https://example/media/archive_me.jpg",
            product_id=product.id,
            listing_id=None,
            uploaded_by="qa-user",
        )

        archived = self.repo.archive_media_asset(media.id, actor="qa-user")
        self.assertTrue(archived.is_archived)
        self.assertFalse(any(r.id == media.id for r in self.repo.list_media_assets()))
        self.assertTrue(any(r.id == media.id for r in self.repo.list_media_assets(include_archived=True)))

        restored = self.repo.restore_media_asset(media.id, actor="qa-user")
        self.assertFalse(restored.is_archived)
        self.assertTrue(any(r.id == media.id for r in self.repo.list_media_assets()))

        logs = self.repo.list_audit_logs(limit=200)
        archive_log = next(
            (
                l
                for l in logs
                if l.entity_type == "media_asset" and l.entity_id == media.id and l.action == "archive"
            ),
            None,
        )
        restore_log = next(
            (
                l
                for l in logs
                if l.entity_type == "media_asset" and l.entity_id == media.id and l.action == "restore"
            ),
            None,
        )
        self.assertIsNotNone(archive_log)
        self.assertIsNotNone(restore_log)

    def test_archive_media_asset_requires_force_when_active_listing_context_exists(self) -> None:
        product = self._create_product(sku="GS-MEDIA-ARC-ACTIVE-1", qty=2)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Active Listing For Media Guardrail",
            listing_price=Decimal("12.00"),
            quantity_listed=1,
        )
        self.repo.review_listing(listing.id, decision="approved", actor="reviewer1", notes="ok")
        self.repo.update_listing(
            listing.id,
            {"listing_status": "active", "review_status": "approved", "reviewed_by": "reviewer1"},
            actor="publisher1",
        )
        media = self.repo.create_media_asset(
            media_type="image",
            original_filename="guardrail.jpg",
            content_type="image/jpeg",
            size_bytes=10,
            s3_bucket="bucket",
            s3_key="media/guardrail.jpg",
            s3_url="https://example/media/guardrail.jpg",
            product_id=product.id,
            listing_id=listing.id,
            uploaded_by="qa-user",
        )
        blockers = self.repo.get_media_asset_archive_blockers(media.id)
        self.assertGreaterEqual(int(blockers.get("linked_listing_active", 0)), 1)
        self.assertGreaterEqual(int(blockers.get("linked_product_active_listings", 0)), 1)
        with self.assertRaisesRegex(ValueError, "Cannot archive media linked to active listing context"):
            self.repo.archive_media_asset(media.id, actor="qa-user", force=False)
        archived = self.repo.archive_media_asset(media.id, actor="qa-user", force=True)
        self.assertTrue(bool(archived.is_archived))

    def test_cleanup_archived_media_assets_by_retention(self) -> None:
        product = self._create_product(sku="GS-MEDIA-RET-001", qty=1)
        old_archived = self.repo.create_media_asset(
            media_type="image",
            original_filename="old_archived.jpg",
            content_type="image/jpeg",
            size_bytes=10,
            s3_bucket="bucket",
            s3_key="media/old_archived.jpg",
            s3_url="https://example/media/old_archived.jpg",
            product_id=product.id,
            listing_id=None,
            uploaded_by="qa-user",
        )
        recent_archived = self.repo.create_media_asset(
            media_type="image",
            original_filename="recent_archived.jpg",
            content_type="image/jpeg",
            size_bytes=10,
            s3_bucket="bucket",
            s3_key="media/recent_archived.jpg",
            s3_url="https://example/media/recent_archived.jpg",
            product_id=product.id,
            listing_id=None,
            uploaded_by="qa-user",
        )
        active_media = self.repo.create_media_asset(
            media_type="image",
            original_filename="active.jpg",
            content_type="image/jpeg",
            size_bytes=10,
            s3_bucket="bucket",
            s3_key="media/active.jpg",
            s3_url="https://example/media/active.jpg",
            product_id=product.id,
            listing_id=None,
            uploaded_by="qa-user",
        )

        self.repo.archive_media_asset(old_archived.id, actor="qa-user")
        self.repo.archive_media_asset(recent_archived.id, actor="qa-user")

        old_row = self.db.get(MediaAsset, old_archived.id)
        old_row.updated_at = utcnow_naive() - timedelta(days=200)
        recent_row = self.db.get(MediaAsset, recent_archived.id)
        recent_row.updated_at = utcnow_naive() - timedelta(days=10)
        self.db.commit()

        result = self.repo.cleanup_archived_media_assets(retain_days=90, actor="qa-user")
        self.assertEqual(int(result.get("deleted_archived_media", 0)), 1)
        self.assertIsNone(self.db.get(MediaAsset, old_archived.id))
        self.assertIsNotNone(self.db.get(MediaAsset, recent_archived.id))
        self.assertIsNotNone(self.db.get(MediaAsset, active_media.id))

        logs = self.repo.list_audit_logs(limit=200)
        cleanup_log = next(
            (
                l
                for l in logs
                if l.entity_type == "media_asset"
                and l.action == "retention_cleanup"
                and l.actor == "qa-user"
            ),
            None,
        )
        self.assertIsNotNone(cleanup_log)

    def test_cleanup_archived_listings_with_dependency_guardrails(self) -> None:
        product = self._create_product(sku="GS-LIST-RET-001", qty=3)
        deletable = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Deletable Archived Listing",
            listing_price=Decimal("10.00"),
            quantity_listed=1,
        )
        blocked = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Blocked Archived Listing",
            listing_price=Decimal("12.00"),
            quantity_listed=1,
        )
        self.repo.archive_listing(deletable.id, actor="qa-user")
        self.repo.archive_listing(blocked.id, actor="qa-user")
        self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("12.00"),
            fees=Decimal("1.00"),
            shipping_cost=Decimal("1.00"),
            quantity_sold=1,
            product_id=product.id,
            listing_id=blocked.id,
            sold_at=datetime(2026, 4, 10, 10, 0, 0),
        )
        self.db.get(type(deletable), deletable.id).updated_at = utcnow_naive() - timedelta(days=400)
        self.db.get(type(blocked), blocked.id).updated_at = utcnow_naive() - timedelta(days=400)
        self.db.commit()

        result = self.repo.cleanup_archived_listings(retain_days=365, actor="qa-user")
        self.assertEqual(int(result.get("deleted_archived_listings", 0)), 1)
        self.assertEqual(int(result.get("skipped_listings_with_dependencies", 0)), 1)
        self.assertIsNone(self.db.get(type(deletable), deletable.id))
        self.assertIsNotNone(self.db.get(type(blocked), blocked.id))

    def test_cleanup_archived_products_and_lots_with_dependency_guardrails(self) -> None:
        source = self.repo.create_inventory_source(name="Retention Vendor", source_type="vendor")
        deletable_product = self._create_product(sku="GS-PROD-RET-DEL", qty=1)
        blocked_product = self._create_product(sku="GS-PROD-RET-BLK", qty=1)
        self.repo.archive_product(deletable_product.id, actor="qa-user", force=True)
        self.repo.archive_product(blocked_product.id, actor="qa-user", force=True)
        self.repo.create_media_asset(
            media_type="image",
            original_filename="blocked_product.jpg",
            content_type="image/jpeg",
            size_bytes=10,
            s3_bucket="bucket",
            s3_key="media/blocked_product.jpg",
            s3_url="https://example/media/blocked_product.jpg",
            product_id=blocked_product.id,
            listing_id=None,
            uploaded_by="qa-user",
        )
        self.db.get(type(deletable_product), deletable_product.id).updated_at = utcnow_naive() - timedelta(days=400)
        self.db.get(type(blocked_product), blocked_product.id).updated_at = utcnow_naive() - timedelta(days=400)

        deletable_lot = self.repo.create_purchase_lot(
            lot_code="LOT-RET-DEL",
            vendor="Retention Vendor",
            purchase_date=datetime(2026, 1, 1, 0, 0, 0),
            total_cost=Decimal("100.00"),
            source_id=source.id,
            notes="",
        )
        blocked_lot = self.repo.create_purchase_lot(
            lot_code="LOT-RET-BLK",
            vendor="Retention Vendor",
            purchase_date=datetime(2026, 1, 1, 0, 0, 0),
            total_cost=Decimal("120.00"),
            source_id=source.id,
            notes="",
        )
        self.repo.archive_purchase_lot(deletable_lot.id, actor="qa-user")
        self.repo.archive_purchase_lot(blocked_lot.id, actor="qa-user")
        self.repo.assign_product_to_lot(
            product_id=blocked_product.id,
            lot_id=blocked_lot.id,
            quantity_acquired=1,
            unit_cost=Decimal("10.00"),
            acquired_at=datetime(2026, 1, 2, 0, 0, 0),
        )
        self.db.get(type(deletable_lot), deletable_lot.id).updated_at = utcnow_naive() - timedelta(days=400)
        self.db.get(type(blocked_lot), blocked_lot.id).updated_at = utcnow_naive() - timedelta(days=400)
        self.db.commit()

        product_result = self.repo.cleanup_archived_products(retain_days=365, actor="qa-user")
        self.assertEqual(int(product_result.get("deleted_archived_products", 0)), 1)
        self.assertEqual(int(product_result.get("skipped_products_with_dependencies", 0)), 1)
        self.assertIsNone(self.db.get(type(deletable_product), deletable_product.id))
        self.assertIsNotNone(self.db.get(type(blocked_product), blocked_product.id))

        lot_result = self.repo.cleanup_archived_purchase_lots(retain_days=365, actor="qa-user")
        self.assertEqual(int(lot_result.get("deleted_archived_lots", 0)), 1)
        self.assertEqual(int(lot_result.get("skipped_lots_with_dependencies", 0)), 1)
        self.assertIsNone(self.db.get(type(deletable_lot), deletable_lot.id))
        self.assertIsNotNone(self.db.get(type(blocked_lot), blocked_lot.id))

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

    def test_lot_total_cost_feeds_cost_maps_and_dashboard_when_assignment_costs_blank(self) -> None:
        p1 = self.repo.create_product(
            sku="GS-LOT-COST-MAP-1",
            title="Lot Cost Product 1",
            category="bullion",
            description="",
            metal_type="silver",
            weight_oz=None,
            acquisition_cost=None,
            current_quantity=2,
            acquired_at=datetime(2026, 3, 27, 8, 0, 0),
        )
        p2 = self.repo.create_product(
            sku="GS-LOT-COST-MAP-2",
            title="Lot Cost Product 2",
            category="bullion",
            description="",
            metal_type="silver",
            weight_oz=None,
            acquisition_cost=None,
            current_quantity=4,
            acquired_at=datetime(2026, 3, 27, 8, 0, 0),
        )
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-COST-MAP-1",
            vendor="Dealer",
            purchase_date=datetime(2026, 3, 27, 7, 0, 0),
            total_cost=Decimal("120.00"),
            total_tax_paid=Decimal("12.00"),
            notes="whole-lot cost",
        )
        self.repo.assign_product_to_lot(
            product_id=p1.id,
            lot_id=lot.id,
            quantity_acquired=2,
            unit_cost=None,
            acquired_at=datetime(2026, 3, 27, 8, 0, 0),
        )
        self.repo.assign_product_to_lot(
            product_id=p2.id,
            lot_id=lot.id,
            quantity_acquired=4,
            unit_cost=None,
            acquired_at=datetime(2026, 3, 27, 8, 0, 0),
        )

        maps = self.repo.report_sale_unit_cost_maps(
            end_dt=datetime(2026, 3, 28),
            default_unit_cost_by_product={p1.id: 0.0, p2.id: 0.0},
        )
        self.assertAlmostEqual(maps["lot_weighted_unit_cost_by_product"][p1.id], 22.0)
        self.assertAlmostEqual(maps["lot_weighted_unit_cost_by_product"][p2.id], 22.0)
        self.assertEqual(maps["lot_weighted_unit_cost_source_by_product"][p1.id], "lot_equal_quantity_fallback")
        self.assertEqual(maps["lot_weighted_unit_cost_source_by_product"][p2.id], "lot_equal_quantity_fallback")

        metrics = self.repo.dashboard_metrics()
        self.assertAlmostEqual(metrics["inventory_cost"], 132.0)

    def test_lot_cost_maps_allocate_only_remaining_lot_cost_to_blank_assignments(self) -> None:
        p1 = self.repo.create_product(
            sku="GS-LOT-REMAIN-1",
            title="Explicit Cost Product",
            category="bullion",
            description="",
            metal_type="silver",
            weight_oz=None,
            acquisition_cost=None,
            current_quantity=2,
            acquired_at=datetime(2026, 3, 28, 8, 0, 0),
        )
        p2 = self.repo.create_product(
            sku="GS-LOT-REMAIN-2",
            title="Blank Cost Product",
            category="bullion",
            description="",
            metal_type="silver",
            weight_oz=None,
            acquisition_cost=None,
            current_quantity=4,
            acquired_at=datetime(2026, 3, 28, 8, 0, 0),
        )
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-REMAIN-1",
            vendor="Dealer",
            purchase_date=datetime(2026, 3, 28, 7, 0, 0),
            total_cost=Decimal("100.00"),
            notes="remaining allocation",
        )
        self.repo.assign_product_to_lot(
            product_id=p1.id,
            lot_id=lot.id,
            quantity_acquired=2,
            unit_cost=Decimal("30.00"),
            acquired_at=datetime(2026, 3, 28, 8, 0, 0),
        )
        self.repo.assign_product_to_lot(
            product_id=p2.id,
            lot_id=lot.id,
            quantity_acquired=4,
            unit_cost=None,
            acquired_at=datetime(2026, 3, 28, 8, 0, 0),
        )

        maps = self.repo.report_sale_unit_cost_maps(
            end_dt=datetime(2026, 3, 29),
            default_unit_cost_by_product={p1.id: 0.0, p2.id: 0.0},
        )
        self.assertAlmostEqual(maps["lot_weighted_unit_cost_by_product"][p1.id], 30.0)
        self.assertAlmostEqual(maps["lot_weighted_unit_cost_by_product"][p2.id], 10.0)
        self.assertEqual(maps["lot_weighted_unit_cost_source_by_product"][p1.id], "assignment_unit_landed_cost")
        self.assertEqual(maps["lot_weighted_unit_cost_source_by_product"][p2.id], "lot_equal_quantity_fallback")

    def test_expected_lot_quantity_prevents_partial_check_in_from_absorbing_full_lot_cost(self) -> None:
        product = self.repo.create_product(
            sku="GS-LOT-PARTIAL-1",
            title="Partial Lot Product",
            category="bullion",
            description="",
            metal_type="silver",
            weight_oz=None,
            acquisition_cost=None,
            current_quantity=2,
            acquired_at=datetime(2026, 3, 29, 8, 0, 0),
        )
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-PARTIAL-1",
            vendor="Dealer",
            purchase_date=datetime(2026, 3, 29, 7, 0, 0),
            total_cost=Decimal("100.00"),
            expected_total_quantity=10,
            notes="partial check-in",
        )
        self.repo.assign_product_to_lot(
            product_id=product.id,
            lot_id=lot.id,
            quantity_acquired=2,
            unit_cost=None,
            acquired_at=datetime(2026, 3, 29, 8, 0, 0),
        )

        maps = self.repo.report_sale_unit_cost_maps(
            end_dt=datetime(2026, 3, 30),
            default_unit_cost_by_product={product.id: 0.0},
        )

        self.assertAlmostEqual(maps["lot_weighted_unit_cost_by_product"][product.id], 10.0)
        self.assertAlmostEqual(maps["fifo_remaining_unit_cost_by_product"][product.id], 10.0)
        self.assertEqual(
            maps["lot_weighted_unit_cost_source_by_product"][product.id],
            "lot_expected_quantity_fallback",
        )
        self.assertEqual(
            maps["fifo_remaining_unit_cost_source_by_product"][product.id],
            "lot_expected_quantity_fallback",
        )

        exceptions = self.repo.report_accounting_exception_rows(
            start_dt=datetime(2026, 3, 29),
            end_dt=datetime(2026, 3, 30),
        )
        self.assertTrue(
            any(
                row.get("exception_type") == "lot_allocation_pending_check_in"
                and int(row.get("entity_id") or 0) == int(lot.id)
                for row in exceptions
            )
        )

    def test_dashboard_profit_uses_expected_lot_quantity_cogs_for_partial_lot_sale(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self.repo.create_product(
            sku="GS-DASH-PARTIAL-LOT",
            title="Dashboard Partial Lot Product",
            category="bullion",
            description="",
            metal_type="silver",
            weight_oz=None,
            acquisition_cost=None,
            product_cost=None,
            current_quantity=1,
            acquired_at=now - timedelta(days=2),
        )
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-DASH-PARTIAL",
            vendor="Dealer",
            purchase_date=now - timedelta(days=2, hours=1),
            total_cost=Decimal("200.00"),
            expected_total_quantity=20,
            notes="partial dashboard check-in",
        )
        self.repo.assign_product_to_lot(
            product_id=product.id,
            lot_id=lot.id,
            quantity_acquired=2,
            unit_cost=None,
            acquired_at=now - timedelta(days=2),
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("30.00"),
            fees=Decimal("3.00"),
            shipping_cost=Decimal("5.00"),
            shipping_label_cost=Decimal("4.00"),
            quantity_sold=1,
            product_id=product.id,
            sold_at=now - timedelta(days=1),
        )

        maps = self.repo.report_sale_unit_cost_maps(
            end_dt=now,
            default_unit_cost_by_product={product.id: 0.0},
        )
        self.assertAlmostEqual(maps["fifo_unit_cost_by_sale"][sale.id], 10.0, places=2)
        self.assertEqual(
            maps["fifo_unit_cost_source_by_sale"][sale.id],
            "lot_expected_quantity_fallback",
        )

        metrics = self.repo.dashboard_live_metrics(now=now)
        self.assertAlmostEqual(metrics["sales_30d_est_cogs"], 10.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_est_profit"], 18.0, places=2)
        self.assertEqual(
            metrics["sales_30d_cogs_source_counts"],
            {"lot_expected_quantity_fallback": 1},
        )
        self.assertEqual(metrics["sales_30d_profit_basis_status"], "partial_lot_estimate")
        self.assertEqual(metrics["sales_30d_cogs_review_count"], 0)
        self.assertAlmostEqual(metrics["sales_30d_cogs_review_amount"], 0.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_cogs_estimate_amount"], 10.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_cogs_verified_amount"], 0.0, places=2)
        self.assertEqual(metrics["sales_30d_cogs_estimate_sale_ids"], [sale.id])
        profit_basis_rows = self.repo.dashboard_profit_basis_rows(now=now)
        profit_basis = next(row for row in profit_basis_rows if int(row["sale_id"]) == int(sale.id))
        self.assertAlmostEqual(profit_basis["net_before_cogs"], 28.0, places=2)
        self.assertAlmostEqual(profit_basis["fifo_cogs"], 10.0, places=2)
        self.assertAlmostEqual(profit_basis["profit_before_returns"], 18.0, places=2)
        self.assertEqual(profit_basis["fifo_cost_source"], "lot_expected_quantity_fallback")
        self.assertFalse(profit_basis["basis_review_required"])
        self.assertTrue(profit_basis["basis_is_estimate"])
        self.assertEqual(profit_basis["cogs_basis_bucket"], "estimate")
        self.assertEqual(profit_basis["basis_review_severity"], "estimate")
        self.assertIn("expected lot quantity", profit_basis["basis_review_reason"])
        self.assertEqual(profit_basis["fifo_cogs_evidence_rows"], 1)

    def test_dashboard_live_metrics_exclude_future_dated_sales_from_profit_window(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self.repo.create_product(
            sku="GS-DASH-FUTURE-SALE",
            title="Dashboard Future Sale Product",
            category="bullion",
            description="",
            metal_type="silver",
            weight_oz=None,
            acquisition_cost=Decimal("10.00"),
            product_cost=None,
            current_quantity=2,
            acquired_at=now - timedelta(days=1),
        )
        self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("30.00"),
            fees=Decimal("3.00"),
            shipping_cost=Decimal("5.00"),
            shipping_label_cost=Decimal("4.00"),
            quantity_sold=1,
            product_id=product.id,
            sold_at=now - timedelta(hours=1),
        )
        self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("999.00"),
            fees=Decimal("99.00"),
            shipping_cost=Decimal("9.00"),
            shipping_label_cost=Decimal("8.00"),
            quantity_sold=1,
            product_id=product.id,
            sold_at=now + timedelta(days=1),
        )

        metrics = self.repo.dashboard_live_metrics(now=now)

        self.assertEqual(metrics["sales_30d_count"], 1)
        self.assertAlmostEqual(metrics["sales_30d_gross"], 30.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_net"], 28.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_est_cogs"], 10.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_est_profit"], 18.0, places=2)

    def test_dashboard_profit_flags_equal_fallback_lot_cogs_for_review(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self.repo.create_product(
            sku="GS-DASH-EQUAL-FALLBACK",
            title="Dashboard Equal Fallback Product",
            category="bullion",
            description="",
            metal_type="silver",
            weight_oz=None,
            acquisition_cost=None,
            product_cost=None,
            current_quantity=1,
            acquired_at=now - timedelta(days=2),
        )
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-DASH-EQUAL-FALLBACK",
            vendor="Dealer",
            purchase_date=now - timedelta(days=2, hours=1),
            total_cost=Decimal("200.00"),
            notes="under-defined partial lot",
        )
        self.repo.assign_product_to_lot(
            product_id=product.id,
            lot_id=lot.id,
            quantity_acquired=1,
            unit_cost=None,
            acquired_at=now - timedelta(days=2),
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("30.00"),
            fees=Decimal("3.00"),
            shipping_cost=Decimal("5.00"),
            shipping_label_cost=Decimal("4.00"),
            quantity_sold=1,
            product_id=product.id,
            sold_at=now - timedelta(days=1),
        )

        maps = self.repo.report_sale_unit_cost_maps(
            end_dt=now,
            default_unit_cost_by_product={product.id: 0.0},
        )
        self.assertAlmostEqual(maps["fifo_unit_cost_by_sale"][sale.id], 200.0, places=2)
        self.assertEqual(
            maps["fifo_unit_cost_source_by_sale"][sale.id],
            "lot_equal_quantity_fallback",
        )

        metrics = self.repo.dashboard_live_metrics(now=now)
        self.assertAlmostEqual(metrics["sales_30d_est_cogs"], 200.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_est_profit"], -172.0, places=2)
        self.assertEqual(
            metrics["sales_30d_cogs_source_counts"],
            {"lot_equal_quantity_fallback": 1},
        )
        self.assertEqual(metrics["sales_30d_cogs_review_count"], 1)
        self.assertAlmostEqual(metrics["sales_30d_cogs_review_amount"], 200.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_cogs_estimate_amount"], 0.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_cogs_verified_amount"], 0.0, places=2)
        self.assertEqual(metrics["sales_30d_cogs_review_sale_ids"], [sale.id])
        self.assertEqual(metrics["sales_30d_profit_basis_status"], "review_needed")
        profit_basis_rows = self.repo.dashboard_profit_basis_rows(now=now)
        profit_basis = next(row for row in profit_basis_rows if int(row["sale_id"]) == int(sale.id))
        self.assertTrue(profit_basis["basis_review_required"])
        self.assertFalse(profit_basis["basis_is_estimate"])
        self.assertEqual(profit_basis["cogs_basis_bucket"], "review")
        self.assertEqual(profit_basis["basis_review_severity"], "review")
        self.assertIn("equal-quantity fallback", profit_basis["basis_review_reason"])

    def test_dashboard_profit_flags_sale_spanning_explicit_and_fallback_lots_for_review(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self.repo.create_product(
            sku="GS-DASH-MIXED-FIFO-REVIEW",
            title="Dashboard Mixed FIFO Review Product",
            category="bullion",
            description="",
            metal_type="silver",
            weight_oz=None,
            acquisition_cost=None,
            product_cost=None,
            current_quantity=2,
            acquired_at=now - timedelta(days=4),
        )
        explicit_lot = self.repo.create_purchase_lot(
            lot_code="LOT-DASH-MIXED-EXPLICIT",
            vendor="Dealer",
            purchase_date=now - timedelta(days=4, hours=1),
            total_cost=Decimal("10.00"),
        )
        fallback_lot = self.repo.create_purchase_lot(
            lot_code="LOT-DASH-MIXED-FALLBACK",
            vendor="Dealer",
            purchase_date=now - timedelta(days=3, hours=1),
            total_cost=Decimal("50.00"),
        )
        self.repo.assign_product_to_lot(
            product_id=product.id,
            lot_id=explicit_lot.id,
            quantity_acquired=1,
            unit_cost=Decimal("10.00"),
            acquired_at=now - timedelta(days=4),
        )
        self.repo.assign_product_to_lot(
            product_id=product.id,
            lot_id=fallback_lot.id,
            quantity_acquired=1,
            unit_cost=None,
            acquired_at=now - timedelta(days=3),
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("5.00"),
            shipping_cost=Decimal("0.00"),
            shipping_label_cost=Decimal("0.00"),
            quantity_sold=2,
            product_id=product.id,
            sold_at=now - timedelta(days=1),
        )

        maps = self.repo.report_sale_unit_cost_maps(
            end_dt=now,
            default_unit_cost_by_product={product.id: 0.0},
        )
        self.assertAlmostEqual(maps["fifo_total_cost_by_sale"][sale.id], 60.0, places=2)
        self.assertEqual(maps["fifo_unit_cost_source_by_sale"][sale.id], "mixed_fifo_cost")
        self.assertEqual(
            [row["cost_source"] for row in maps["fifo_cogs_evidence_by_sale"][sale.id]],
            ["assignment_unit_landed_cost", "lot_equal_quantity_fallback"],
        )

        metrics = self.repo.dashboard_live_metrics(now=now)
        self.assertEqual(metrics["sales_30d_profit_basis_status"], "review_needed")
        self.assertEqual(metrics["sales_30d_cogs_review_count"], 1)
        self.assertAlmostEqual(metrics["sales_30d_cogs_review_amount"], 60.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_cogs_verified_amount"], 0.0, places=2)
        self.assertEqual(metrics["sales_30d_cogs_review_sale_ids"], [sale.id])
        self.assertEqual(metrics["sales_30d_cogs_source_counts"], {"mixed_fifo_cost": 1})
        profit_basis = next(
            row for row in self.repo.dashboard_profit_basis_rows(now=now) if int(row["sale_id"]) == int(sale.id)
        )
        self.assertTrue(profit_basis["basis_review_required"])
        self.assertEqual(profit_basis["cogs_basis_bucket"], "review")
        self.assertEqual(profit_basis["basis_review_severity"], "review")
        self.assertIn("reviewed and fallback FIFO basis sources", profit_basis["basis_review_reason"])

        self.repo.suppress_accounting_exception(
            exception_type="mixed_fifo_cost_review",
            target_entity_type="sale",
            target_entity_id=sale.id,
            actor="qa-user",
            reason="Reviewed mixed FIFO evidence.",
        )
        reviewed_metrics = self.repo.dashboard_live_metrics(now=now)
        self.assertEqual(reviewed_metrics["sales_30d_profit_basis_status"], "ok")
        self.assertEqual(reviewed_metrics["sales_30d_cogs_review_count"], 0)
        self.assertAlmostEqual(reviewed_metrics["sales_30d_cogs_review_amount"], 0.0, places=2)
        self.assertAlmostEqual(reviewed_metrics["sales_30d_cogs_verified_amount"], 60.0, places=2)
        reviewed_profit_basis = next(
            row for row in self.repo.dashboard_profit_basis_rows(now=now) if int(row["sale_id"]) == int(sale.id)
        )
        self.assertFalse(reviewed_profit_basis["basis_review_required"])
        self.assertEqual(reviewed_profit_basis["basis_review_severity"], "reviewed")

    def test_dashboard_profit_marks_mixed_verified_fifo_as_ok(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self.repo.create_product(
            sku="GS-DASH-MIXED-FIFO-OK",
            title="Dashboard Mixed FIFO OK Product",
            category="bullion",
            description="",
            metal_type="silver",
            weight_oz=None,
            acquisition_cost=None,
            product_cost=None,
            current_quantity=2,
            acquired_at=now - timedelta(days=4),
        )
        explicit_lot = self.repo.create_purchase_lot(
            lot_code="LOT-DASH-MIXED-OK-EXPLICIT",
            vendor="Dealer",
            purchase_date=now - timedelta(days=4, hours=1),
            total_cost=Decimal("10.00"),
        )
        weighted_lot = self.repo.create_purchase_lot(
            lot_code="LOT-DASH-MIXED-OK-WEIGHT",
            vendor="Dealer",
            purchase_date=now - timedelta(days=3, hours=1),
            total_cost=Decimal("50.00"),
        )
        self.repo.assign_product_to_lot(
            product_id=product.id,
            lot_id=explicit_lot.id,
            quantity_acquired=1,
            unit_cost=Decimal("10.00"),
            acquired_at=now - timedelta(days=4),
        )
        self.repo.assign_product_to_lot(
            product_id=product.id,
            lot_id=weighted_lot.id,
            quantity_acquired=1,
            unit_cost=None,
            allocation_weight=Decimal("1"),
            acquired_at=now - timedelta(days=3),
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("5.00"),
            shipping_cost=Decimal("0.00"),
            shipping_label_cost=Decimal("0.00"),
            quantity_sold=2,
            product_id=product.id,
            sold_at=now - timedelta(days=1),
        )

        maps = self.repo.report_sale_unit_cost_maps(
            end_dt=now,
            default_unit_cost_by_product={product.id: 0.0},
        )
        self.assertAlmostEqual(maps["fifo_total_cost_by_sale"][sale.id], 60.0, places=2)
        self.assertEqual(maps["fifo_unit_cost_source_by_sale"][sale.id], "mixed_verified_fifo_cost")
        self.assertEqual(
            [row["cost_source"] for row in maps["fifo_cogs_evidence_by_sale"][sale.id]],
            ["assignment_unit_landed_cost", "lot_allocation_weight"],
        )

        metrics = self.repo.dashboard_live_metrics(now=now)
        self.assertEqual(metrics["sales_30d_profit_basis_status"], "ok")
        self.assertEqual(metrics["sales_30d_cogs_review_count"], 0)
        self.assertAlmostEqual(metrics["sales_30d_cogs_review_amount"], 0.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_cogs_estimate_amount"], 0.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_cogs_verified_amount"], 60.0, places=2)
        self.assertEqual(metrics["sales_30d_cogs_source_counts"], {"mixed_verified_fifo_cost": 1})
        profit_basis = next(
            row for row in self.repo.dashboard_profit_basis_rows(now=now) if int(row["sale_id"]) == int(sale.id)
        )
        self.assertFalse(profit_basis["basis_review_required"])
        self.assertFalse(profit_basis["basis_is_estimate"])
        self.assertEqual(profit_basis["cogs_basis_bucket"], "ok")
        self.assertEqual(profit_basis["basis_review_severity"], "ok")
        self.assertIn("multiple verified FIFO", profit_basis["basis_review_reason"])

    def test_dashboard_profit_marks_mixed_expected_fifo_as_estimate(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self.repo.create_product(
            sku="GS-DASH-MIXED-FIFO-EST",
            title="Dashboard Mixed FIFO Estimate Product",
            category="bullion",
            description="",
            metal_type="silver",
            weight_oz=None,
            acquisition_cost=None,
            product_cost=None,
            current_quantity=2,
            acquired_at=now - timedelta(days=4),
        )
        explicit_lot = self.repo.create_purchase_lot(
            lot_code="LOT-DASH-MIXED-EST-EXPLICIT",
            vendor="Dealer",
            purchase_date=now - timedelta(days=4, hours=1),
            total_cost=Decimal("10.00"),
        )
        expected_lot = self.repo.create_purchase_lot(
            lot_code="LOT-DASH-MIXED-EST-EXPECTED",
            vendor="Dealer",
            purchase_date=now - timedelta(days=3, hours=1),
            total_cost=Decimal("200.00"),
            expected_total_quantity=20,
        )
        self.repo.assign_product_to_lot(
            product_id=product.id,
            lot_id=explicit_lot.id,
            quantity_acquired=1,
            unit_cost=Decimal("10.00"),
            acquired_at=now - timedelta(days=4),
        )
        self.repo.assign_product_to_lot(
            product_id=product.id,
            lot_id=expected_lot.id,
            quantity_acquired=1,
            unit_cost=None,
            acquired_at=now - timedelta(days=3),
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("5.00"),
            shipping_cost=Decimal("0.00"),
            shipping_label_cost=Decimal("0.00"),
            quantity_sold=2,
            product_id=product.id,
            sold_at=now - timedelta(days=1),
        )

        maps = self.repo.report_sale_unit_cost_maps(
            end_dt=now,
            default_unit_cost_by_product={product.id: 0.0},
        )
        self.assertAlmostEqual(maps["fifo_total_cost_by_sale"][sale.id], 20.0, places=2)
        self.assertEqual(maps["fifo_unit_cost_source_by_sale"][sale.id], "mixed_estimate_fifo_cost")
        self.assertEqual(
            [row["cost_source"] for row in maps["fifo_cogs_evidence_by_sale"][sale.id]],
            ["assignment_unit_landed_cost", "lot_expected_quantity_fallback"],
        )

        metrics = self.repo.dashboard_live_metrics(now=now)
        self.assertEqual(metrics["sales_30d_profit_basis_status"], "partial_lot_estimate")
        self.assertEqual(metrics["sales_30d_cogs_review_count"], 0)
        self.assertAlmostEqual(metrics["sales_30d_cogs_review_amount"], 0.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_cogs_estimate_amount"], 20.0, places=2)
        self.assertAlmostEqual(metrics["sales_30d_cogs_verified_amount"], 0.0, places=2)
        self.assertEqual(metrics["sales_30d_cogs_estimate_sale_ids"], [sale.id])
        self.assertEqual(metrics["sales_30d_cogs_source_counts"], {"mixed_estimate_fifo_cost": 1})
        profit_basis = next(
            row for row in self.repo.dashboard_profit_basis_rows(now=now) if int(row["sale_id"]) == int(sale.id)
        )
        self.assertFalse(profit_basis["basis_review_required"])
        self.assertTrue(profit_basis["basis_is_estimate"])
        self.assertEqual(profit_basis["cogs_basis_bucket"], "estimate")
        self.assertEqual(profit_basis["basis_review_severity"], "estimate")
        self.assertIn("expected-quantity lot estimates", profit_basis["basis_review_reason"])

    def test_lot_allocation_weight_splits_mixed_lot_cost_by_product_value_share(self) -> None:
        gold = self.repo.create_product(
            sku="GS-MIXED-LOT-GOLD",
            title="Mixed Lot Gold Coin",
            category="coin",
            description="",
            metal_type="gold",
            weight_oz=None,
            acquisition_cost=None,
            current_quantity=1,
            acquired_at=datetime(2026, 3, 30, 8, 0, 0),
        )
        silver = self.repo.create_product(
            sku="GS-MIXED-LOT-SILVER",
            title="Mixed Lot Silver Round",
            category="bullion",
            description="",
            metal_type="silver",
            weight_oz=None,
            acquisition_cost=None,
            current_quantity=1,
            acquired_at=datetime(2026, 3, 30, 8, 0, 0),
        )
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-MIXED-WEIGHT-1",
            vendor="Dealer",
            purchase_date=datetime(2026, 3, 30, 7, 0, 0),
            total_cost=Decimal("100.00"),
            notes="mixed value lot",
        )
        self.repo.assign_product_to_lot(
            product_id=gold.id,
            lot_id=lot.id,
            quantity_acquired=1,
            unit_cost=None,
            allocation_weight=Decimal("9"),
            acquired_at=datetime(2026, 3, 30, 8, 0, 0),
        )
        self.repo.assign_product_to_lot(
            product_id=silver.id,
            lot_id=lot.id,
            quantity_acquired=1,
            unit_cost=None,
            allocation_weight=Decimal("1"),
            acquired_at=datetime(2026, 3, 30, 8, 0, 0),
        )

        maps = self.repo.report_sale_unit_cost_maps(
            end_dt=datetime(2026, 3, 31),
            default_unit_cost_by_product={gold.id: 0.0, silver.id: 0.0},
        )

        self.assertAlmostEqual(maps["lot_weighted_unit_cost_by_product"][gold.id], 90.0)
        self.assertAlmostEqual(maps["lot_weighted_unit_cost_by_product"][silver.id], 10.0)
        self.assertAlmostEqual(maps["fifo_remaining_unit_cost_by_product"][gold.id], 90.0)
        self.assertAlmostEqual(maps["fifo_remaining_unit_cost_by_product"][silver.id], 10.0)
        self.assertEqual(maps["lot_weighted_unit_cost_source_by_product"][gold.id], "lot_allocation_weight")
        self.assertEqual(maps["lot_weighted_unit_cost_source_by_product"][silver.id], "lot_allocation_weight")

        metrics = self.repo.dashboard_metrics()
        self.assertAlmostEqual(metrics["inventory_cost"], 100.0)

        exceptions = self.repo.report_accounting_exception_rows(
            start_dt=datetime(2026, 3, 30),
            end_dt=datetime(2026, 3, 31),
        )
        self.assertFalse(
            any(
                row.get("exception_type") == "lot_equal_fallback_review_needed"
                and int(row.get("entity_id") or 0) == int(lot.id)
                for row in exceptions
            )
        )

    def test_accounting_exception_flags_multi_product_equal_lot_fallback(self) -> None:
        p1 = self.repo.create_product(
            sku="GS-MIXED-LOT-EQUAL-1",
            title="Mixed Lot Equal Fallback 1",
            category="coin",
            description="",
            metal_type="gold",
            weight_oz=None,
            acquisition_cost=None,
            current_quantity=1,
            acquired_at=datetime(2026, 4, 1, 8, 0, 0),
        )
        p2 = self.repo.create_product(
            sku="GS-MIXED-LOT-EQUAL-2",
            title="Mixed Lot Equal Fallback 2",
            category="bullion",
            description="",
            metal_type="silver",
            weight_oz=None,
            acquisition_cost=None,
            current_quantity=1,
            acquired_at=datetime(2026, 4, 1, 8, 0, 0),
        )
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-MIXED-EQUAL-FALLBACK",
            vendor="Dealer",
            purchase_date=datetime(2026, 4, 1, 7, 0, 0),
            total_cost=Decimal("100.00"),
            notes="needs allocation review",
        )
        self.repo.assign_product_to_lot(
            product_id=p1.id,
            lot_id=lot.id,
            quantity_acquired=1,
            unit_cost=None,
            acquired_at=datetime(2026, 4, 1, 8, 0, 0),
        )
        self.repo.assign_product_to_lot(
            product_id=p2.id,
            lot_id=lot.id,
            quantity_acquired=1,
            unit_cost=None,
            acquired_at=datetime(2026, 4, 1, 8, 0, 0),
        )

        exceptions = self.repo.report_accounting_exception_rows(
            start_dt=datetime(2026, 4, 1),
            end_dt=datetime(2026, 4, 2),
        )

        match = next(
            row
            for row in exceptions
            if row.get("exception_type") == "lot_equal_fallback_review_needed"
            and int(row.get("entity_id") or 0) == int(lot.id)
        )
        self.assertEqual(match["severity"], "P2")
        self.assertIn("equal quantity fallback", str(match.get("details") or ""))

    def test_repair_equal_quantity_fallback_records_allocation_weights(self) -> None:
        p1 = self.repo.create_product(
            sku="GS-MIXED-EQUAL-REPAIR-1",
            title="Mixed Equal Repair 1",
            category="coin",
            description="",
            metal_type="gold",
            weight_oz=None,
            acquisition_cost=None,
            current_quantity=2,
            acquired_at=datetime(2026, 4, 2, 8, 0, 0),
        )
        p2 = self.repo.create_product(
            sku="GS-MIXED-EQUAL-REPAIR-2",
            title="Mixed Equal Repair 2",
            category="bullion",
            description="",
            metal_type="silver",
            weight_oz=None,
            acquisition_cost=None,
            current_quantity=3,
            acquired_at=datetime(2026, 4, 2, 8, 0, 0),
        )
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-MIXED-EQUAL-REPAIR",
            vendor="Dealer",
            purchase_date=datetime(2026, 4, 2, 7, 0, 0),
            total_cost=Decimal("100.00"),
            notes="accept equal quantity allocation",
        )
        self.repo.assign_product_to_lot(
            product_id=p1.id,
            lot_id=lot.id,
            quantity_acquired=2,
            unit_cost=None,
            acquired_at=datetime(2026, 4, 2, 8, 0, 0),
        )
        self.repo.assign_product_to_lot(
            product_id=p2.id,
            lot_id=lot.id,
            quantity_acquired=3,
            unit_cost=None,
            acquired_at=datetime(2026, 4, 2, 8, 0, 0),
        )

        preview = self.repo.repair_purchase_lot_equal_quantity_allocation_weights(
            lot.id,
            actor="qa",
            dry_run=True,
        )
        self.assertTrue(preview["dry_run"])
        self.assertEqual(preview["target_assignment_count"], 2)
        self.assertEqual(preview["target_quantity"], 5)
        before_assignments = self.db.scalars(
            select(ProductLotAssignment)
            .where(ProductLotAssignment.lot_id == lot.id)
            .order_by(ProductLotAssignment.product_id.asc())
        ).all()
        self.assertTrue(all(row.allocation_weight is None for row in before_assignments))

        result = self.repo.repair_purchase_lot_equal_quantity_allocation_weights(
            lot.id,
            actor="qa",
            dry_run=False,
        )

        self.assertFalse(result["dry_run"])
        assignments = self.db.scalars(
            select(ProductLotAssignment)
            .where(ProductLotAssignment.lot_id == lot.id)
            .order_by(ProductLotAssignment.product_id.asc())
        ).all()
        self.assertEqual([assignment.allocation_weight for assignment in assignments], [Decimal("2"), Decimal("3")])

        maps = self.repo.report_sale_unit_cost_maps(
            end_dt=datetime(2026, 4, 3),
            default_unit_cost_by_product={p1.id: 0.0, p2.id: 0.0},
        )
        self.assertAlmostEqual(maps["lot_weighted_unit_cost_by_product"][p1.id], 20.0)
        self.assertAlmostEqual(maps["lot_weighted_unit_cost_by_product"][p2.id], 20.0)
        self.assertEqual(maps["lot_weighted_unit_cost_source_by_product"][p1.id], "lot_allocation_weight")
        self.assertEqual(maps["lot_weighted_unit_cost_source_by_product"][p2.id], "lot_allocation_weight")

        exceptions = self.repo.report_accounting_exception_rows(
            start_dt=datetime(2026, 4, 2),
            end_dt=datetime(2026, 4, 3),
        )
        self.assertFalse(
            any(
                row.get("exception_type") == "lot_equal_fallback_review_needed"
                and int(row.get("entity_id") or 0) == int(lot.id)
                for row in exceptions
            )
        )

    def test_repair_equal_quantity_fallback_rejects_expected_quantity_lot(self) -> None:
        p1 = self.repo.create_product(
            sku="GS-MIXED-EQUAL-EXPECTED-1",
            title="Mixed Equal Expected 1",
            category="coin",
            description="",
            metal_type="gold",
            weight_oz=None,
            acquisition_cost=None,
            current_quantity=1,
            acquired_at=datetime(2026, 4, 3, 8, 0, 0),
        )
        p2 = self.repo.create_product(
            sku="GS-MIXED-EQUAL-EXPECTED-2",
            title="Mixed Equal Expected 2",
            category="bullion",
            description="",
            metal_type="silver",
            weight_oz=None,
            acquisition_cost=None,
            current_quantity=1,
            acquired_at=datetime(2026, 4, 3, 8, 0, 0),
        )
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-MIXED-EQUAL-EXPECTED",
            vendor="Dealer",
            purchase_date=datetime(2026, 4, 3, 7, 0, 0),
            total_cost=Decimal("100.00"),
            expected_total_quantity=10,
        )
        self.repo.assign_product_to_lot(
            product_id=p1.id,
            lot_id=lot.id,
            quantity_acquired=1,
            unit_cost=None,
            acquired_at=datetime(2026, 4, 3, 8, 0, 0),
        )
        self.repo.assign_product_to_lot(
            product_id=p2.id,
            lot_id=lot.id,
            quantity_acquired=1,
            unit_cost=None,
            acquired_at=datetime(2026, 4, 3, 8, 0, 0),
        )

        with self.assertRaisesRegex(ValueError, "expected_total_quantity"):
            self.repo.repair_purchase_lot_equal_quantity_allocation_weights(lot.id, actor="qa")

    def test_report_lot_assignment_rows_exposes_weighted_allocation_basis(self) -> None:
        p1 = self.repo.create_product(
            sku="GS-LOT-RPT-WEIGHT-1",
            title="Weighted Lot Report 1",
            category="coin",
            description="",
            metal_type="gold",
            weight_oz=None,
            acquisition_cost=None,
            current_quantity=1,
            acquired_at=datetime(2026, 4, 2, 8, 0, 0),
        )
        p2 = self.repo.create_product(
            sku="GS-LOT-RPT-WEIGHT-2",
            title="Weighted Lot Report 2",
            category="bullion",
            description="",
            metal_type="silver",
            weight_oz=None,
            acquisition_cost=None,
            current_quantity=1,
            acquired_at=datetime(2026, 4, 2, 8, 0, 0),
        )
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-RPT-WEIGHTED",
            vendor="Dealer",
            purchase_date=datetime(2026, 4, 2, 7, 0, 0),
            total_cost=Decimal("100.00"),
        )
        self.repo.assign_product_to_lot(
            product_id=p1.id,
            lot_id=lot.id,
            quantity_acquired=1,
            unit_cost=None,
            allocation_weight=Decimal("3"),
            acquired_at=datetime(2026, 4, 2, 8, 0, 0),
        )
        self.repo.assign_product_to_lot(
            product_id=p2.id,
            lot_id=lot.id,
            quantity_acquired=1,
            unit_cost=None,
            allocation_weight=Decimal("1"),
            acquired_at=datetime(2026, 4, 2, 8, 0, 0),
        )

        rows = self.repo.report_lot_assignment_rows(
            start_dt=datetime(2026, 4, 2),
            end_dt=datetime(2026, 4, 3),
        )
        by_sku = {str(row.get("sku") or ""): row for row in rows}

        self.assertEqual(by_sku[p1.sku]["cost_source"], "lot_allocation_weight")
        self.assertAlmostEqual(float(by_sku[p1.sku]["resolved_landed_unit_cost"] or 0.0), 75.0)
        self.assertAlmostEqual(float(by_sku[p1.sku]["resolved_landed_total_cost"] or 0.0), 75.0)
        self.assertEqual(by_sku[p2.sku]["cost_source"], "lot_allocation_weight")
        self.assertAlmostEqual(float(by_sku[p2.sku]["resolved_landed_unit_cost"] or 0.0), 25.0)
        self.assertAlmostEqual(float(by_sku[p2.sku]["lot_landed_total"] or 0.0), 100.0)

    def test_update_product_lot_assignment_reprices_mixed_lot_allocation(self) -> None:
        p1 = self.repo.create_product(
            sku="GS-MIXED-LOT-EDIT-1",
            title="Mixed Lot Edit 1",
            category="coin",
            description="",
            metal_type="gold",
            weight_oz=None,
            acquisition_cost=None,
            current_quantity=1,
            acquired_at=datetime(2026, 3, 31, 8, 0, 0),
        )
        p2 = self.repo.create_product(
            sku="GS-MIXED-LOT-EDIT-2",
            title="Mixed Lot Edit 2",
            category="coin",
            description="",
            metal_type="silver",
            weight_oz=None,
            acquisition_cost=None,
            current_quantity=1,
            acquired_at=datetime(2026, 3, 31, 8, 0, 0),
        )
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-MIXED-EDIT-1",
            vendor="Dealer",
            purchase_date=datetime(2026, 3, 31, 7, 0, 0),
            total_cost=Decimal("100.00"),
        )
        a1 = self.repo.assign_product_to_lot(
            product_id=p1.id,
            lot_id=lot.id,
            quantity_acquired=1,
            unit_cost=None,
            allocation_weight=Decimal("1"),
            acquired_at=datetime(2026, 3, 31, 8, 0, 0),
        )
        a2 = self.repo.assign_product_to_lot(
            product_id=p2.id,
            lot_id=lot.id,
            quantity_acquired=1,
            unit_cost=None,
            allocation_weight=Decimal("1"),
            acquired_at=datetime(2026, 3, 31, 8, 0, 0),
        )

        self.repo.update_product_lot_assignment(
            a1.id,
            {"allocation_weight": Decimal("3")},
            actor="qa-user",
        )

        maps = self.repo.report_sale_unit_cost_maps(
            end_dt=datetime(2026, 4, 1),
            default_unit_cost_by_product={p1.id: 0.0, p2.id: 0.0},
        )
        self.assertAlmostEqual(maps["lot_weighted_unit_cost_by_product"][p1.id], 75.0)
        self.assertAlmostEqual(maps["lot_weighted_unit_cost_by_product"][p2.id], 25.0)

        updated = self.db.get(type(a1), a1.id)
        self.assertEqual(updated.allocation_weight, Decimal("3"))
        self.assertEqual(a2.allocation_weight, Decimal("1"))

    def test_product_cost_is_dashboard_and_sale_cogs_fallback_when_acquisition_cost_is_blank(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self.repo.create_product(
            sku="GS-PRODUCT-COST-FALLBACK",
            title="Product Cost Fallback",
            category="bullion",
            description="",
            metal_type="silver",
            weight_oz=None,
            acquisition_cost=None,
            product_cost=Decimal("15.00"),
            current_quantity=3,
            acquired_at=now - timedelta(days=3),
        )
        sale = self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("50.00"),
            fees=Decimal("5.00"),
            shipping_cost=Decimal("4.00"),
            shipping_label_cost=Decimal("3.00"),
            quantity_sold=2,
            product_id=product.id,
            sold_at=now - timedelta(days=1),
        )

        metrics = self.repo.dashboard_metrics()
        self.assertAlmostEqual(metrics["inventory_cost"], 15.0)

        live = self.repo.dashboard_live_metrics(now=now)
        self.assertAlmostEqual(live["sales_30d_est_cogs"], 30.0)
        self.assertAlmostEqual(live["sales_30d_est_profit"], 16.0)

        maps = self.repo.report_sale_unit_cost_maps(
            end_dt=now,
            default_unit_cost_by_product={product.id: 15.0},
        )
        self.assertAlmostEqual(maps["fifo_unit_cost_by_sale"][sale.id], 15.0)

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
        listing = self.repo.create_listing(
            product_id=p1.id,
            marketplace="ebay",
            external_listing_id="",
            listing_title="T1",
            listing_price=Decimal("50.00"),
            listing_status="active",
            quantity_listed=1,
            listed_at=datetime(2026, 3, 20, 10, 0, 0),
        )
        self.repo.review_listing(int(listing.id), decision="approved", actor="reviewer", notes="ok")
        self.repo.update_listing(
            int(listing.id),
            {"listing_status": "active", "review_status": "approved", "reviewed_by": "reviewer"},
            actor="publisher",
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
        self.assertEqual(metrics["net_sales"], 115.0)

    def test_dashboard_metrics_net_sales_uses_linked_normalized_actuals(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        product = self._create_product(sku="GS-MET-NORM-ACTUALS", qty=3)
        order = self.repo.create_order(
            marketplace="ebay",
            external_order_id="DASH-MET-NORM-ACTUALS",
            order_status="paid",
            sold_at=now,
            fees=Decimal("0.00"),
            shipping_cost=Decimal("5.00"),
            items=[{"product_id": product.id, "listing_id": None, "quantity": 1, "unit_price": Decimal("100.00")}],
            actor="qa-user",
        )
        self.repo.create_sale(
            marketplace="ebay",
            sold_price=Decimal("100.00"),
            fees=Decimal("10.00"),
            shipping_cost=Decimal("5.00"),
            shipping_label_cost=Decimal("9.00"),
            quantity_sold=1,
            product_id=product.id,
            order_id=order.id,
            external_order_id=order.external_order_id,
            sold_at=now,
        )
        self.repo.replace_order_finance_entries(
            order.id,
            [
                {"entry_kind": "marketplace_fee", "fee_type": "FINAL_VALUE_FEE", "amount": Decimal("7.50")},
                {"entry_kind": "shipping_label", "fee_type": "SHIPPING_LABEL", "amount": Decimal("4.25")},
            ],
            actor="qa-user",
        )

        metrics = self.repo.dashboard_metrics()

        self.assertEqual(metrics["gross_sales"], 100.0)
        self.assertAlmostEqual(metrics["net_sales"], 93.25, places=2)

    def test_dashboard_metrics_counts_only_active_listings(self) -> None:
        p1 = self._create_product(sku="GS-MET-ACTIVE", qty=2)
        active_listing = self.repo.create_listing(
            product_id=p1.id,
            marketplace="ebay",
            external_listing_id="",
            listing_title="Active Listing",
            listing_price=Decimal("20.00"),
            listing_status="active",
            quantity_listed=1,
            listed_at=datetime(2026, 3, 22, 10, 0, 0),
        )
        self.repo.review_listing(int(active_listing.id), decision="approved", actor="reviewer", notes="ok")
        self.repo.update_listing(
            int(active_listing.id),
            {"listing_status": "active", "review_status": "approved", "reviewed_by": "reviewer"},
            actor="publisher",
        )
        self.repo.create_listing(
            product_id=p1.id,
            marketplace="ebay",
            external_listing_id="",
            listing_title="Draft Listing",
            listing_price=Decimal("21.00"),
            listing_status="draft",
            quantity_listed=1,
            listed_at=datetime(2026, 3, 22, 11, 0, 0),
        )
        self.repo.create_listing(
            product_id=p1.id,
            marketplace="ebay",
            external_listing_id="",
            listing_title="Ended Listing",
            listing_price=Decimal("22.00"),
            listing_status="ended",
            quantity_listed=1,
            listed_at=datetime(2026, 3, 22, 12, 0, 0),
        )
        metrics = self.repo.dashboard_metrics()
        self.assertEqual(metrics["listing_count"], 1)

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

    def test_record_product_repurchase_updates_landed_components_and_assignment_allocations(self) -> None:
        product = self._create_product(sku="GS-REP-003", qty=10)
        self.repo.update_product(
            product.id,
            {
                "acquisition_tax_paid": Decimal("1.00"),
                "acquisition_shipping_paid": Decimal("0.50"),
                "acquisition_handling_paid": Decimal("0.25"),
                "product_cost": Decimal("25.00"),
            },
            actor="qa-user",
        )
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-REP-2",
            vendor="Rep Vendor",
            purchase_date=datetime(2026, 3, 2, 8, 0, 0),
            total_cost=Decimal("500.00"),
            notes="restock lot 2",
        )
        updated = self.repo.record_product_repurchase(
            product_id=product.id,
            quantity_acquired=5,
            unit_cost=Decimal("35.00"),
            unit_tax_paid=Decimal("2.00"),
            unit_shipping_paid=Decimal("1.50"),
            unit_handling_paid=Decimal("0.50"),
            unit_product_cost=Decimal("35.00"),
            lot_id=lot.id,
            actor="qa-user",
        )
        self.assertEqual(updated.current_quantity, 15)
        self.assertAlmostEqual(float(updated.acquisition_tax_paid or 0), 1.33, places=2)
        self.assertAlmostEqual(float(updated.acquisition_shipping_paid or 0), 0.83, places=2)
        self.assertAlmostEqual(float(updated.acquisition_handling_paid or 0), 0.33, places=2)
        assignments = [a for a in self.repo.list_product_lot_assignments() if a.product_id == product.id and a.lot_id == lot.id]
        self.assertTrue(assignments)
        latest = sorted(assignments, key=lambda a: int(a.id or 0))[-1]
        self.assertEqual(float(latest.allocated_tax_paid or 0), 10.0)
        self.assertEqual(float(latest.allocated_shipping_paid or 0), 7.5)
        self.assertEqual(float(latest.allocated_handling_paid or 0), 2.5)

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

    def test_archive_product_requires_force_when_active_listing_exists(self) -> None:
        product = self._create_product(sku="GS-UPD-ARCH-1", qty=3)
        listing = self.repo.create_listing(
            product_id=product.id,
            marketplace="ebay",
            listing_title="Active Product Listing",
            listing_price=Decimal("10.00"),
            quantity_listed=1,
        )
        self.repo.review_listing(listing.id, decision="approved", actor="reviewer1", notes="ok")
        self.repo.update_listing(
            listing.id,
            {"listing_status": "active", "review_status": "approved", "reviewed_by": "reviewer1"},
            actor="publisher1",
        )
        with self.assertRaisesRegex(ValueError, "Cannot archive product with active listings"):
            self.repo.archive_product(product.id, actor="qa-user", force=False)

        archived = self.repo.archive_product(product.id, actor="qa-user", force=True)
        self.assertEqual(str(archived.status or ""), "archived")
        restored = self.repo.restore_product(product.id, actor="qa-user")
        self.assertEqual(str(restored.status or ""), "active")

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

    def test_archive_and_restore_purchase_lot_lifecycle(self) -> None:
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-ARCH-1",
            vendor="Vendor A",
            purchase_date=datetime(2026, 3, 1, 8, 0, 0),
            total_cost=Decimal("100.00"),
            notes="legacy notes",
        )
        archived = self.repo.archive_purchase_lot(lot.id, actor="qa-user", reason="duplicate lot")
        archived_notes = json.loads(str(archived.notes or "{}"))
        self.assertTrue(bool(archived_notes.get("lifecycle", {}).get("archived")))
        self.assertEqual(str(archived_notes.get("lifecycle", {}).get("archive_reason") or ""), "duplicate lot")

        restored = self.repo.restore_purchase_lot(lot.id, actor="qa-user")
        restored_notes = json.loads(str(restored.notes or "{}"))
        self.assertFalse(bool(restored_notes.get("lifecycle", {}).get("archived")))

    def test_archive_purchase_lot_requires_force_when_dependencies_exist(self) -> None:
        lot = self.repo.create_purchase_lot(
            lot_code="LOT-ARCH-BLOCK-1",
            vendor="Vendor B",
            purchase_date=datetime(2026, 3, 1, 8, 0, 0),
            total_cost=Decimal("50.00"),
            notes="",
        )
        product = self._create_product(sku="GS-LOT-ARCH-BLOCK-1", qty=1)
        self.repo.assign_product_to_lot(
            product_id=product.id,
            lot_id=lot.id,
            quantity_acquired=1,
            unit_cost=Decimal("10.00"),
            acquired_at=datetime(2026, 3, 1, 9, 0, 0),
        )
        blockers = self.repo.get_purchase_lot_archive_blockers(lot.id)
        self.assertGreaterEqual(int(blockers.get("product_assignments", 0)), 1)
        self.assertGreaterEqual(int(blockers.get("active_products", 0)), 1)
        with self.assertRaisesRegex(ValueError, "Cannot archive lot with linked records"):
            self.repo.archive_purchase_lot(lot.id, actor="qa-user", reason="guardrail test", force=False)
        archived = self.repo.archive_purchase_lot(
            lot.id,
            actor="qa-user",
            reason="guardrail override",
            force=True,
        )
        archived_notes = json.loads(str(archived.notes or "{}"))
        lifecycle = archived_notes.get("lifecycle", {}) if isinstance(archived_notes, dict) else {}
        self.assertTrue(bool(lifecycle.get("archive_forced")))
        blocker_snapshot = lifecycle.get("archive_blockers", {}) if isinstance(lifecycle.get("archive_blockers"), dict) else {}
        self.assertGreaterEqual(int(blocker_snapshot.get("product_assignments", 0)), 1)

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

    def test_create_and_update_product_normalize_oversized_metal_type(self) -> None:
        long_metal_type = (
            "Copper-Nickel (Golden Dollar), 99.93% Silver (American Eagle Silver Dollar)"
        )
        product = self.repo.create_product(
            sku="GS-LONG-METAL-1",
            title="Millennium Coinage and Currency Set",
            category="collectibles",
            description="US Mint set with certificate.",
            metal_type=long_metal_type,
            weight_oz=None,
            acquisition_cost=None,
            current_quantity=1,
        )

        self.assertLessEqual(len(product.metal_type), 64)
        self.assertEqual(product.metal_type, "copper-nickel, silver")
        self.assertIn(f"Metal composition: {long_metal_type}", product.description)

        updated_long_metal_type = (
            "99.9% Gold plated copper alloy with nickel clad presentation token detail"
        )
        updated = self.repo.update_product(
            int(product.id),
            {"metal_type": updated_long_metal_type},
            actor="qa-user",
        )

        self.assertLessEqual(len(updated.metal_type), 64)
        self.assertEqual(updated.metal_type, "gold, copper, nickel, clad")
        self.assertIn(f"Metal composition: {updated_long_metal_type}", updated.description)

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
