import importlib.util
import json
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace


def _bootstrap_views_package() -> None:
    if "boto3" not in sys.modules:
        fake_boto3 = types.ModuleType("boto3")
        fake_boto3.session = types.SimpleNamespace(Session=lambda *args, **kwargs: None)
        sys.modules["boto3"] = fake_boto3
    if "botocore" not in sys.modules:
        sys.modules["botocore"] = types.ModuleType("botocore")
    if "botocore.config" not in sys.modules:
        fake_botocore_config = types.ModuleType("botocore.config")
        fake_botocore_config.Config = lambda *args, **kwargs: None
        sys.modules["botocore.config"] = fake_botocore_config
    if "botocore.exceptions" not in sys.modules:
        fake_botocore_exceptions = types.ModuleType("botocore.exceptions")
        fake_botocore_exceptions.BotoCoreError = Exception
        fake_botocore_exceptions.ClientError = Exception
        sys.modules["botocore.exceptions"] = fake_botocore_exceptions

    if "app.components.views" not in sys.modules:
        pkg = types.ModuleType("app.components.views")
        pkg.__path__ = []
        sys.modules["app.components.views"] = pkg

    root = Path(__file__).resolve().parents[1]
    for name in ("shared", "workspace_shell"):
        full = f"app.components.views.{name}"
        if full in sys.modules:
            continue
        path = root / "app" / "components" / "views" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(full, path)
        mod = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(mod)
        sys.modules[full] = mod


def _load_module():
    _bootstrap_views_package()
    root = Path(__file__).resolve().parents[1]
    path = root / "app" / "components" / "views" / "reports.py"
    spec = importlib.util.spec_from_file_location("test_reports_module", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


reports = _load_module()


class ReportsHelpersTests(unittest.TestCase):
    def test_default_tax_marketplace_scope_excludes_facilitator_channels(self):
        scoped = reports._default_tax_marketplace_scope(
            sales_marketplace_options=["ebay", "local", "shopify"],
            facilitator_channels={"ebay"},
        )
        self.assertEqual(scoped, ["local", "shopify"])

    def test_default_tax_marketplace_scope_falls_back_when_all_are_facilitators(self):
        scoped = reports._default_tax_marketplace_scope(
            sales_marketplace_options=["ebay"],
            facilitator_channels={"ebay"},
        )
        self.assertEqual(scoped, ["ebay"])

    def test_bounded_dataframe_preview_mode(self):
        df = reports.pd.DataFrame({"a": list(range(10))})
        bounded, truncated = reports._bounded_dataframe(
            df,
            render_full_tables=False,
            preview_row_limit=3,
        )
        self.assertTrue(truncated)
        self.assertEqual(len(bounded), 3)
        self.assertEqual(list(bounded["a"]), [0, 1, 2])

    def test_bounded_dataframe_full_mode(self):
        df = reports.pd.DataFrame({"a": [1, 2, 3]})
        bounded, truncated = reports._bounded_dataframe(
            df,
            render_full_tables=True,
            preview_row_limit=1,
        )
        self.assertFalse(truncated)
        self.assertEqual(len(bounded), 3)

    def test_safe_float_and_csv_set_and_presets(self):
        self.assertEqual(reports._safe_float(None), 0.0)
        self.assertEqual(reports._safe_float("bad"), 0.0)
        self.assertAlmostEqual(reports._safe_float("12.5"), 12.5)

        self.assertEqual(reports._parse_csv_set(" a, B ,,c "), {"a", "b", "c"})

        presets = reports._tax_report_presets(
            default_jurisdiction="Golden, Colorado",
            default_tax_rate_percent=8.9,
            default_shipping_taxable=True,
        )
        self.assertIn("Golden Local Retail", presets)
        self.assertEqual(presets["Marketplace Shipped"]["shipping_taxable"], False)

    def test_build_fifo_unit_cost_map(self):
        assignments = [
            SimpleNamespace(id=1, product_id=1, acquired_at=datetime(2026, 1, 1), quantity_acquired=5, unit_cost=2.0, allocated_cost=None),
            SimpleNamespace(id=2, product_id=1, acquired_at=datetime(2026, 1, 2), quantity_acquired=3, unit_cost=0, allocated_cost=9.0),
            SimpleNamespace(id=3, product_id=None, acquired_at=datetime(2026, 1, 1), quantity_acquired=2, unit_cost=1.0, allocated_cost=None),
        ]
        sales = [
            SimpleNamespace(id=11, product_id=1, sold_at=datetime(2026, 1, 3), quantity_sold=4),
            SimpleNamespace(id=12, product_id=1, sold_at=datetime(2026, 1, 4), quantity_sold=4),
            SimpleNamespace(id=13, product_id=2, sold_at=datetime(2026, 1, 5), quantity_sold=2),
            SimpleNamespace(id=14, product_id=None, sold_at=datetime(2026, 1, 6), quantity_sold=1),
        ]
        out = reports._build_fifo_unit_cost_map(sales, assignments, {2: 5.0})
        self.assertAlmostEqual(out[11], 2.0)
        self.assertAlmostEqual(out[12], 2.75)
        self.assertAlmostEqual(out[13], 5.0)
        self.assertAlmostEqual(out[14], 0.0)

    def test_build_lot_weighted_unit_cost_map(self):
        assignments = [
            SimpleNamespace(product_id=1, quantity_acquired=2, unit_cost=4.0, allocated_cost=None),
            SimpleNamespace(product_id=1, quantity_acquired=1, unit_cost=0.0, allocated_cost=9.0),
            SimpleNamespace(product_id=2, quantity_acquired=0, unit_cost=5.0, allocated_cost=None),
        ]
        out = reports._build_lot_weighted_unit_cost_map(assignments, {2: 3.5, 3: -1})
        self.assertAlmostEqual(out[1], (2 * 4 + 1 * 9) / 3)
        self.assertAlmostEqual(out[2], 3.5)
        self.assertAlmostEqual(out[3], 0.0)

    def test_build_inventory_cycle_rows(self):
        products = [
            SimpleNamespace(id=1, sku="SKU1", title="Coin A"),
            SimpleNamespace(id=2, sku="SKU2", title="Coin B"),
        ]
        movements = [
            SimpleNamespace(id=1, product_id=1, occurred_at=datetime(2026, 1, 1), quantity_before=0, quantity_after=2, quantity_delta=2, unit_cost=10.0),
            SimpleNamespace(id=2, product_id=1, occurred_at=datetime(2026, 1, 2), quantity_before=2, quantity_after=0, quantity_delta=-2, unit_cost=None),
            SimpleNamespace(id=3, product_id=2, occurred_at=datetime(2026, 1, 3), quantity_before=0, quantity_after=3, quantity_delta=3, unit_cost=2.0),
        ]
        sales = [
            SimpleNamespace(id=11, product_id=1, sold_at=datetime(2026, 1, 1, 12), quantity_sold=1, sold_price=25.0, fees=1.0, shipping_cost=2.0),
            SimpleNamespace(id=12, product_id=2, sold_at=datetime(2026, 1, 4), quantity_sold=1, sold_price=10.0, fees=0.5, shipping_cost=1.0),
        ]
        rows = reports._build_inventory_cycle_rows(products, movements, sales)
        self.assertEqual(len(rows), 2)
        closed = next(r for r in rows if r["sku"] == "SKU1")
        self.assertEqual(closed["cycle_status"], "closed")
        self.assertEqual(closed["sale_count"], 1)
        self.assertAlmostEqual(closed["net_sales"], 22.0)
        self.assertAlmostEqual(closed["estimated_margin_vs_known_cost"], 2.0)

        open_row = next(r for r in rows if r["sku"] == "SKU2")
        self.assertEqual(open_row["cycle_status"], "open")
        self.assertEqual(open_row["qty_in"], 3)

    def test_build_inventory_cycle_summary_rows(self):
        rows = [
            {
                "product_id": 1,
                "sku": "SKU1",
                "product_title": "Coin A",
                "cycle_number": 1,
                "cycle_status": "closed",
                "cycle_start": "2026-01-01T00:00:00",
                "cycle_end": "2026-01-03T00:00:00",
                "qty_in": 2,
                "qty_out_movements": 2,
                "qty_sold_sales": 2,
                "sale_count": 1,
                "net_sales": 40.0,
                "acquisition_cost_known": 20.0,
                "estimated_margin_vs_known_cost": 20.0,
            },
            {
                "product_id": 1,
                "sku": "SKU1",
                "product_title": "Coin A",
                "cycle_number": 2,
                "cycle_status": "open",
                "cycle_start": "2026-01-05T00:00:00",
                "cycle_end": "",
                "qty_in": 1,
                "qty_out_movements": 0,
                "qty_sold_sales": 0,
                "sale_count": 0,
                "net_sales": 0.0,
                "acquisition_cost_known": 8.0,
                "estimated_margin_vs_known_cost": -8.0,
            },
        ]
        summary = reports._build_inventory_cycle_summary_rows(rows)
        self.assertEqual(len(summary), 1)
        row = summary[0]
        self.assertEqual(row["sku"], "SKU1")
        self.assertEqual(row["cycle_count"], 2)
        self.assertEqual(row["closed_cycle_count"], 1)
        self.assertEqual(row["open_cycle_count"], 1)
        self.assertEqual(row["qty_in_total"], 3)
        self.assertEqual(row["qty_sold_total"], 2)
        self.assertAlmostEqual(row["net_sales_total"], 40.0)
        self.assertAlmostEqual(row["acquisition_cost_known_total"], 28.0)
        self.assertAlmostEqual(row["estimated_margin_vs_known_cost_total"], 12.0)
        self.assertAlmostEqual(row["avg_closed_cycle_days"], 2.0)

    def test_build_rebuy_cost_trend_rows(self):
        products = [SimpleNamespace(id=1, sku="SKU1", title="Coin A")]
        assignments = [
            SimpleNamespace(id=7, product_id=1, acquired_at=datetime(2026, 1, 1), quantity_acquired=2, unit_cost=5.0),
        ]
        movements = [
            SimpleNamespace(id=8, product_id=1, occurred_at=datetime(2026, 1, 1), movement_type="repurchase_in", quantity_delta=2, unit_cost=5.0),
            SimpleNamespace(id=9, product_id=1, occurred_at=datetime(2026, 1, 2), movement_type="repurchase_in", quantity_delta=1, unit_cost=7.0),
            SimpleNamespace(id=10, product_id=1, occurred_at=datetime(2026, 1, 3), movement_type="sale", quantity_delta=-1, unit_cost=1.0),
        ]
        rows = reports._build_rebuy_cost_trend_rows(products, assignments, movements)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["event_type"], "lot_assignment")
        self.assertEqual(rows[1]["event_type"], "repurchase_in")
        self.assertAlmostEqual(rows[-1]["weighted_unit_cost"], (2 * 5 + 1 * 7) / 3, places=4)

    def test_build_economics_intelligence_drilldowns(self):
        economics_df = reports.pd.DataFrame(
            [
                {
                    "sale_id": 1,
                    "sold_at": "2026-04-01T00:00:00",
                    "marketplace": "ebay",
                    "sku": "SKU1",
                    "product_title": "Coin A",
                    "sold_price": 100.0,
                    "estimate_available": True,
                    "estimated_fee_alloc": 8.0,
                    "expected_shipping_alloc": 5.0,
                    "estimated_net_before_cogs": 87.0,
                    "actual_fee_alloc": 11.0,
                    "actual_shipping_alloc": 6.0,
                    "actual_net_before_cogs": 83.0,
                    "fee_variance_actual_minus_estimated": 3.0,
                    "net_variance_actual_minus_estimated": -4.0,
                },
                {
                    "sale_id": 2,
                    "sold_at": "2026-04-02T00:00:00",
                    "marketplace": "ebay",
                    "sku": "SKU1",
                    "product_title": "Coin A",
                    "sold_price": 80.0,
                    "estimate_available": True,
                    "estimated_fee_alloc": 7.0,
                    "expected_shipping_alloc": 4.0,
                    "estimated_net_before_cogs": 69.0,
                    "actual_fee_alloc": 10.0,
                    "actual_shipping_alloc": 4.0,
                    "actual_net_before_cogs": 66.0,
                    "fee_variance_actual_minus_estimated": 3.0,
                    "net_variance_actual_minus_estimated": -3.0,
                },
                {
                    "sale_id": 3,
                    "sold_at": "2026-04-03T00:00:00",
                    "marketplace": "local",
                    "sku": "SKU2",
                    "product_title": "Coin B",
                    "sold_price": 50.0,
                    "estimate_available": False,
                    "estimated_fee_alloc": None,
                    "expected_shipping_alloc": 0.0,
                    "estimated_net_before_cogs": None,
                    "actual_fee_alloc": 0.0,
                    "actual_shipping_alloc": 0.0,
                    "actual_net_before_cogs": 50.0,
                    "fee_variance_actual_minus_estimated": None,
                    "net_variance_actual_minus_estimated": None,
                },
            ]
        )
        out = reports._build_economics_intelligence_drilldowns(
            economics_df,
            min_margin_alert_pct=40.0,
            max_fee_variance_alert_usd=2.5,
            min_group_sales_for_alert=2,
        )
        by_sku = out["by_sku"]
        by_marketplace = out["by_marketplace"]
        alerts = out["alerts"]

        self.assertEqual(len(by_sku), 2)
        sku1 = by_sku[by_sku["sku"] == "SKU1"].iloc[0]
        self.assertEqual(int(sku1["sales_count"]), 2)
        self.assertAlmostEqual(float(sku1["avg_abs_fee_variance"]), 3.0, places=2)
        self.assertTrue(bool(sku1["alert_fee_variance_high"]))
        self.assertTrue(bool(sku1["alert_any"]))

        self.assertEqual(len(by_marketplace), 2)
        ebay_row = by_marketplace[by_marketplace["marketplace"] == "ebay"].iloc[0]
        self.assertTrue(bool(ebay_row["alert_any"]))

        self.assertEqual(int(len(alerts)), 2)
        self.assertTrue(all(bool(v) for v in alerts["alert_any"].tolist()))

    def test_build_listing_review_activity_rows(self):
        history_ok = {
            "review_history": [
                {
                    "decision": "approved",
                    "actor": "admin",
                    "reviewed_at": "2026-01-10T12:00:00Z",
                    "notes": "looks good",
                },
                "bad-item",
            ]
        }
        listing = SimpleNamespace(
            id=1,
            marketplace="ebay",
            product=SimpleNamespace(sku="SKU1"),
            listing_title="Title",
            marketplace_details=json.dumps(history_ok),
        )
        listing_bad = SimpleNamespace(
            id=2,
            marketplace="ebay",
            product=None,
            listing_title="Bad",
            marketplace_details="{",
        )
        rows = reports._build_listing_review_activity_rows(
            [listing, listing_bad],
            start_dt=datetime(2026, 1, 1),
            end_dt=datetime(2026, 1, 31),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["review_decision"], "approved")

    def test_build_listing_format_outcome_rows(self):
        published = SimpleNamespace(
            id=1,
            listed_at=datetime(2026, 1, 10),
            marketplace="ebay",
            product=SimpleNamespace(sku="SKU1"),
            listing_title="Published",
            review_status="approved",
            listing_status="active",
            external_listing_id="abc",
            marketplace_details=json.dumps({"ebay_publish": {"format": "AUCTION", "listing_duration": "DAYS_7", "history": [{"status": "published"}]}}),
        )
        failed = SimpleNamespace(
            id=2,
            listed_at=datetime(2026, 1, 11),
            marketplace="ebay",
            product=None,
            listing_title="Failed",
            review_status="pending",
            listing_status="draft",
            external_listing_id="",
            marketplace_details=json.dumps({"ebay_publish": {"history": [{"status": "failed", "error": "bad req"}]}}),
        )
        attempted = SimpleNamespace(
            id=3,
            listed_at=datetime(2026, 1, 12),
            marketplace="ebay",
            product=None,
            listing_title="Attempted",
            review_status="pending",
            listing_status="draft",
            external_listing_id="",
            marketplace_details=json.dumps({"ebay_publish": {"history": [{"status": "queued"}]}}),
        )
        untouched = SimpleNamespace(
            id=4,
            listed_at=datetime(2026, 1, 13),
            marketplace="ebay",
            product=None,
            listing_title="Untouched",
            review_status="pending",
            listing_status="draft",
            external_listing_id="",
            marketplace_details="",
        )
        rows = reports._build_listing_format_outcome_rows(
            [published, failed, attempted, untouched],
            start_dt=datetime(2026, 1, 1),
            end_dt=datetime(2026, 1, 31),
        )
        by_id = {r["listing_id"]: r for r in rows}
        self.assertEqual(by_id[1]["publish_outcome"], "published")
        self.assertEqual(by_id[2]["publish_outcome"], "publish_error")
        self.assertEqual(by_id[3]["publish_outcome"], "attempted_no_publish")
        self.assertEqual(by_id[4]["publish_outcome"], "not_attempted")

    def test_build_ebay_marketplace_fee_rows(self):
        orders = [
            SimpleNamespace(
                id=2,
                marketplace="ebay",
                external_order_id="23-14477-17302",
                sold_at=datetime(2026, 4, 13, 5, 54, 42),
                marketplace_payload_json=json.dumps(
                    {
                        "lineItems": [
                            {
                                "lineItemId": "10080248303323",
                                "legacyItemId": "137217809542",
                                "sku": "DOC-44-0408",
                                "title": "Statue of Liberty 5 oz .999 Copper Colorized Bar Collectible",
                            }
                        ],
                        "_finance_transactions": [
                            {
                                "transactionId": "23-14477-17302",
                                "transactionType": "SALE",
                                "transactionDate": "2026-04-13T05:54:42.891Z",
                                "transactionStatus": "FUNDS_ON_HOLD",
                                "orderLineItems": [
                                    {
                                        "lineItemId": "10080248303323",
                                        "marketplaceFees": [
                                            {
                                                "feeType": "INTERNATIONAL_FEE",
                                                "amount": {"value": "2.34", "currency": "USD"},
                                                "feeMemo": "Intl",
                                            },
                                            {
                                                "feeType": "FINAL_VALUE_FEE",
                                                "amount": {"value": "19.31", "currency": "USD"},
                                            },
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ),
            )
        ]

        repo = SimpleNamespace(db=SimpleNamespace(scalars=lambda *_args, **_kwargs: SimpleNamespace(all=lambda: [])))
        rows = reports._build_ebay_marketplace_fee_rows(repo, orders)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["external_order_id"], "23-14477-17302")
        self.assertEqual(rows[0]["line_item_id"], "10080248303323")
        self.assertEqual(rows[0]["sku"], "DOC-44-0408")
        self.assertEqual(rows[0]["source"], "finance_transactions_orderLineItems")
        self.assertTrue({r["fee_type"] for r in rows}.issuperset({"INTERNATIONAL_FEE", "FINAL_VALUE_FEE"}))

    def test_build_ebay_marketplace_fee_rows_prefers_normalized_entries(self):
        normalized_rows = [
            SimpleNamespace(
                order_id=2,
                line_item_id="L1",
                sku="DOC-1",
                legacy_item_id="123",
                fee_type="FINAL_VALUE_FEE",
                amount=19.31,
                currency="USD",
                memo="memo",
                transaction_id="T1",
                transaction_date=datetime(2026, 4, 13, 5, 54, 42),
                transaction_type="SALE",
                transaction_status="FUNDS_ON_HOLD",
                source="finance_transactions_orderLineItems",
            )
        ]
        repo = SimpleNamespace(
            db=SimpleNamespace(
                scalars=lambda *_args, **_kwargs: SimpleNamespace(all=lambda: normalized_rows),
            )
        )
        orders = [
            SimpleNamespace(
                id=2,
                marketplace="ebay",
                external_order_id="23-14477-17302",
                sold_at=datetime(2026, 4, 13, 5, 54, 42),
                marketplace_payload_json="{}",
            )
        ]

        rows = reports._build_ebay_marketplace_fee_rows(repo, orders)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["line_item_id"], "L1")
        self.assertEqual(rows[0]["fee_type"], "FINAL_VALUE_FEE")

    def test_build_fee_source_priority_summary(self):
        df = reports.pd.DataFrame(
            [
                {"sale_id": 1, "actual_fee_source": "sale_fees_field", "actual_fee": 3.0},
                {
                    "sale_id": 2,
                    "actual_fee_source": "normalized_order_finance_entries_marketplace_fee_sum",
                    "actual_fee": 2.0,
                },
                {
                    "sale_id": 3,
                    "actual_fee_source": "order_fee_breakdown_total_marketplace_fee",
                    "actual_fee": 2.5,
                },
            ]
        )
        summary = reports._build_fee_source_priority_summary(df)
        self.assertEqual(len(summary), 3)
        self.assertEqual(summary.iloc[0]["actual_fee_source"], "normalized_order_finance_entries_marketplace_fee_sum")
        self.assertEqual(summary.iloc[1]["actual_fee_source"], "order_fee_breakdown_total_marketplace_fee")
        self.assertEqual(summary.iloc[2]["actual_fee_source"], "sale_fees_field")

    def test_build_fee_source_priority_trend(self):
        df = reports.pd.DataFrame(
            [
                {
                    "sale_id": 1,
                    "actual_fee_source": "normalized_order_finance_entries_marketplace_fee_sum",
                    "actual_fee": 2.0,
                    "sold_at": "2026-04-10T10:00:00",
                },
                {
                    "sale_id": 2,
                    "actual_fee_source": "sale_fees_field",
                    "actual_fee": 3.0,
                    "sold_at": "2026-04-10T11:00:00",
                },
                {
                    "sale_id": 3,
                    "actual_fee_source": "order_fee_breakdown_total_marketplace_fee",
                    "actual_fee": 1.5,
                    "sold_at": "2026-04-11T09:00:00",
                },
            ]
        )
        trend = reports._build_fee_source_priority_trend(df)
        self.assertFalse(trend.empty)
        granularities = set(trend["bucket_granularity"].tolist())
        self.assertIn("daily", granularities)
        self.assertIn("weekly", granularities)

    def test_build_normalized_source_weekly_coverage(self):
        trend_df = reports.pd.DataFrame(
            [
                {
                    "bucket_granularity": "weekly",
                    "bucket_date": "2026-04-07",
                    "actual_fee_source": "normalized_order_finance_entries_marketplace_fee_sum",
                    "sales_count": 2,
                },
                {
                    "bucket_granularity": "weekly",
                    "bucket_date": "2026-04-07",
                    "actual_fee_source": "sale_fees_field",
                    "sales_count": 2,
                },
                {
                    "bucket_granularity": "weekly",
                    "bucket_date": "2026-04-14",
                    "actual_fee_source": "normalized_order_finance_entries_marketplace_fee_sum",
                    "sales_count": 3,
                },
            ]
        )
        coverage = reports._build_normalized_source_weekly_coverage(trend_df)
        self.assertEqual(len(coverage), 2)
        row_a = coverage.iloc[0]
        row_b = coverage.iloc[1]
        self.assertEqual(str(row_a["bucket_date"]), "2026-04-07")
        self.assertAlmostEqual(float(row_a["normalized_coverage_pct"]), 50.0, places=2)
        self.assertEqual(str(row_b["bucket_date"]), "2026-04-14")
        self.assertAlmostEqual(float(row_b["normalized_coverage_pct"]), 100.0, places=2)

    def test_build_weekly_fee_source_count_chart_data(self):
        trend_df = reports.pd.DataFrame(
            [
                {
                    "bucket_granularity": "weekly",
                    "bucket_date": "2026-04-07",
                    "actual_fee_source": "normalized_order_finance_entries_marketplace_fee_sum",
                    "sales_count": 2,
                },
                {
                    "bucket_granularity": "weekly",
                    "bucket_date": "2026-04-07",
                    "actual_fee_source": "sale_fees_field",
                    "sales_count": 1,
                },
                {
                    "bucket_granularity": "weekly",
                    "bucket_date": "2026-04-14",
                    "actual_fee_source": "order_fee_breakdown_total_marketplace_fee",
                    "sales_count": 3,
                },
            ]
        )
        chart = reports._build_weekly_fee_source_count_chart_data(trend_df)
        self.assertEqual(len(chart), 2)
        self.assertTrue({"week_start", "normalized_source", "notes_fallback", "sale_field_fallback"}.issubset(chart.columns))
        first = chart.iloc[0]
        second = chart.iloc[1]
        self.assertEqual(str(first["week_start"]), "2026-04-07")
        self.assertEqual(int(first["normalized_source"]), 2)
        self.assertEqual(int(first["sale_field_fallback"]), 1)
        self.assertEqual(int(second["notes_fallback"]), 3)


if __name__ == "__main__":
    unittest.main()
