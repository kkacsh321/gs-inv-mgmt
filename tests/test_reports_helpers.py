import importlib.util
import json
import sys
import types
import unittest
from io import BytesIO
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
import zipfile


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
    def test_sale_listing_bundle_summary_counts_inventory_units(self):
        sale = SimpleNamespace(
            quantity_sold=2,
            listing=SimpleNamespace(
                marketplace_details=json.dumps(
                    {
                        "bundle": {
                            "enabled": True,
                            "kind": "mixed_product_bundle",
                            "components": [
                                {"product_id": 1, "quantity_per_listing": 2},
                                {"product_id": 2, "quantity_per_listing": 3},
                            ],
                        }
                    }
                )
            ),
        )

        summary = reports._sale_listing_bundle_summary(sale)

        self.assertTrue(summary["listing_is_bundle"])
        self.assertEqual(summary["listing_bundle_kind"], "mixed_product_bundle")
        self.assertEqual(summary["listing_bundle_component_count"], 2)
        self.assertEqual(summary["listing_bundle_units_per_listing"], 5)
        self.assertEqual(summary["listing_bundle_inventory_units_sold"], 10)

    def test_parse_ai_json_sections_keeps_tax_review_findings_visible(self):
        sections = reports._parse_ai_json_sections(
            json.dumps(
                {
                    "executive_summary": ["Close packet needs review"],
                    "tax_review_findings": ["Tax packet hash matches current evidence"],
                    "next_actions": "Record advisor evidence before final sign-off",
                    "ignored": ["not requested"],
                }
            ),
            ["executive_summary", "tax_review_findings", "next_actions"],
        )

        self.assertEqual(sections["executive_summary"], ["Close packet needs review"])
        self.assertEqual(sections["tax_review_findings"], ["Tax packet hash matches current evidence"])
        self.assertEqual(sections["next_actions"], ["Record advisor evidence before final sign-off"])
        self.assertNotIn("ignored", sections)

    def test_parse_ai_json_sections_accepts_fenced_or_prefaced_json(self):
        fenced = reports._parse_ai_json_sections(
            """```json
{"tax_review_findings":["Advisor evidence missing"]}
```""",
            ["tax_review_findings"],
        )
        prefaced = reports._parse_ai_json_sections(
            'Here is the review:\n{"unsupported_tax_or_legal_items":["Confirm state rules"]}\nEnd.',
            ["unsupported_tax_or_legal_items"],
        )

        self.assertEqual(fenced["tax_review_findings"], ["Advisor evidence missing"])
        self.assertEqual(prefaced["unsupported_tax_or_legal_items"], ["Confirm state rules"])

    def test_stable_json_sha256_is_order_independent(self):
        first = reports._stable_json_sha256({"b": 2, "a": {"x": 1}})
        second = reports._stable_json_sha256({"a": {"x": 1}, "b": 2})

        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)

    def test_log_reports_ai_outcome_records_acceptance_metadata(self):
        calls = []
        repo = SimpleNamespace(log_ai_chat_interaction=lambda **kwargs: calls.append(kwargs))

        reports._log_reports_ai_outcome(
            repo,
            actor="operator",
            review_type="ai_accountant_review",
            outcome="accepted",
            answer_text='{"close_status":"review_needed"}',
            review_metadata={"prompt_hash_sha256": "p" * 64, "data_scope_hash_sha256": "d" * 64},
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["intent"], "ai_accountant_review_outcome")
        self.assertEqual(calls[0]["metadata"]["event_type"], "ai_accountant_review_outcome")
        self.assertEqual(calls[0]["metadata"]["outcome"], "accepted")
        self.assertEqual(len(calls[0]["metadata"]["answer_hash_sha256"]), 64)
        self.assertIn("tax", calls[0]["allowed_domains"])

    def test_log_reports_ai_outcome_preserves_edited_outcome(self):
        calls = []
        repo = SimpleNamespace(log_ai_chat_interaction=lambda **kwargs: calls.append(kwargs))

        reports._log_reports_ai_outcome(
            repo,
            actor="operator",
            review_type="reports_copilot_review",
            outcome="edited",
            answer_text='{"next_actions":["Clarify tax profile"]}',
            review_metadata={},
        )

        self.assertEqual(calls[0]["metadata"]["outcome"], "edited")

    def test_ai_review_outcome_rows_from_audit_logs_extracts_feedback(self):
        row = SimpleNamespace(
            created_at=datetime(2026, 5, 4, 12, 0, 0),
            actor="operator",
            changes_json=json.dumps(
                {
                    "after": {
                        "intent": "ai_accountant_review_outcome",
                        "answer_preview": '{"close_status":"review_needed"}',
                        "metadata": {
                            "event_type": "ai_accountant_review_outcome",
                            "review_type": "ai_accountant_review",
                            "outcome": "edited",
                            "prompt_hash_sha256": "p" * 64,
                            "data_scope_hash_sha256": "d" * 64,
                            "answer_hash_sha256": "a" * 64,
                            "data_scope": {
                                "accounting_close_packet_evidence_hash": "c" * 64,
                                "tax_packet_evidence_hash": "t" * 64,
                            },
                        },
                    }
                }
            ),
        )

        rows = reports._ai_review_outcome_rows_from_audit_logs([row])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["review_type"], "ai_accountant_review")
        self.assertEqual(rows[0]["outcome"], "edited")
        self.assertEqual(rows[0]["answer_hash_sha256"], "a" * 64)
        self.assertEqual(rows[0]["accounting_close_packet_evidence_hash"], "c" * 64)
        self.assertEqual(rows[0]["tax_packet_evidence_hash"], "t" * 64)

    def test_accounting_field_semantics_cover_canonical_cost_sources(self):
        fields = {str(row.get("field") or "") for row in reports.ACCOUNTING_FIELD_SEMANTICS_ROWS}
        self.assertIn("products.acquisition_cost", fields)
        self.assertIn("products.product_cost", fields)
        self.assertIn("purchase_lots.total_cost / tax / shipping / handling", fields)
        self.assertIn("product_lot_assignments.unit_cost / unit_tax / unit_shipping / unit_handling", fields)
        self.assertIn(
            "product_lot_assignments.allocated_cost / allocated_tax / allocated_shipping / allocated_handling",
            fields,
        )
        self.assertIn("product_lot_assignments.allocation_weight", fields)
        self.assertIn("FIFO remaining lot cost", fields)

    def test_lot_fallback_uses_expected_quantity_for_partial_check_in(self):
        assignments = [
            SimpleNamespace(
                lot_id=1,
                quantity_acquired=2,
                unit_cost=None,
                unit_tax_paid=None,
                unit_shipping_paid=None,
                unit_handling_paid=None,
                allocated_cost=None,
                allocated_tax_paid=None,
                allocated_shipping_paid=None,
                allocated_handling_paid=None,
                lot_total_cost=100.0,
                lot_total_tax_paid=None,
                lot_total_shipping_paid=None,
                lot_total_handling_paid=None,
                lot_expected_total_quantity=10,
            )
        ]

        fallback = reports._lot_fallback_unit_costs_by_lot(assignments)

        self.assertEqual(fallback[1], 10.0)

    def test_lot_fallback_uses_allocation_weight_for_mixed_lot_values(self):
        assignments = [
            SimpleNamespace(
                id=11,
                lot_id=1,
                quantity_acquired=1,
                unit_cost=None,
                unit_tax_paid=None,
                unit_shipping_paid=None,
                unit_handling_paid=None,
                allocated_cost=None,
                allocated_tax_paid=None,
                allocated_shipping_paid=None,
                allocated_handling_paid=None,
                allocation_weight=9.0,
                lot_total_cost=100.0,
                lot_total_tax_paid=None,
                lot_total_shipping_paid=None,
                lot_total_handling_paid=None,
            ),
            SimpleNamespace(
                id=12,
                lot_id=1,
                quantity_acquired=1,
                unit_cost=None,
                unit_tax_paid=None,
                unit_shipping_paid=None,
                unit_handling_paid=None,
                allocated_cost=None,
                allocated_tax_paid=None,
                allocated_shipping_paid=None,
                allocated_handling_paid=None,
                allocation_weight=1.0,
                lot_total_cost=100.0,
                lot_total_tax_paid=None,
                lot_total_shipping_paid=None,
                lot_total_handling_paid=None,
            ),
        ]

        lot_fallback, assignment_fallback = reports._lot_fallback_unit_cost_maps(assignments)

        self.assertEqual(lot_fallback, {})
        self.assertEqual(assignment_fallback[11], 90.0)
        self.assertEqual(assignment_fallback[12], 10.0)

    def test_default_tax_marketplace_scope_excludes_facilitator_channels(self):
        scoped = reports._default_tax_marketplace_scope(
            sales_marketplace_options=["ebay", "local", "shopify"],
            facilitator_channels={"ebay"},
        )
        self.assertEqual(scoped, ["local", "shopify"])

    def test_default_tax_marketplace_scope_empty_when_all_are_facilitators(self):
        scoped = reports._default_tax_marketplace_scope(
            sales_marketplace_options=["ebay"],
            facilitator_channels={"ebay"},
        )
        self.assertEqual(scoped, [])

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

    def test_tax_profile_rows_from_audit_logs_uses_latest_active_profiles(self):
        rows = [
            SimpleNamespace(
                changes_json=(
                    '{"profile_key":"local_default","profile_name":"Local","jurisdiction":"Golden, Colorado",'
                    '"tax_rate_percent":7.5,"shipping_taxable":true,"facilitator_channels":"ebay",'
                    '"tax_exempt_categories":"bullion,coins","effective_from":"2026-04-01",'
                    '"human_validation_status":"advisor_validated","advisor_evidence_link":"ticket-1","is_active":true}'
                )
            ),
            SimpleNamespace(
                changes_json=(
                    '{"profile_key":"local_default","profile_name":"Old Local","jurisdiction":"Old",'
                    '"tax_rate_percent":1.0,"shipping_taxable":false,"is_active":true}'
                )
            ),
            SimpleNamespace(
                changes_json='{"profile_key":"inactive","profile_name":"Inactive","is_active":false}'
            ),
        ]

        out = reports._tax_profile_rows_from_audit_logs(rows)

        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["profile_key"], "local_default")
        self.assertEqual(out[0]["profile_name"], "Local")
        self.assertEqual(out[0]["jurisdiction"], "Golden, Colorado")
        self.assertEqual(out[0]["tax_rate_percent"], 7.5)
        self.assertTrue(out[0]["shipping_taxable"])
        self.assertEqual(out[0]["human_validation_status"], "advisor_validated")

    def test_tax_signoff_rows_from_audit_logs_normalizes_evidence(self):
        rows = [
            SimpleNamespace(
                created_at=datetime(2026, 4, 28, 12, 0),
                actor="accountant",
                changes_json=(
                    '{"target_env":"prod","tax_period":"2026-04","jurisdiction":"Golden, Colorado",'
                    '"profile_key":"local_default","status":"approved","owner":"Keith",'
                    '"signoff_date":"2026-04-28","tax_packet_ref":"tax_review_packet.zip",'
                    '"tax_packet_hash":"hash-123",'
                    '"advisor_evidence_link":"advisor-ticket","tax_exception_count":2,"notes":"Reviewed"}'
                ),
            )
        ]

        out = reports._tax_signoff_rows_from_audit_logs(rows)

        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["actor"], "accountant")
        self.assertEqual(out[0]["tax_period"], "2026-04")
        self.assertEqual(out[0]["profile_key"], "local_default")
        self.assertEqual(out[0]["tax_exception_count"], 2)
        self.assertEqual(out[0]["tax_packet_hash"], "hash-123")

    def test_build_tax_reporting_signoff_payload_captures_packet_hash(self):
        payload = reports._build_tax_reporting_signoff_payload(
            target_env="Prod",
            tax_period="2026-04",
            jurisdiction="Golden, Colorado",
            profile_key="Local_Default",
            status="Approved",
            owner="Tax Owner",
            signoff_date=date(2026, 5, 1),
            tax_packet_ref="tax_review_packet_2026-04-01_2026-04-30.zip",
            tax_packet_hash="hash-123",
            advisor_evidence_link="advisor-ticket",
            tax_exception_count=2,
            notes="Reviewed.",
        )

        self.assertEqual(payload["target_env"], "prod")
        self.assertEqual(payload["profile_key"], "local_default")
        self.assertEqual(payload["status"], "approved")
        self.assertEqual(payload["signoff_date"], "2026-05-01")
        self.assertEqual(payload["tax_packet_hash"], "hash-123")
        self.assertEqual(payload["tax_packet_evidence_hash_sha256"], "hash-123")
        self.assertEqual(payload["tax_exception_count"], 2)

    def test_tax_reporting_signoff_review_flags_stale_approved_signoff(self):
        review = reports._build_tax_reporting_signoff_review(
            signoff_df=reports.pd.DataFrame(
                [
                    {
                        "recorded_at_utc": "2026-05-01T08:00:00+00:00",
                        "tax_period": "2026-04",
                        "jurisdiction": "Golden, Colorado",
                        "profile_key": "local_default",
                        "status": "approved",
                        "owner": "Tax Owner",
                        "signoff_date": "2026-05-01",
                        "tax_packet_ref": "tax_review_packet_2026-04.zip",
                        "tax_packet_hash": "stale",
                        "advisor_evidence_link": "advisor-ticket",
                        "tax_exception_count": 0,
                    }
                ]
            ),
            tax_period="2026-04",
            jurisdiction="Golden, Colorado",
            profile_key="local_default",
            tax_exception_count=1,
            current_packet_hash="hash-123",
            to_date=date(2026, 4, 30),
        )

        statuses = {row["check"]: row["status"] for row in review.to_dict("records")}
        self.assertEqual(statuses["Tax Sign-Off Evidence Present"], "pass")
        self.assertEqual(statuses["Approved Tax Sign-Off Jurisdiction Match"], "pass")
        self.assertEqual(statuses["Approved Tax Sign-Off Profile Match"], "pass")
        self.assertEqual(statuses["Approved Tax Sign-Off Exception Count"], "warn")
        self.assertEqual(statuses["Approved Tax Sign-Off Packet Hash"], "warn")

    def test_tax_reporting_signoff_review_uses_latest_approved_signoff(self):
        review = reports._build_tax_reporting_signoff_review(
            signoff_df=reports.pd.DataFrame(
                [
                    {
                        "recorded_at_utc": "2026-04-30T08:00:00+00:00",
                        "tax_period": "2026-04",
                        "jurisdiction": "Old Jurisdiction",
                        "profile_key": "old_profile",
                        "status": "approved",
                        "owner": "Tax Owner",
                        "signoff_date": "2026-04-30",
                        "tax_packet_ref": "old.zip",
                        "tax_packet_hash": "stale",
                        "advisor_evidence_link": "advisor-ticket",
                        "tax_exception_count": 1,
                    },
                    {
                        "recorded_at_utc": "2026-05-01T08:00:00+00:00",
                        "tax_period": "2026-04",
                        "jurisdiction": "Golden, Colorado",
                        "profile_key": "local_default",
                        "status": "approved",
                        "owner": "Tax Owner",
                        "signoff_date": "2026-05-01",
                        "tax_packet_ref": "tax_review_packet_2026-04.zip",
                        "tax_packet_hash": "hash-123",
                        "advisor_evidence_link": "advisor-ticket",
                        "tax_exception_count": 0,
                    },
                ]
            ),
            tax_period="2026-04",
            jurisdiction="Golden, Colorado",
            profile_key="local_default",
            tax_exception_count=0,
            current_packet_hash="hash-123",
            to_date=date(2026, 4, 30),
        )

        statuses = {row["check"]: row["status"] for row in review.to_dict("records")}
        self.assertEqual(statuses["Approved Tax Sign-Off Jurisdiction Match"], "pass")
        self.assertEqual(statuses["Approved Tax Sign-Off Exception Count"], "pass")
        self.assertEqual(statuses["Approved Tax Sign-Off Packet Hash"], "pass")

    def test_accounting_close_signoff_rows_from_audit_logs_normalizes_evidence(self):
        rows = [
            SimpleNamespace(
                created_at=datetime(2026, 4, 28, 13, 0),
                actor="controller",
                changes_json=(
                    '{"target_env":"prod","signoff_type":"monthly_close_review","close_period":"2026-04",'
                    '"status":"approved","owner":"Keith","signoff_date":"2026-04-28",'
                    '"close_readiness_status":"close_ready","exception_count":0,'
                    '"unresolved_blocker_count":0,"period_drift_warn_count":0,'
                    '"ai_review_followup_count":0,'
                    '"accounting_packet_ref":"accounting_close_packet.zip",'
                    '"accounting_packet_hash":"abc123","evidence_link":"close-ticket",'
                    '"notes":"Reviewed"}'
                ),
            )
        ]

        out = reports._accounting_close_signoff_rows_from_audit_logs(rows)

        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["actor"], "controller")
        self.assertEqual(out[0]["signoff_type"], "monthly_close_review")
        self.assertEqual(out[0]["close_period"], "2026-04")
        self.assertEqual(out[0]["period_drift_warn_count"], 0)
        self.assertEqual(out[0]["ai_review_followup_count"], 0)
        self.assertEqual(out[0]["accounting_packet_ref"], "accounting_close_packet.zip")
        self.assertEqual(out[0]["accounting_packet_hash"], "abc123")

    def test_default_accounting_close_period_prefers_month_key(self):
        self.assertEqual(
            reports._default_accounting_close_period(date(2026, 4, 1), date(2026, 4, 30)),
            "2026-04",
        )
        self.assertEqual(
            reports._default_accounting_close_period(date(2026, 4, 15), date(2026, 5, 1)),
            "2026-04-15..2026-05-01",
        )

    def test_build_accounting_close_signoff_payload_captures_packet_hash(self):
        payload = reports._build_accounting_close_signoff_payload(
            target_env="Prod",
            close_period="2026-04",
            status="Approved",
            owner="Finance Owner",
            signoff_date=date(2026, 5, 1),
            close_summary={
                "readiness_status": "close_ready",
                "total_exceptions": 0,
                "blocker_count": 0,
                "period_drift_warn_count": 0,
                "ai_review_followup_count": 0,
            },
            accounting_packet_ref="accounting_close_packet_2026-04-01_2026-04-30.zip",
            accounting_packet_hash="abc123",
            evidence_link="ticket-123",
            notes="Reviewed packet.",
        )

        self.assertEqual(payload["target_env"], "prod")
        self.assertEqual(payload["status"], "approved")
        self.assertEqual(payload["signoff_date"], "2026-05-01")
        self.assertEqual(payload["close_readiness_status"], "close_ready")
        self.assertEqual(payload["ai_review_followup_count"], 0)
        self.assertEqual(payload["accounting_packet_hash"], "abc123")
        self.assertEqual(payload["accounting_close_packet_evidence_hash_sha256"], "abc123")

    def test_build_tax_exception_rows_flags_review_conditions(self):
        tax_detail_df = reports.pd.DataFrame(
            [
                {
                    "sale_id": 1,
                    "marketplace": "ebay",
                    "category": "bullion",
                    "gross_sales": 100.0,
                    "shipping_cost": 5.0,
                    "is_tax_exempt_category": True,
                    "taxable_shipping_subtotal": 5.0,
                    "taxable_subtotal": 5.0,
                    "estimated_tax_collected": 0.38,
                },
                {
                    "sale_id": 2,
                    "marketplace": "local",
                    "category": "",
                    "gross_sales": 50.0,
                    "shipping_cost": 0.0,
                    "is_tax_exempt_category": False,
                    "taxable_shipping_subtotal": 0.0,
                    "taxable_subtotal": 50.0,
                    "estimated_tax_collected": 3.75,
                },
            ]
        )

        rows = reports._build_tax_exception_rows(
            tax_detail_df,
            tax_jurisdiction="",
            tax_rate_percent=0.0,
            shipping_taxable=True,
            facilitator_channels={"ebay"},
            tax_exempt_categories={"bullion", "coins"},
        )

        by_type = {str(row["exception_type"]): row for row in rows}
        self.assertIn("missing_tax_jurisdiction", by_type)
        self.assertIn("missing_or_zero_tax_rate", by_type)
        self.assertIn("facilitator_channel_in_tax_scope", by_type)
        self.assertIn("exempt_category_review_needed", by_type)
        self.assertIn("exempt_item_taxable_shipping_review_needed", by_type)
        self.assertIn("missing_tax_category", by_type)
        self.assertEqual(by_type["facilitator_channel_in_tax_scope"]["sale_id"], 1)
        self.assertEqual(by_type["missing_tax_category"]["sale_id"], 2)

    def test_build_tax_exception_rows_allows_clean_taxable_local_sale(self):
        tax_detail_df = reports.pd.DataFrame(
            [
                {
                    "sale_id": 3,
                    "marketplace": "local",
                    "category": "collectible",
                    "gross_sales": 80.0,
                    "shipping_cost": 4.0,
                    "is_tax_exempt_category": False,
                    "taxable_shipping_subtotal": 4.0,
                    "taxable_subtotal": 84.0,
                    "estimated_tax_collected": 6.3,
                }
            ]
        )

        rows = reports._build_tax_exception_rows(
            tax_detail_df,
            tax_jurisdiction="Golden, Colorado",
            tax_rate_percent=7.5,
            shipping_taxable=True,
            facilitator_channels={"ebay"},
            tax_exempt_categories={"bullion", "coins"},
        )

        self.assertEqual(rows, [])

    def test_colorado_suts_upload_workbook_fills_template_rows(self):
        tax_detail_df = reports.pd.DataFrame(
            [
                {"gross_sales": 100.0, "taxable_subtotal": 100.0},
                {"gross_sales": 25.5, "taxable_subtotal": 25.5},
            ]
        )

        payload, summary_df = reports._build_colorado_suts_upload_workbook(
            tax_detail_df,
            account_number="080390",
            gross_jurisdiction_code="70003",
            zero_filing_jurisdiction_codes=["110004"],
        )

        self.assertFalse(summary_df.empty)
        by_code = {str(row["jurisdiction_code"]): row for row in summary_df.to_dict("records")}
        self.assertEqual(by_code["70003"]["gross_amount"], "125.50")
        self.assertEqual(by_code["110004"]["gross_amount"], "0")

        wb = reports.load_workbook(BytesIO(payload), data_only=False)
        ws = wb["Upload Data"]
        self.assertIsNone(ws["A1"].value)
        self.assertEqual(ws.max_row, 5)
        self.assertEqual(ws.cell(4, 1).value, 80390)
        self.assertEqual(ws.cell(4, 5).value, "0")
        self.assertEqual(ws.cell(5, 1).value, 80390)
        self.assertEqual(ws.cell(5, 1).number_format, "000000")
        self.assertEqual(ws.cell(5, 5).value, "125.50")
        self.assertEqual(ws.cell(5, 5).number_format, "@")

    def test_colorado_suts_upload_workbook_can_append_golden_custom_row(self):
        tax_detail_df = reports.pd.DataFrame([{"gross_sales": 200.0}])

        payload, summary_df = reports._build_colorado_suts_upload_workbook(
            tax_detail_df,
            account_number="080390",
            gross_jurisdiction_code="110042",
            account_number_by_jurisdiction_code={
                "110042": reports.COLORADO_SUTS_GOLDEN_STATE_ACCOUNT_NUMBER,
            },
            custom_jurisdictions=[
                {
                    "account_type": "STATE",
                    "jurisdiction_code": "110042",
                    "jurisdiction_name": "GOLDEN",
                }
            ],
        )

        by_code = {str(row["jurisdiction_code"]): row for row in summary_df.to_dict("records")}
        self.assertEqual(by_code["110042"]["jurisdiction_name"], "GOLDEN")
        self.assertEqual(by_code["110042"]["account_type"], "STATE")
        self.assertEqual(by_code["110042"]["account_number"], "970074130001")
        self.assertEqual(by_code["110042"]["gross_amount"], "200.00")
        wb = reports.load_workbook(BytesIO(payload), data_only=False)
        ws = wb["Upload Data"]
        self.assertEqual(ws.max_row, 4)
        self.assertEqual(ws.cell(ws.max_row, 1).value, 970074130001)
        self.assertEqual(ws.cell(ws.max_row, 1).number_format, "000000000000")
        self.assertEqual(ws.cell(ws.max_row, 2).value, "STATE")
        self.assertEqual(ws.cell(ws.max_row, 3).value, 110042)
        self.assertEqual(ws.cell(ws.max_row, 4).value, "GOLDEN")
        self.assertEqual(ws.cell(ws.max_row, 5).value, "200.00")

    def test_colorado_suts_upload_workbook_can_zero_file_custom_golden_row(self):
        payload, summary_df = reports._build_colorado_suts_upload_workbook(
            reports.pd.DataFrame(),
            account_number="080390",
            gross_jurisdiction_code="",
            zero_filing_jurisdiction_codes=["110042"],
            account_number_by_jurisdiction_code={
                "110042": reports.COLORADO_SUTS_GOLDEN_STATE_ACCOUNT_NUMBER,
            },
            custom_jurisdictions=[
                {
                    "account_type": "STATE",
                    "jurisdiction_code": "110042",
                    "jurisdiction_name": "GOLDEN",
                }
            ],
        )

        by_code = {str(row["jurisdiction_code"]): row for row in summary_df.to_dict("records")}
        self.assertEqual(by_code["110042"]["filing_type"], "zero_filing")
        self.assertEqual(by_code["110042"]["account_number"], "970074130001")
        self.assertEqual(by_code["110042"]["gross_amount"], "0")
        wb = reports.load_workbook(BytesIO(payload), data_only=False)
        ws = wb["Upload Data"]
        self.assertEqual(ws.max_row, 4)
        self.assertEqual(ws.cell(ws.max_row, 1).value, 970074130001)
        self.assertEqual(ws.cell(ws.max_row, 2).value, "STATE")
        self.assertEqual(ws.cell(ws.max_row, 3).value, 110042)
        self.assertEqual(ws.cell(ws.max_row, 5).value, "0")

    def test_colorado_suts_upload_workbook_can_include_golden_state_and_local_rows(self):
        state_key = reports._suts_jurisdiction_key("110042", "STATE")
        local_key = reports._suts_jurisdiction_key("110042", "LOCAL")

        payload, summary_df = reports._build_colorado_suts_upload_workbook(
            reports.pd.DataFrame([{"gross_sales": 75.0}]),
            account_number="080390",
            gross_jurisdiction_codes=["110042"],
            gross_jurisdiction_keys=[state_key, local_key],
            account_number_by_jurisdiction_key={
                state_key: reports.COLORADO_SUTS_GOLDEN_STATE_ACCOUNT_NUMBER,
            },
            allow_blank_account_jurisdiction_keys={local_key},
            custom_jurisdictions=[
                {"account_type": "STATE", "jurisdiction_code": "110042", "jurisdiction_name": "GOLDEN"},
                {"account_type": "LOCAL", "jurisdiction_code": "110042", "jurisdiction_name": "GOLDEN"},
            ],
        )

        by_key = {str(row["jurisdiction_key"]): row for row in summary_df.to_dict("records")}
        self.assertEqual(by_key[state_key]["filing_type"], "gross_sales")
        self.assertEqual(by_key[state_key]["account_number"], "970074130001")
        self.assertEqual(by_key[state_key]["gross_amount"], "75.00")
        self.assertEqual(by_key[local_key]["filing_type"], "gross_sales")
        self.assertEqual(by_key[local_key]["account_number"], "")
        self.assertEqual(by_key[local_key]["gross_amount"], "75.00")

        wb = reports.load_workbook(BytesIO(payload), data_only=False)
        ws = wb["Upload Data"]
        self.assertEqual(ws.max_row, 5)
        state_row = ws.max_row - 1
        local_row = ws.max_row
        self.assertEqual(ws.cell(state_row, 2).value, "STATE")
        self.assertEqual(ws.cell(state_row, 1).value, 970074130001)
        self.assertEqual(ws.cell(state_row, 5).value, "75.00")
        self.assertEqual(ws.cell(local_row, 2).value, "LOCAL")
        self.assertIsNone(ws.cell(local_row, 1).value)
        self.assertEqual(ws.cell(local_row, 5).value, "75.00")

    def test_colorado_suts_upload_workbook_zero_files_golden_state_and_local_for_facilitator_only_month(self):
        state_key = reports._suts_jurisdiction_key("110042", "STATE")
        local_key = reports._suts_jurisdiction_key("110042", "LOCAL")

        payload, summary_df = reports._build_colorado_suts_upload_workbook(
            reports.pd.DataFrame(),
            account_number="080390",
            zero_filing_jurisdiction_codes=["110042"],
            zero_filing_jurisdiction_keys=[state_key, local_key],
            account_number_by_jurisdiction_key={
                state_key: reports.COLORADO_SUTS_GOLDEN_STATE_ACCOUNT_NUMBER,
            },
            allow_blank_account_jurisdiction_keys={local_key},
            custom_jurisdictions=[
                {"account_type": "STATE", "jurisdiction_code": "110042", "jurisdiction_name": "GOLDEN"},
                {"account_type": "LOCAL", "jurisdiction_code": "110042", "jurisdiction_name": "GOLDEN"},
            ],
        )

        by_key = {str(row["jurisdiction_key"]): row for row in summary_df.to_dict("records")}
        self.assertEqual(by_key[state_key]["filing_type"], "zero_filing")
        self.assertEqual(by_key[state_key]["gross_amount"], "0")
        self.assertEqual(by_key[state_key]["account_number"], "970074130001")
        self.assertEqual(by_key[local_key]["filing_type"], "zero_filing")
        self.assertEqual(by_key[local_key]["gross_amount"], "0")
        self.assertEqual(by_key[local_key]["account_number"], "")

        wb = reports.load_workbook(BytesIO(payload), data_only=False)
        ws = wb["Upload Data"]
        self.assertEqual(ws.max_row, 5)
        self.assertEqual(ws.cell(4, 2).value, "STATE")
        self.assertEqual(ws.cell(4, 5).value, "0")
        self.assertEqual(ws.cell(5, 2).value, "LOCAL")
        self.assertIsNone(ws.cell(5, 1).value)
        self.assertEqual(ws.cell(5, 5).value, "0")

    def test_colorado_suts_scope_summary_separates_reportable_and_facilitator_sales(self):
        rows = reports._build_colorado_suts_scope_summary_rows(
            reports.pd.DataFrame(
                [
                    {"marketplace": "local", "gross_sales": 100.0},
                    {"marketplace": "pos", "gross_sales": 25.5},
                ]
            ),
            reports.pd.DataFrame(
                [
                    {"marketplace": "ebay", "gross_sales": 300.0},
                ]
            ),
            selected_marketplaces={"local", "pos"},
            facilitator_channels={"ebay"},
        )

        by_scope = {row["scope"]: row for row in rows}
        self.assertEqual(by_scope["Reportable SUTS upload gross"]["suts_treatment"], "included")
        self.assertEqual(by_scope["Reportable SUTS upload gross"]["gross_sales"], 125.5)
        self.assertEqual(by_scope["Marketplace facilitator gross"]["suts_treatment"], "excluded by default")
        self.assertEqual(by_scope["Marketplace facilitator gross"]["gross_sales"], 300.0)

    def test_colorado_suts_scope_summary_flags_selected_facilitator_override(self):
        rows = reports._build_colorado_suts_scope_summary_rows(
            reports.pd.DataFrame([{"marketplace": "ebay", "gross_sales": 10.0}]),
            reports.pd.DataFrame(),
            selected_marketplaces={"ebay"},
            facilitator_channels={"ebay"},
        )

        self.assertTrue(any(row["suts_treatment"] == "facilitator selected" for row in rows))

    def test_colorado_suts_summary_warnings_flag_missing_golden_state_account(self):
        summary_df = reports.pd.DataFrame(
            [
                {
                    "account_number": "",
                    "account_type": "STATE",
                    "jurisdiction_code": "110042",
                    "jurisdiction_name": "GOLDEN",
                    "gross_amount": "10.00",
                    "filing_type": "gross_sales",
                }
            ]
        )

        warnings = reports._colorado_suts_summary_warnings(summary_df)

        self.assertTrue(any("Golden STATE" in warning for warning in warnings))
        self.assertTrue(any("gross-sales row is missing" in warning for warning in warnings))

    def test_colorado_suts_summary_warnings_flag_blank_golden_local_gross_account(self):
        summary_df = reports.pd.DataFrame(
            [
                {
                    "account_number": "",
                    "account_type": "LOCAL",
                    "jurisdiction_code": "110042",
                    "jurisdiction_name": "GOLDEN",
                    "gross_amount": "75.00",
                    "filing_type": "gross_sales",
                }
            ]
        )

        warnings = reports._colorado_suts_summary_warnings(summary_df)

        self.assertTrue(any("Golden LOCAL gross-sales row has a blank account number" in warning for warning in warnings))

    def test_filter_tax_detail_for_month_uses_sold_at(self):
        tax_detail_df = reports.pd.DataFrame(
            [
                {"sale_id": 1, "sold_at": "2026-04-30T12:00:00", "gross_sales": 10.0},
                {"sale_id": 2, "sold_at": "2026-05-01T12:00:00", "gross_sales": 20.0},
            ]
        )

        filtered = reports._filter_tax_detail_for_month(tax_detail_df, month_value="2026-05")

        self.assertEqual(filtered["sale_id"].tolist(), [2])
        start, end = reports._month_bounds_from_yyyy_mm("2026-05")
        self.assertEqual(start.isoformat(), "2026-05-01T00:00:00")
        self.assertEqual(end.month, 5)

    def test_colorado_suts_jurisdiction_options_load_from_template(self):
        reports._load_colorado_suts_jurisdiction_options.clear()
        options = reports._load_colorado_suts_jurisdiction_options()
        labels = [str(row["label"]) for row in options]
        self.assertTrue(any("70003 | BOULDER" in label for label in labels))
        self.assertTrue(any(str(row["jurisdiction_code"]) == "110004" for row in options))

    def test_accounting_close_readiness_summary_close_ready(self):
        inventory_df = reports.pd.DataFrame({"landed_inventory_value": [100.0, 50.0]})
        cogs_margin_df = reports.pd.DataFrame(
            {
                "gross_sales": [120.0],
                "net_before_cogs": [105.0],
                "fifo_cogs": [60.0],
                "fifo_margin": [45.0],
            }
        )
        returns_df = reports.pd.DataFrame(
            {
                "refund_amount": [5.0],
                "refund_fees": [2.0],
                "refund_shipping": [1.0],
            }
        )
        reconciliation_df = reports.pd.DataFrame({"reconcile_flag": [False]})
        shipping_df = reports.pd.DataFrame(
            {
                "shipping_charged": [8.0, 2.0],
                "shipping_label_spend": [4.0, 3.0],
            }
        )
        fee_source_df = reports.pd.DataFrame(
            {
                "actual_fee_source": ["normalized_order_finance_entries_marketplace_fee_sum"],
                "sales_count": [2],
                "actual_fee_total": [8.0],
            }
        )
        exceptions_df = reports.pd.DataFrame()

        summary, checks = reports._build_accounting_close_readiness_summary(
            inventory_df=inventory_df,
            cogs_margin_df=cogs_margin_df,
            returns_df=returns_df,
            reconciliation_df=reconciliation_df,
            shipping_economics_df=shipping_df,
            ebay_fee_source_priority_df=fee_source_df,
            accounting_exceptions_df=exceptions_df,
        )

        self.assertEqual(summary["readiness_status"], "close_ready")
        self.assertEqual(summary["inventory_value"], 150.0)
        self.assertEqual(summary["profit_before_returns"], 45.0)
        self.assertEqual(summary["net_after_returns_and_cogs"], 37.0)
        self.assertEqual(summary["estimated_profit_after_returns"], 37.0)
        self.assertEqual(summary["shipping_charged_total"], 10.0)
        self.assertEqual(summary["shipping_label_spend_total"], 7.0)
        self.assertEqual(summary["shipping_delta_total"], 3.0)
        self.assertEqual(summary["fee_total"], 8.0)
        self.assertTrue((checks["status"] == "pass").all())

    def test_accounting_close_profit_helpers_prefer_clear_fields_with_legacy_fallback(self):
        clear_summary = {
            "profit_before_returns": 45.0,
            "fifo_margin": 999.0,
            "estimated_profit_after_returns": 37.0,
            "net_after_returns_and_cogs": 999.0,
        }
        legacy_summary = {
            "fifo_margin": 45.0,
            "net_after_returns_and_cogs": 37.0,
        }

        self.assertEqual(reports._accounting_close_profit_before_returns(clear_summary), 45.0)
        self.assertEqual(reports._accounting_close_estimated_profit_after_returns(clear_summary), 37.0)
        self.assertEqual(reports._accounting_close_profit_before_returns(legacy_summary), 45.0)
        self.assertEqual(reports._accounting_close_estimated_profit_after_returns(legacy_summary), 37.0)

    def test_accounting_close_readiness_summary_applies_return_cogs_reversal(self):
        summary, checks = reports._build_accounting_close_readiness_summary(
            inventory_df=reports.pd.DataFrame({"landed_inventory_value": [100.0]}),
            cogs_margin_df=reports.pd.DataFrame(
                {
                    "gross_sales": [120.0],
                    "net_before_cogs": [105.0],
                    "fifo_cogs": [60.0],
                    "fifo_margin": [45.0],
                }
            ),
            returns_df=reports.pd.DataFrame(
                {
                    "refund_amount": [30.0],
                    "refund_fees": [2.0],
                    "refund_shipping": [1.0],
                }
            ),
            reconciliation_df=reports.pd.DataFrame({"reconcile_flag": [False]}),
            shipping_economics_df=reports.pd.DataFrame({"shipping_label_spend": [4.0]}),
            ebay_fee_source_priority_df=reports.pd.DataFrame(
                {
                    "actual_fee_source": ["normalized_order_finance_entries_marketplace_fee_sum"],
                    "sales_count": [1],
                }
            ),
            accounting_exceptions_df=reports.pd.DataFrame(),
            qbo_adjustments_df=reports.pd.DataFrame({"cogs_reversal_estimate": [25.0]}),
        )

        self.assertEqual(summary["returns_refund_total"], 33.0)
        self.assertEqual(summary["returns_cogs_reversal_total"], 25.0)
        self.assertEqual(summary["returns_estimated_profit_impact"], -8.0)
        self.assertEqual(summary["profit_before_returns"], 45.0)
        self.assertEqual(summary["net_after_returns_and_cogs"], 37.0)
        self.assertEqual(summary["estimated_profit_after_returns"], 37.0)
        reversal_row = checks[checks["check"] == "Return COGS Reversal"].iloc[0]
        self.assertEqual(reversal_row["status"], "info")
        self.assertEqual(reversal_row["value"], 25.0)

    def test_accounting_period_drift_checks_compare_close_summary_to_qbo_exports(self):
        drift = reports._build_accounting_period_drift_checks(
            close_summary={
                "sales_count": 1,
                "gross_sales": 120.0,
                "net_before_cogs": 105.0,
                "fifo_cogs": 60.0,
                "fifo_margin": 45.0,
                "returns_refund_total": 33.0,
                "returns_cogs_reversal_total": 25.0,
                "net_after_returns_and_cogs": 37.0,
            },
            qbo_sales_df=reports.pd.DataFrame(
                {
                    "amount": [120.0],
                    "shipping_cost": [0.0],
                    "fees": [15.0],
                    "shipping_label_cost": [0.0],
                    "net_amount": [105.0],
                    "cogs_input_estimate": [60.0],
                    "gross_margin_estimate": [45.0],
                }
            ),
            qbo_adjustments_df=reports.pd.DataFrame(
                {
                    "refund_amount": [30.0],
                    "refund_fees": [2.0],
                    "refund_shipping": [1.0],
                    "cogs_reversal_estimate": [25.0],
                    "estimated_profit_impact": [-8.0],
                }
            ),
        )

        self.assertTrue((drift["status"] == "pass").all())
        by_check = {row["check"]: row for row in drift.to_dict("records")}
        self.assertEqual(
            by_check["net_after_returns_and_cogs_close_vs_qbo"]["observed"],
            37.0,
        )

    def test_accounting_close_formula_checks_validate_core_profit_math(self):
        checks = reports._build_accounting_close_formula_checks(
            {
                "net_before_cogs": 105.0,
                "fifo_cogs": 60.0,
                "fifo_margin": 45.0,
                "gross_sales": 120.0,
                "shipping_charged_total": 9.0,
                "fee_total": 20.0,
                "shipping_label_spend_total": 4.0,
                "shipping_delta_total": 5.0,
                "returns_refund_total": 33.0,
                "returns_cogs_reversal_total": 25.0,
                "returns_estimated_profit_impact": -8.0,
                "net_after_returns_and_cogs": 37.0,
            }
        )

        self.assertTrue((checks["status"] == "pass").all())
        by_check = {row["check"]: row for row in checks.to_dict("records")}
        self.assertEqual(by_check["net_before_cogs_component_formula"]["expected"], 105.0)
        self.assertEqual(by_check["net_before_cogs_minus_fifo_cogs_equals_fifo_margin"]["expected"], 45.0)
        self.assertEqual(by_check["shipping_delta_total_formula"]["expected"], 5.0)
        self.assertEqual(by_check["return_profit_impact_formula"]["expected"], -8.0)
        self.assertEqual(by_check["net_after_returns_and_cogs_formula"]["expected"], 37.0)

    def test_accounting_formula_warnings_block_close_readiness(self):
        summary, checks = reports._apply_formula_checks_to_close_readiness(
            {
                "readiness_status": "close_ready",
                "blocker_count": 0,
                "blockers": "",
            },
            reports.pd.DataFrame({"check": ["P0 Exceptions"], "status": ["pass"], "value": [0]}),
            reports.pd.DataFrame(
                {
                    "check": ["net_before_cogs_minus_fifo_cogs_equals_fifo_margin"],
                    "status": ["warn"],
                }
            ),
        )

        self.assertEqual(summary["readiness_status"], "blocked")
        self.assertEqual(summary["formula_warn_count"], 1)
        self.assertIn("accounting formula warnings", summary["blockers"])
        formula_row = checks[checks["check"] == "Accounting Formula Warnings"].iloc[0]
        self.assertEqual(formula_row["status"], "fail")
        self.assertEqual(formula_row["value"], 1)

    def test_accounting_sales_component_checks_tie_sales_detail_to_margin(self):
        checks = reports._build_accounting_sales_component_checks(
            sales_df=reports.pd.DataFrame(
                {
                    "gross_sales": [100.0],
                    "actual_shipping_charged": [5.0],
                    "actual_fee": [8.0],
                    "actual_shipping_label_cost": [4.0],
                    "actual_net_before_cogs": [93.0],
                }
            ),
            cogs_margin_df=reports.pd.DataFrame(
                {
                    "gross_sales": [100.0],
                    "net_before_cogs": [93.0],
                }
            ),
        )

        self.assertTrue((checks["status"] == "pass").all())
        by_check = {row["check"]: row for row in checks.to_dict("records")}
        self.assertEqual(by_check["sales_detail_component_net_formula"]["expected"], 93.0)
        self.assertEqual(by_check["sales_detail_net_matches_cogs_margin"]["observed"], 93.0)

    def test_sales_component_warnings_block_close_readiness(self):
        summary, checks = reports._apply_sales_component_checks_to_close_readiness(
            {
                "readiness_status": "close_ready",
                "blocker_count": 0,
                "blockers": "",
            },
            reports.pd.DataFrame({"check": ["P0 Exceptions"], "status": ["pass"], "value": [0]}),
            reports.pd.DataFrame(
                {
                    "check": ["sales_detail_net_matches_cogs_margin"],
                    "status": ["warn"],
                }
            ),
        )

        self.assertEqual(summary["readiness_status"], "blocked")
        self.assertEqual(summary["sales_component_warn_count"], 1)
        self.assertIn("sales component tie-out warnings", summary["blockers"])
        component_row = checks[checks["check"] == "Sales Component Tie-Out Warnings"].iloc[0]
        self.assertEqual(component_row["status"], "fail")
        self.assertEqual(component_row["value"], 1)

    def test_accounting_return_tieout_checks_match_qbo_adjustments(self):
        checks = reports._build_accounting_return_tieout_checks(
            returns_df=reports.pd.DataFrame(
                {
                    "refund_amount": [30.0],
                    "refund_fees": [2.0],
                    "refund_shipping": [1.0],
                }
            ),
            qbo_adjustments_df=reports.pd.DataFrame(
                {
                    "refund_amount": [30.0],
                    "refund_fees": [2.0],
                    "refund_shipping": [1.0],
                    "cogs_reversal_estimate": [25.0],
                    "estimated_profit_impact": [-8.0],
                }
            ),
            close_summary={
                "returns_cogs_reversal_total": 25.0,
                "returns_estimated_profit_impact": -8.0,
            },
        )

        self.assertTrue((checks["status"] == "pass").all())
        by_check = {row["check"]: row for row in checks.to_dict("records")}
        self.assertEqual(by_check["returns_refund_total_matches_qbo_adjustments"]["expected"], 33.0)
        self.assertEqual(by_check["qbo_return_profit_impact_formula"]["expected"], -8.0)

    def test_return_tieout_warnings_block_close_readiness(self):
        summary, checks = reports._apply_return_tieout_checks_to_close_readiness(
            {
                "readiness_status": "close_ready",
                "blocker_count": 0,
                "blockers": "",
            },
            reports.pd.DataFrame({"check": ["P0 Exceptions"], "status": ["pass"], "value": [0]}),
            reports.pd.DataFrame(
                {
                    "check": ["return_profit_impact_matches_close_summary"],
                    "status": ["warn"],
                }
            ),
        )

        self.assertEqual(summary["readiness_status"], "blocked")
        self.assertEqual(summary["return_tieout_warn_count"], 1)
        self.assertIn("return tie-out warnings", summary["blockers"])
        tieout_row = checks[checks["check"] == "Return Tie-Out Warnings"].iloc[0]
        self.assertEqual(tieout_row["status"], "fail")
        self.assertEqual(tieout_row["value"], 1)

    def test_accounting_inventory_valuation_checks_validate_stocked_costs(self):
        checks = reports._build_accounting_inventory_valuation_checks(
            inventory_df=reports.pd.DataFrame(
                {
                    "qty_on_hand": [2, 1],
                    "landed_unit_cost": [10.0, 5.0],
                    "landed_inventory_value": [20.0, 5.0],
                }
            ),
            close_summary={"inventory_value": 25.0},
        )

        self.assertTrue((checks["status"] == "pass").all())
        by_check = {row["check"]: row for row in checks.to_dict("records")}
        self.assertEqual(by_check["inventory_snapshot_value_formula"]["expected"], 25.0)
        self.assertEqual(by_check["close_inventory_value_matches_inventory_snapshot"]["observed"], 25.0)

    def test_inventory_valuation_warnings_block_close_readiness(self):
        summary, checks = reports._apply_inventory_valuation_checks_to_close_readiness(
            {
                "readiness_status": "close_ready",
                "blocker_count": 0,
                "blockers": "",
            },
            reports.pd.DataFrame({"check": ["P0 Exceptions"], "status": ["pass"], "value": [0]}),
            reports.pd.DataFrame(
                {
                    "check": ["stocked_inventory_rows_have_landed_cost"],
                    "status": ["warn"],
                }
            ),
        )

        self.assertEqual(summary["readiness_status"], "blocked")
        self.assertEqual(summary["inventory_valuation_warn_count"], 1)
        self.assertIn("inventory valuation warnings", summary["blockers"])
        valuation_row = checks[checks["check"] == "Inventory Valuation Warnings"].iloc[0]
        self.assertEqual(valuation_row["status"], "fail")
        self.assertEqual(valuation_row["value"], 1)

    def test_accounting_fee_evidence_checks_tie_reconciliation_to_sales_detail(self):
        checks = reports._build_accounting_fee_evidence_checks(
            sales_df=reports.pd.DataFrame({"actual_fee": [8.0]}),
            fee_reconciliation_df=reports.pd.DataFrame(
                {
                    "sale_id": [1],
                    "actual_fee": [8.0],
                    "actual_fee_source": ["normalized_order_finance_entries_marketplace_fee_sum"],
                }
            ),
            fee_source_priority_df=reports.pd.DataFrame(
                {
                    "actual_fee_source": ["normalized_order_finance_entries_marketplace_fee_sum"],
                    "sales_count": [1],
                    "actual_fee_total": [8.0],
                }
            ),
        )

        self.assertTrue((checks["status"] == "pass").all())
        by_check = {row["check"]: row for row in checks.to_dict("records")}
        self.assertEqual(by_check["fee_reconciliation_total_matches_sales_detail"]["observed"], 8.0)
        self.assertEqual(by_check["sale_fee_field_fallback_rows"]["observed"], 0.0)

    def test_fee_evidence_warnings_block_close_readiness(self):
        summary, checks = reports._apply_fee_evidence_checks_to_close_readiness(
            {
                "readiness_status": "close_ready",
                "blocker_count": 0,
                "blockers": "",
            },
            reports.pd.DataFrame({"check": ["P0 Exceptions"], "status": ["pass"], "value": [0]}),
            reports.pd.DataFrame(
                {
                    "check": ["sale_fee_field_fallback_rows"],
                    "status": ["warn"],
                }
            ),
        )

        self.assertEqual(summary["readiness_status"], "blocked")
        self.assertEqual(summary["fee_evidence_warn_count"], 1)
        self.assertIn("fee evidence warnings", summary["blockers"])
        fee_row = checks[checks["check"] == "Fee Evidence Warnings"].iloc[0]
        self.assertEqual(fee_row["status"], "fail")
        self.assertEqual(fee_row["value"], 1)

    def test_accounting_shipping_evidence_checks_tie_shipping_tables(self):
        checks = reports._build_accounting_shipping_evidence_checks(
            sales_df=reports.pd.DataFrame(
                {
                    "actual_shipping_charged": [5.0],
                    "actual_shipping_label_cost": [4.0],
                }
            ),
            shipping_economics_df=reports.pd.DataFrame(
                {
                    "shipping_charged_to_buyer": [5.0],
                    "shipping_label_spend": [4.0],
                    "shipping_delta_charged_minus_spend": [1.0],
                }
            ),
            shipping_econ_summary_df=reports.pd.DataFrame(
                {
                    "total_shipping_charged": [5.0],
                    "total_label_spend": [4.0],
                    "shipping_delta_charged_minus_spend": [1.0],
                }
            ),
        )

        self.assertTrue((checks["status"] == "pass").all())
        by_check = {row["check"]: row for row in checks.to_dict("records")}
        self.assertEqual(by_check["shipping_economics_delta_formula"]["expected"], 1.0)
        self.assertEqual(by_check["paid_shipping_rows_missing_label_spend"]["observed"], 0.0)

    def test_shipping_evidence_warnings_block_close_readiness(self):
        summary, checks = reports._apply_shipping_evidence_checks_to_close_readiness(
            {
                "readiness_status": "close_ready",
                "blocker_count": 0,
                "blockers": "",
            },
            reports.pd.DataFrame({"check": ["P0 Exceptions"], "status": ["pass"], "value": [0]}),
            reports.pd.DataFrame(
                {
                    "check": ["paid_shipping_rows_missing_label_spend"],
                    "status": ["warn"],
                }
            ),
        )

        self.assertEqual(summary["readiness_status"], "blocked")
        self.assertEqual(summary["shipping_evidence_warn_count"], 1)
        self.assertIn("shipping evidence warnings", summary["blockers"])
        shipping_row = checks[checks["check"] == "Shipping Evidence Warnings"].iloc[0]
        self.assertEqual(shipping_row["status"], "fail")
        self.assertEqual(shipping_row["value"], 1)

    def test_accounting_reconciliation_tieout_checks_tie_marketplace_rows(self):
        checks = reports._build_accounting_reconciliation_tieout_checks(
            sales_df=reports.pd.DataFrame(
                {
                    "gross_sales": [100.0],
                    "actual_net_before_cogs": [93.0],
                }
            ),
            returns_df=reports.pd.DataFrame(
                {
                    "refund_amount": [10.0],
                    "refund_fees": [1.0],
                    "refund_shipping": [2.0],
                }
            ),
            reconciliation_df=reports.pd.DataFrame(
                {
                    "sales_count": [1],
                    "returns_count": [1],
                    "sales_gross": [100.0],
                    "sales_net_before_returns": [93.0],
                    "returns_refund_total": [13.0],
                    "net_after_returns": [80.0],
                    "reconcile_flag": [False],
                }
            ),
            close_summary={"reconcile_flags": 0},
        )

        self.assertTrue((checks["status"] == "pass").all())
        by_check = {row["check"]: row for row in checks.to_dict("records")}
        self.assertEqual(by_check["reconciliation_net_after_returns_formula"]["expected"], 80.0)
        self.assertEqual(by_check["reconciliation_flags_match_close_summary"]["observed"], 0.0)

    def test_reconciliation_tieout_warnings_block_close_readiness(self):
        summary, checks = reports._apply_reconciliation_tieout_checks_to_close_readiness(
            {
                "readiness_status": "close_ready",
                "blocker_count": 0,
                "blockers": "",
            },
            reports.pd.DataFrame({"check": ["P0 Exceptions"], "status": ["pass"], "value": [0]}),
            reports.pd.DataFrame(
                {
                    "check": ["reconciliation_sales_gross_matches_sales_detail"],
                    "status": ["warn"],
                }
            ),
        )

        self.assertEqual(summary["readiness_status"], "blocked")
        self.assertEqual(summary["reconciliation_tieout_warn_count"], 1)
        self.assertIn("reconciliation tie-out warnings", summary["blockers"])
        tieout_row = checks[checks["check"] == "Reconciliation Tie-Out Warnings"].iloc[0]
        self.assertEqual(tieout_row["status"], "fail")
        self.assertEqual(tieout_row["value"], 1)

    def test_accounting_cogs_source_checks_tie_source_summary_to_margin(self):
        checks = reports._build_accounting_cogs_source_checks(
            cogs_margin_df=reports.pd.DataFrame(
                {
                    "quantity": [2],
                    "fifo_cogs": [50.0],
                    "fifo_margin": [45.0],
                    "fifo_cogs_evidence_rows": [2],
                }
            ),
            cogs_source_summary_df=reports.pd.DataFrame(
                {
                    "fifo_cost_source": ["lot_expected_quantity_fallback"],
                    "sale_count": [1],
                    "quantity": [2],
                    "fifo_cogs": [50.0],
                    "fifo_margin": [45.0],
                }
            ),
            sale_fifo_cogs_evidence_df=reports.pd.DataFrame(
                [
                    {"sale_id": 1, "quantity": 1, "total_cost": 25.0},
                    {"sale_id": 1, "quantity": 1, "total_cost": 25.0},
                ]
            ),
            close_summary={
                "fifo_cogs": 50.0,
                "sold_equal_fallback_cogs": 0.0,
                "sold_missing_cost_cogs": 0.0,
            },
        )

        self.assertTrue((checks["status"] == "pass").all())
        by_check = {row["check"]: row for row in checks.to_dict("records")}
        self.assertEqual(by_check["cogs_source_fifo_cogs_matches_close_summary"]["observed"], 50.0)
        self.assertEqual(by_check["fifo_cogs_evidence_total_matches_margin_detail"]["observed"], 50.0)
        self.assertEqual(by_check["fifo_cogs_evidence_sale_count_matches_margin_detail"]["observed"], 1.0)
        self.assertEqual(by_check["fifo_cogs_evidence_row_count_matches_margin_detail"]["observed"], 2.0)
        self.assertEqual(by_check["sold_equal_fallback_cogs_present"]["observed"], 0.0)

    def test_accounting_cogs_source_checks_warn_when_fifo_evidence_is_missing(self):
        checks = reports._build_accounting_cogs_source_checks(
            cogs_margin_df=reports.pd.DataFrame(
                {
                    "quantity": [1],
                    "fifo_cogs": [25.0],
                    "fifo_margin": [10.0],
                    "fifo_cogs_evidence_rows": [1],
                }
            ),
            cogs_source_summary_df=reports.pd.DataFrame(
                {
                    "fifo_cost_source": ["assignment_unit_landed_cost"],
                    "sale_count": [1],
                    "quantity": [1],
                    "fifo_cogs": [25.0],
                    "fifo_margin": [10.0],
                }
            ),
            sale_fifo_cogs_evidence_df=reports.pd.DataFrame(),
            close_summary={
                "fifo_cogs": 25.0,
                "sold_equal_fallback_cogs": 0.0,
                "sold_missing_cost_cogs": 0.0,
            },
        )

        by_check = {row["check"]: row for row in checks.to_dict("records")}
        self.assertEqual(by_check["fifo_cogs_evidence_total_matches_margin_detail"]["status"], "warn")
        self.assertEqual(by_check["fifo_cogs_evidence_sale_count_matches_margin_detail"]["status"], "warn")
        self.assertEqual(by_check["fifo_cogs_evidence_row_count_matches_margin_detail"]["status"], "warn")

    def test_cogs_source_summary_includes_bundle_inventory_evidence(self):
        summary = reports._build_cogs_source_summary(
            reports.pd.DataFrame(
                [
                    {
                        "fifo_cost_source": "product_default_landed_cost",
                        "quantity": 2,
                        "listing_is_bundle": True,
                        "listing_bundle_inventory_units_sold": 10,
                        "gross_sales": 100.0,
                        "net_before_cogs": 90.0,
                        "fifo_cogs": 50.0,
                        "fifo_margin": 40.0,
                    },
                    {
                        "fifo_cost_source": "product_default_landed_cost",
                        "quantity": 1,
                        "listing_is_bundle": False,
                        "listing_bundle_inventory_units_sold": 0,
                        "gross_sales": 25.0,
                        "net_before_cogs": 20.0,
                        "fifo_cogs": 10.0,
                        "fifo_margin": 10.0,
                    },
                ]
            )
        )

        row = summary.iloc[0]
        self.assertEqual(int(row["sale_count"]), 2)
        self.assertEqual(int(row["bundle_sale_count"]), 1)
        self.assertEqual(int(row["bundle_inventory_units_sold"]), 10)

    def test_cogs_source_warnings_block_close_readiness(self):
        summary, checks = reports._apply_cogs_source_checks_to_close_readiness(
            {
                "readiness_status": "close_ready",
                "blocker_count": 0,
                "blockers": "",
            },
            reports.pd.DataFrame({"check": ["P0 Exceptions"], "status": ["pass"], "value": [0]}),
            reports.pd.DataFrame(
                {
                    "check": ["sold_equal_fallback_cogs_present"],
                    "status": ["warn"],
                }
            ),
        )

        self.assertEqual(summary["readiness_status"], "blocked")
        self.assertEqual(summary["cogs_source_warn_count"], 1)
        self.assertIn("COGS source warnings", summary["blockers"])
        cogs_row = checks[checks["check"] == "COGS Source Warnings"].iloc[0]
        self.assertEqual(cogs_row["status"], "fail")
        self.assertEqual(cogs_row["value"], 1)

    def test_accounting_lot_allocation_checks_tie_summary_to_lot_detail(self):
        checks = reports._build_accounting_lot_allocation_checks(
            lots_df=reports.pd.DataFrame(
                {
                    "quantity_acquired": [2],
                    "resolved_landed_total_cost": [50.0],
                }
            ),
            lot_allocation_source_summary_df=reports.pd.DataFrame(
                {
                    "cost_source": ["lot_expected_quantity_fallback"],
                    "assignment_count": [1],
                    "quantity_acquired": [2],
                    "resolved_landed_total_cost": [50.0],
                }
            ),
            close_summary={
                "lot_equal_fallback_assignments": 0,
                "lot_missing_cost_assignments": 0,
            },
        )

        self.assertTrue((checks["status"] == "pass").all())
        by_check = {row["check"]: row for row in checks.to_dict("records")}
        self.assertEqual(by_check["lot_allocation_resolved_cost_matches_detail"]["observed"], 50.0)
        self.assertEqual(by_check["lot_equal_fallback_assignments_present"]["observed"], 0.0)

    def test_lot_allocation_warnings_block_close_readiness(self):
        summary, checks = reports._apply_lot_allocation_checks_to_close_readiness(
            {
                "readiness_status": "close_ready",
                "blocker_count": 0,
                "blockers": "",
            },
            reports.pd.DataFrame({"check": ["P0 Exceptions"], "status": ["pass"], "value": [0]}),
            reports.pd.DataFrame(
                {
                    "check": ["lot_equal_fallback_assignments_present"],
                    "status": ["warn"],
                }
            ),
        )

        self.assertEqual(summary["readiness_status"], "blocked")
        self.assertEqual(summary["lot_allocation_warn_count"], 1)
        self.assertIn("lot allocation warnings", summary["blockers"])
        lot_row = checks[checks["check"] == "Lot Allocation Warnings"].iloc[0]
        self.assertEqual(lot_row["status"], "fail")
        self.assertEqual(lot_row["value"], 1)

    def test_accounting_exception_queue_checks_tie_counts_to_close_summary(self):
        checks = reports._build_accounting_exception_queue_checks(
            accounting_exceptions_df=reports.pd.DataFrame(
                {
                    "severity": ["P0", "P1"],
                    "exception_type": ["missing_cost_basis", "missing_label_spend"],
                }
            ),
            close_summary={
                "total_exceptions": 2,
                "p0_exceptions": 1,
                "p1_exceptions": 1,
            },
        )

        by_check = {row["check"]: row for row in checks.to_dict("records")}
        self.assertEqual(by_check["total_exception_count_matches_close_summary"]["status"], "pass")
        self.assertEqual(by_check["p0_exception_count_matches_close_summary"]["status"], "pass")
        self.assertEqual(by_check["p1_exception_count_matches_close_summary"]["status"], "pass")
        self.assertEqual(by_check["p0_exceptions_present"]["status"], "warn")
        self.assertEqual(by_check["p0_exceptions_present"]["observed"], 1.0)
        self.assertEqual(by_check["exception_rows_have_type"]["status"], "pass")

    def test_exception_queue_warnings_block_close_readiness(self):
        summary, checks = reports._apply_exception_queue_checks_to_close_readiness(
            {
                "readiness_status": "close_ready",
                "blocker_count": 0,
                "blockers": "",
            },
            reports.pd.DataFrame({"check": ["P0 Exceptions"], "status": ["pass"], "value": [0]}),
            reports.pd.DataFrame(
                {
                    "check": ["p0_exceptions_present"],
                    "status": ["warn"],
                }
            ),
        )

        self.assertEqual(summary["readiness_status"], "blocked")
        self.assertEqual(summary["exception_queue_warn_count"], 1)
        self.assertIn("exception queue warnings", summary["blockers"])
        exception_row = checks[checks["check"] == "Exception Queue Warnings"].iloc[0]
        self.assertEqual(exception_row["status"], "fail")
        self.assertEqual(exception_row["value"], 1)

    def test_accounting_margin_anomaly_checks_tie_margin_detail_to_exception_queue(self):
        checks = reports._build_accounting_margin_anomaly_checks(
            cogs_margin_df=reports.pd.DataFrame({"fifo_margin": [-5.0, 0.0, 12.0]}),
            accounting_exceptions_df=reports.pd.DataFrame(
                {
                    "severity": ["P1", "P1"],
                    "exception_type": ["nonpositive_margin", "nonpositive_margin"],
                }
            ),
            close_summary={"negative_margin_rows": 1},
        )

        by_check = {row["check"]: row for row in checks.to_dict("records")}
        self.assertEqual(by_check["negative_fifo_margin_rows_match_close_summary"]["status"], "pass")
        self.assertEqual(by_check["nonpositive_fifo_margin_rows_have_exception"]["status"], "pass")
        self.assertEqual(by_check["negative_fifo_margin_rows_present"]["status"], "warn")
        self.assertEqual(by_check["nonpositive_fifo_margin_rows_present"]["status"], "warn")

    def test_margin_anomaly_warnings_block_close_readiness(self):
        summary, checks = reports._apply_margin_anomaly_checks_to_close_readiness(
            {
                "readiness_status": "close_ready",
                "blocker_count": 0,
                "blockers": "",
            },
            reports.pd.DataFrame({"check": ["P0 Exceptions"], "status": ["pass"], "value": [0]}),
            reports.pd.DataFrame(
                {
                    "check": ["negative_fifo_margin_rows_present"],
                    "status": ["warn"],
                }
            ),
        )

        self.assertEqual(summary["readiness_status"], "blocked")
        self.assertEqual(summary["margin_anomaly_warn_count"], 1)
        self.assertIn("margin anomaly warnings", summary["blockers"])
        margin_row = checks[checks["check"] == "Margin Anomaly Warnings"].iloc[0]
        self.assertEqual(margin_row["status"], "fail")
        self.assertEqual(margin_row["value"], 1)

    def test_accounting_close_consistency_checks_validate_summary_and_status(self):
        checks = reports._build_accounting_close_consistency_checks(
            close_summary={
                "readiness_status": "blocked",
                "blocker_count": 1,
                "warning_count": 1,
                "blockers": "P0 accounting exceptions",
                "warnings": "P1 accounting exceptions",
            },
            close_checks_df=reports.pd.DataFrame(
                {
                    "check": ["P0 Exceptions", "P1 Exceptions"],
                    "status": ["fail", "warn"],
                    "value": [1, 1],
                }
            ),
        )

        self.assertTrue((checks["status"] == "pass").all())

    def test_accounting_close_consistency_checks_warn_on_mismatch(self):
        checks = reports._build_accounting_close_consistency_checks(
            close_summary={
                "readiness_status": "close_ready",
                "blocker_count": 2,
                "warning_count": 0,
                "blockers": "P0 accounting exceptions",
                "warnings": "",
            },
            close_checks_df=reports.pd.DataFrame(
                {
                    "check": ["P0 Exceptions"],
                    "status": ["fail"],
                    "value": [1],
                }
            ),
        )

        by_check = {row["check"]: row for row in checks.to_dict("records")}
        self.assertEqual(by_check["blocker_count_matches_blocker_list"]["status"], "warn")
        self.assertEqual(by_check["failed_close_checks_have_blocked_status"]["status"], "warn")
        self.assertEqual(by_check["close_ready_has_no_blockers_or_warnings"]["status"], "warn")

    def test_accounting_close_packet_completeness_checks_validate_required_artifacts(self):
        required_prefixes = [
            "accounting_close_readiness_checks",
            "accounting_close_formula_checks",
            "accounting_sales_component_checks",
            "accounting_return_tieout_checks",
            "accounting_inventory_valuation_checks",
            "accounting_fee_evidence_checks",
            "accounting_shipping_evidence_checks",
            "accounting_reconciliation_tieout_checks",
            "accounting_cogs_source_checks",
            "accounting_lot_allocation_checks",
            "accounting_exception_queue_checks",
            "accounting_margin_anomaly_checks",
            "accounting_close_consistency_checks",
            "accounting_period_drift_checks",
            "inventory_snapshot",
            "sales_detail",
            "cogs_margin_detail",
            "sale_fifo_cogs_evidence",
            "qbo_sales_export",
            "qbo_adjustments_export",
        ]
        checks = reports._build_accounting_close_packet_completeness_checks(
            report_frames={
                prefix: reports.pd.DataFrame({"x": [1]})
                for prefix in required_prefixes
            },
            close_summary={"sales_count": 1, "returns_refund_total": 8.0},
        )

        self.assertTrue((checks["status"] == "pass").all())

    def test_accounting_close_packet_completeness_checks_warn_on_missing_artifact(self):
        checks = reports._build_accounting_close_packet_completeness_checks(
            report_frames={
                "accounting_close_readiness_checks": reports.pd.DataFrame({"x": [1]}),
                "sales_detail": reports.pd.DataFrame(),
            },
            close_summary={"sales_count": 1},
        )

        by_artifact = {row["artifact"]: row for row in checks.to_dict("records")}
        self.assertEqual(by_artifact["accounting_close_formula_checks.csv"]["status"], "warn")
        self.assertFalse(by_artifact["accounting_close_formula_checks.csv"]["present_in_report_list"])
        self.assertEqual(by_artifact["sales_detail.csv"]["status"], "warn")
        self.assertEqual(by_artifact["qbo_adjustments_export.csv"]["status"], "pass")

    def test_accounting_close_packet_completeness_requires_adjustment_export_for_returns(self):
        checks = reports._build_accounting_close_packet_completeness_checks(
            report_frames={
                "accounting_close_readiness_checks": reports.pd.DataFrame({"x": [1]}),
                "qbo_sales_export": reports.pd.DataFrame({"x": [1]}),
            },
            close_summary={"sales_count": 1, "returns_refund_total": 8.0},
        )

        by_artifact = {row["artifact"]: row for row in checks.to_dict("records")}
        self.assertEqual(by_artifact["qbo_adjustments_export.csv"]["status"], "warn")
        self.assertFalse(by_artifact["qbo_adjustments_export.csv"]["present_in_report_list"])

    def test_accounting_close_packet_manifest_checks_validate_selected_prefixes(self):
        frames = {
            prefix: reports.pd.DataFrame({"x": [1, 2]})
            for prefix in reports._accounting_close_packet_prefixes()
        }
        checks = reports._build_accounting_close_packet_manifest_checks(report_frames=frames)

        self.assertTrue((checks["status"] == "pass").all())
        by_artifact = {row["artifact"]: row for row in checks.to_dict("records")}
        self.assertEqual(by_artifact["sales_detail.csv"]["manifest_key"], "row_count_sales_detail")
        self.assertEqual(by_artifact["sales_detail.csv"]["manifest_value"], 2)
        self.assertEqual(by_artifact["sales_detail.csv"]["observed_rows"], 2)

    def test_accounting_close_packet_manifest_checks_warn_on_missing_selected_prefix(self):
        checks = reports._build_accounting_close_packet_manifest_checks(
            report_frames={"sales_detail": reports.pd.DataFrame({"x": [1]})}
        )

        by_artifact = {row["artifact"]: row for row in checks.to_dict("records")}
        self.assertEqual(by_artifact["accounting_close_readiness_checks.csv"]["status"], "warn")
        self.assertFalse(by_artifact["accounting_close_readiness_checks.csv"]["present_in_report_list"])

    def test_accounting_close_packet_hash_checks_validate_selected_prefix_hashes(self):
        frames = {
            prefix: reports.pd.DataFrame({"x": [1, 2]})
            for prefix in reports._accounting_close_packet_prefixes()
        }
        checks = reports._build_accounting_close_packet_hash_checks(report_frames=frames)

        self.assertTrue((checks["status"] == "pass").all())
        by_artifact = {row["artifact"]: row for row in checks.to_dict("records")}
        expected_hash = reports.hashlib.sha256(
            reports.pd.DataFrame({"x": [1, 2]}).to_csv(index=False).encode("utf-8")
        ).hexdigest()
        self.assertEqual(by_artifact["sales_detail.csv"]["manifest_hash_key"], "sha256_sales_detail")
        self.assertEqual(by_artifact["sales_detail.csv"]["sha256"], expected_hash)

    def test_accounting_close_packet_hash_checks_warn_on_missing_selected_prefix(self):
        checks = reports._build_accounting_close_packet_hash_checks(
            report_frames={"sales_detail": reports.pd.DataFrame({"x": [1]})}
        )

        by_artifact = {row["artifact"]: row for row in checks.to_dict("records")}
        self.assertEqual(by_artifact["accounting_close_readiness_checks.csv"]["status"], "warn")
        self.assertFalse(by_artifact["accounting_close_readiness_checks.csv"]["present_in_report_list"])

    def test_accounting_close_packet_evidence_hash_rows_expose_copyable_hash(self):
        rows = reports._build_accounting_close_packet_evidence_hash_rows(
            evidence_hash="a" * 64,
            from_date=date(2026, 4, 1),
            to_date=date(2026, 4, 30),
        )

        row = rows.iloc[0].to_dict()
        self.assertEqual(row["hash_key"], "accounting_close_packet_evidence_hash_sha256")
        self.assertEqual(row["sha256"], "a" * 64)
        self.assertIn("excludes sign-off", row["hash_scope"])

    def test_accounting_period_drift_checks_warn_on_mismatch(self):
        drift = reports._build_accounting_period_drift_checks(
            close_summary={"sales_count": 1, "gross_sales": 120.0},
            qbo_sales_df=reports.pd.DataFrame({"amount": [119.0]}),
            qbo_adjustments_df=reports.pd.DataFrame(),
        )

        gross_row = drift[drift["check"] == "gross_sales_close_vs_qbo"].iloc[0]
        self.assertEqual(gross_row["status"], "warn")
        self.assertEqual(gross_row["delta_observed_minus_expected"], -1.0)

    def test_accounting_period_drift_checks_validate_qbo_sales_formulas(self):
        drift = reports._build_accounting_period_drift_checks(
            close_summary={
                "sales_count": 1,
                "gross_sales": 120.0,
                "net_before_cogs": 113.0,
                "fifo_cogs": 60.0,
                "fifo_margin": 53.0,
                "returns_refund_total": 13.0,
                "returns_cogs_reversal_total": 6.0,
                "net_after_returns_and_cogs": 46.0,
            },
            qbo_sales_df=reports.pd.DataFrame(
                {
                    "amount": [120.0],
                    "shipping_cost": [5.0],
                    "fees": [8.0],
                    "shipping_label_cost": [4.0],
                    "net_amount": [113.0],
                    "cogs_input_estimate": [60.0],
                    "gross_margin_estimate": [53.0],
                    "profit_before_returns_estimate": [53.0],
                }
            ),
            qbo_adjustments_df=reports.pd.DataFrame(
                {
                    "refund_amount": [10.0],
                    "refund_fees": [2.0],
                    "refund_shipping": [1.0],
                    "cogs_reversal_estimate": [6.0],
                    "estimated_profit_impact": [-7.0],
                }
            ),
        )

        by_check = {row["check"]: row for row in drift.to_dict("records")}
        self.assertEqual(by_check["qbo_sales_net_formula"]["status"], "pass")
        self.assertEqual(by_check["profit_before_returns_close_vs_qbo"]["status"], "pass")
        self.assertEqual(
            by_check["profit_before_returns_close_vs_qbo"]["expected_source"],
            "Accounting Close Readiness.profit_before_returns",
        )
        self.assertEqual(
            by_check["net_after_returns_and_cogs_close_vs_qbo"]["expected_source"],
            "Accounting Close Readiness.estimated_profit_after_returns",
        )
        self.assertEqual(by_check["qbo_sales_profit_before_returns_formula"]["status"], "pass")
        self.assertEqual(by_check["qbo_return_profit_impact_formula"]["status"], "pass")

        stale_drift = reports._build_accounting_period_drift_checks(
            close_summary={
                "sales_count": 1,
                "gross_sales": 120.0,
                "net_before_cogs": 113.0,
                "fifo_cogs": 60.0,
                "fifo_margin": 53.0,
                "returns_refund_total": 13.0,
                "returns_cogs_reversal_total": 6.0,
                "net_after_returns_and_cogs": 46.0,
            },
            qbo_sales_df=reports.pd.DataFrame(
                {
                    "amount": [120.0],
                    "shipping_cost": [5.0],
                    "fees": [8.0],
                    "shipping_label_cost": [4.0],
                    "net_amount": [112.0],
                    "cogs_input_estimate": [60.0],
                    "gross_margin_estimate": [53.0],
                    "profit_before_returns_estimate": [53.0],
                }
            ),
            qbo_adjustments_df=reports.pd.DataFrame(
                {
                    "refund_amount": [10.0],
                    "refund_fees": [2.0],
                    "refund_shipping": [1.0],
                    "cogs_reversal_estimate": [6.0],
                    "estimated_profit_impact": [-6.0],
                }
            ),
        )

        stale_by_check = {row["check"]: row for row in stale_drift.to_dict("records")}
        self.assertEqual(stale_by_check["qbo_sales_net_formula"]["status"], "warn")
        self.assertEqual(stale_by_check["qbo_sales_profit_before_returns_formula"]["status"], "warn")
        self.assertEqual(stale_by_check["qbo_return_profit_impact_formula"]["status"], "warn")

    def test_period_drift_warnings_block_close_readiness(self):
        summary, checks = reports._apply_period_drift_to_close_readiness(
            {"readiness_status": "close_ready", "blocker_count": 0, "blockers": ""},
            reports.pd.DataFrame([{"check": "P0 Exceptions", "status": "pass", "value": 0}]),
            reports.pd.DataFrame(
                [
                    {
                        "check": "gross_sales_close_vs_qbo",
                        "status": "warn",
                        "expected": 120.0,
                        "observed": 119.0,
                    }
                ]
            ),
        )

        self.assertEqual(summary["readiness_status"], "blocked")
        self.assertEqual(summary["period_drift_warn_count"], 1)
        self.assertIn("period drift warnings", summary["blockers"])
        drift_row = checks[checks["check"] == "Period Drift Warnings"].iloc[0]
        self.assertEqual(drift_row["status"], "fail")
        self.assertEqual(drift_row["value"], 1)

    def test_ai_review_outcome_followup_blocks_close_readiness(self):
        summary, checks = reports._apply_ai_review_outcomes_to_close_readiness(
            {"readiness_status": "close_ready", "blocker_count": 0, "blockers": ""},
            reports.pd.DataFrame([{"check": "P0 Exceptions", "status": "pass", "value": 0}]),
            reports.pd.DataFrame(
                [
                    {
                        "recorded_at_utc": "2026-05-04T12:00:00",
                        "review_type": "reports_copilot_review",
                        "outcome": "edited",
                    }
                ]
            ),
        )

        self.assertEqual(summary["readiness_status"], "blocked")
        self.assertEqual(summary["ai_review_followup_count"], 1)
        self.assertIn("AI review outcome follow-up", summary["blockers"])
        outcome_row = checks[checks["check"] == "AI Review Outcome Follow-Up"].iloc[0]
        self.assertEqual(outcome_row["status"], "fail")
        self.assertEqual(outcome_row["value"], 1)

    def test_ai_review_outcome_followup_uses_latest_outcome_per_review_type(self):
        summary, checks = reports._apply_ai_review_outcomes_to_close_readiness(
            {"readiness_status": "close_ready", "blocker_count": 0, "blockers": ""},
            reports.pd.DataFrame(),
            reports.pd.DataFrame(
                [
                    {
                        "recorded_at_utc": "2026-05-04T12:00:00",
                        "review_type": "ai_accountant_review",
                        "outcome": "edited",
                    },
                    {
                        "recorded_at_utc": "2026-05-04T13:00:00",
                        "review_type": "ai_accountant_review",
                        "outcome": "accepted",
                    },
                ]
            ),
        )

        self.assertEqual(summary["readiness_status"], "close_ready")
        self.assertEqual(summary["ai_review_followup_count"], 0)
        self.assertEqual(checks.iloc[0]["status"], "pass")

    def test_accounting_period_drift_checks_include_dashboard_metrics_when_available(self):
        drift = reports._build_accounting_period_drift_checks(
            close_summary={
                "sales_count": 1,
                "gross_sales": 120.0,
                "net_before_cogs": 117.0,
                "fee_total": 8.0,
                "shipping_charged_total": 8.0,
                "shipping_label_spend_total": 3.0,
                "shipping_delta_total": 5.0,
                "fifo_cogs": 60.0,
                "fifo_margin": 57.0,
            },
            qbo_sales_df=reports.pd.DataFrame(
                {
                    "amount": [120.0],
                    "net_amount": [117.0],
                    "cogs_input_estimate": [60.0],
                    "gross_margin_estimate": [57.0],
                }
            ),
            qbo_adjustments_df=reports.pd.DataFrame(),
            dashboard_live_metrics={
                "sales_30d_count": 1,
                "sales_30d_gross": 120.0,
                "sales_30d_net": 117.0,
                "ebay_fees_30d_total": 8.0,
                "sales_30d_shipping_charged": 8.0,
                "sales_30d_shipping_label_spend": 3.0,
                "sales_30d_shipping_delta": 5.0,
                "sales_30d_est_cogs": 60.0,
                "sales_30d_profit_before_returns": 57.0,
                "sales_30d_est_profit": 57.0,
            },
        )

        by_check = {row["check"]: row for row in drift.to_dict("records")}
        self.assertEqual(by_check["gross_sales_close_vs_dashboard_30d"]["status"], "pass")
        self.assertEqual(by_check["fee_total_close_vs_dashboard_30d"]["status"], "pass")
        self.assertEqual(by_check["shipping_charged_close_vs_dashboard_30d"]["status"], "pass")
        self.assertEqual(by_check["shipping_label_spend_close_vs_dashboard_30d"]["status"], "pass")
        self.assertEqual(by_check["shipping_delta_close_vs_dashboard_30d"]["status"], "pass")
        self.assertEqual(by_check["dashboard_30d_net_formula"]["status"], "pass")
        self.assertEqual(by_check["dashboard_30d_shipping_delta_formula"]["status"], "pass")
        self.assertEqual(by_check["dashboard_30d_profit_formula"]["status"], "pass")
        self.assertEqual(by_check["profit_before_returns_close_vs_dashboard_30d"]["observed"], 57.0)
        self.assertEqual(by_check["estimated_profit_after_returns_close_vs_dashboard_30d"]["observed"], 57.0)

    def test_accounting_period_drift_checks_include_dashboard_return_impact(self):
        drift = reports._build_accounting_period_drift_checks(
            close_summary={
                "sales_count": 1,
                "gross_sales": 100.0,
                "net_before_cogs": 91.0,
                "fee_total": 10.0,
                "shipping_charged_total": 5.0,
                "shipping_label_spend_total": 4.0,
                "shipping_delta_total": 1.0,
                "fifo_cogs": 25.0,
                "fifo_margin": 66.0,
                "returns_refund_total": 100.0,
                "returns_cogs_reversal_total": 25.0,
                "returns_estimated_profit_impact": -75.0,
                "net_after_returns_and_cogs": -9.0,
            },
            qbo_sales_df=reports.pd.DataFrame(),
            qbo_adjustments_df=reports.pd.DataFrame(),
            dashboard_live_metrics={
                "sales_30d_count": 1,
                "sales_30d_gross": 100.0,
                "sales_30d_net": 91.0,
                "ebay_fees_30d_total": 10.0,
                "sales_30d_shipping_charged": 5.0,
                "sales_30d_shipping_label_spend": 4.0,
                "sales_30d_shipping_delta": 1.0,
                "sales_30d_est_cogs": 25.0,
                "sales_30d_profit_before_returns": 66.0,
                "returns_30d_count": 1,
                "returns_30d_refund_total": 100.0,
                "returns_30d_cogs_reversal": 25.0,
                "returns_30d_profit_impact": -75.0,
                "sales_30d_est_profit": -9.0,
            },
        )

        by_check = {row["check"]: row for row in drift.to_dict("records")}
        self.assertEqual(by_check["returns_refund_total_close_vs_dashboard_30d"]["status"], "pass")
        self.assertEqual(by_check["returns_cogs_reversal_close_vs_dashboard_30d"]["status"], "pass")
        self.assertEqual(by_check["dashboard_30d_return_profit_impact_formula"]["status"], "pass")
        self.assertEqual(by_check["dashboard_30d_profit_formula"]["status"], "pass")
        self.assertEqual(by_check["profit_before_returns_close_vs_dashboard_30d"]["expected"], 66.0)
        self.assertEqual(by_check["estimated_profit_after_returns_close_vs_dashboard_30d"]["expected"], -9.0)

    def test_accounting_period_drift_checks_warn_on_dashboard_component_mismatch(self):
        drift = reports._build_accounting_period_drift_checks(
            close_summary={
                "sales_count": 1,
                "gross_sales": 120.0,
                "net_before_cogs": 105.0,
                "fee_total": 8.0,
                "shipping_charged_total": 8.0,
                "shipping_label_spend_total": 3.0,
                "shipping_delta_total": 5.0,
                "fifo_cogs": 60.0,
                "fifo_margin": 45.0,
            },
            qbo_sales_df=reports.pd.DataFrame(
                {
                    "amount": [120.0],
                    "net_amount": [105.0],
                    "cogs_input_estimate": [60.0],
                    "gross_margin_estimate": [45.0],
                }
            ),
            qbo_adjustments_df=reports.pd.DataFrame(),
            dashboard_live_metrics={
                "sales_30d_count": 1,
                "sales_30d_gross": 120.0,
                "sales_30d_net": 105.0,
                "ebay_fees_30d_total": 9.0,
                "sales_30d_shipping_charged": 8.0,
                "sales_30d_shipping_label_spend": 4.0,
                "sales_30d_shipping_delta": 4.0,
                "sales_30d_est_cogs": 60.0,
                "sales_30d_est_profit": 46.0,
            },
        )

        by_check = {row["check"]: row for row in drift.to_dict("records")}
        self.assertEqual(by_check["fee_total_close_vs_dashboard_30d"]["status"], "warn")
        self.assertEqual(by_check["shipping_charged_close_vs_dashboard_30d"]["status"], "pass")
        self.assertEqual(by_check["shipping_label_spend_close_vs_dashboard_30d"]["status"], "warn")
        self.assertEqual(by_check["shipping_delta_close_vs_dashboard_30d"]["status"], "warn")
        self.assertEqual(by_check["dashboard_30d_shipping_delta_formula"]["status"], "pass")
        self.assertEqual(by_check["dashboard_30d_net_formula"]["status"], "warn")
        self.assertEqual(by_check["dashboard_30d_profit_formula"]["status"], "warn")

    def test_accounting_period_drift_checks_include_slack_summary_metrics_when_available(self):
        slack_metrics = reports._build_slack_summary_drift_metrics(
            reports.pd.DataFrame(
                {
                    "gross_sales": [120.0],
                    "net_before_cogs": [105.0],
                    "fifo_cogs": [60.0],
                }
            ),
            window_label="daily",
            returns_df=reports.pd.DataFrame(
                {
                    "refund_amount": [20.0],
                    "refund_fees": [2.0],
                    "refund_shipping": [3.0],
                }
            ),
            qbo_adjustments_df=reports.pd.DataFrame({"cogs_reversal_estimate": [10.0]}),
        )
        drift = reports._build_accounting_period_drift_checks(
            close_summary={
                "sales_count": 1,
                "gross_sales": 120.0,
                "net_before_cogs": 105.0,
                "fifo_cogs": 60.0,
                "fifo_margin": 45.0,
                "returns_refund_total": 25.0,
                "returns_cogs_reversal_total": 10.0,
                "net_after_returns_and_cogs": 30.0,
            },
            qbo_sales_df=reports.pd.DataFrame(
                {
                    "amount": [120.0],
                    "net_amount": [105.0],
                    "cogs_input_estimate": [60.0],
                    "gross_margin_estimate": [45.0],
                }
            ),
            qbo_adjustments_df=reports.pd.DataFrame(),
            slack_summary_metrics=slack_metrics,
        )

        by_check = {row["check"]: row for row in drift.to_dict("records")}
        self.assertEqual(by_check["gross_sales_close_vs_slack_daily"]["status"], "pass")
        self.assertEqual(by_check["profit_before_returns_close_vs_slack_daily"]["observed"], 45.0)
        self.assertEqual(by_check["slack_daily_profit_before_returns_formula"]["status"], "pass")
        self.assertEqual(by_check["returns_refund_close_vs_slack_daily"]["status"], "pass")
        self.assertEqual(by_check["returns_cogs_reversal_close_vs_slack_daily"]["status"], "pass")
        self.assertEqual(by_check["slack_daily_return_profit_impact_formula"]["status"], "pass")
        self.assertEqual(by_check["net_after_returns_and_cogs_close_vs_slack_daily"]["observed"], 30.0)
        self.assertEqual(by_check["slack_daily_estimated_profit_after_returns_formula"]["status"], "pass")

        stale_metrics = dict(slack_metrics)
        stale_metrics["profit_before_returns"] = 44.0
        stale_drift = reports._build_accounting_period_drift_checks(
            close_summary={
                "sales_count": 1,
                "gross_sales": 120.0,
                "net_before_cogs": 105.0,
                "fifo_cogs": 60.0,
                "fifo_margin": 45.0,
                "returns_refund_total": 25.0,
                "returns_cogs_reversal_total": 10.0,
                "net_after_returns_and_cogs": 30.0,
            },
            qbo_sales_df=reports.pd.DataFrame(),
            qbo_adjustments_df=reports.pd.DataFrame(),
            slack_summary_metrics=stale_metrics,
        )

        stale_by_check = {row["check"]: row for row in stale_drift.to_dict("records")}
        self.assertEqual(stale_by_check["slack_daily_profit_before_returns_formula"]["status"], "warn")

    def test_accounting_period_drift_checks_accept_legacy_slack_estimated_margin(self):
        slack_metrics = {
            "window_label": "daily",
            "observed_source": "Slack daily business summary",
            "sales_window_count": 1,
            "gross_window": 120.0,
            "net_window": 105.0,
            "cogs_window": 60.0,
            "estimated_margin": 45.0,
            "returns_refund_window": 0.0,
            "returns_cogs_reversal_window": 0.0,
            "returns_profit_impact_window": 0.0,
            "estimated_profit_after_returns": 45.0,
        }
        drift = reports._build_accounting_period_drift_checks(
            close_summary={
                "sales_count": 1,
                "gross_sales": 120.0,
                "net_before_cogs": 105.0,
                "fifo_cogs": 60.0,
                "fifo_margin": 45.0,
                "returns_refund_total": 0.0,
                "returns_cogs_reversal_total": 0.0,
                "net_after_returns_and_cogs": 45.0,
            },
            qbo_sales_df=reports.pd.DataFrame(),
            qbo_adjustments_df=reports.pd.DataFrame(),
            slack_summary_metrics=slack_metrics,
        )

        by_check = {row["check"]: row for row in drift.to_dict("records")}
        self.assertEqual(by_check["profit_before_returns_close_vs_slack_daily"]["status"], "pass")
        self.assertEqual(by_check["profit_before_returns_close_vs_slack_daily"]["observed"], 45.0)
        self.assertIn(
            "estimated_margin",
            by_check["profit_before_returns_close_vs_slack_daily"]["observed_source"],
        )
        self.assertEqual(by_check["slack_daily_profit_before_returns_formula"]["status"], "pass")

    def test_accounting_period_drift_checks_include_ai_accounting_metrics_when_available(self):
        ai_metrics = reports._build_ai_accounting_snapshot_drift_metrics(
            reports.pd.DataFrame(
                {
                    "gross_sales": [120.0],
                    "net_before_cogs": [105.0],
                    "fifo_cogs": [60.0],
                }
            ),
            window_label="30d",
            returns_df=reports.pd.DataFrame(
                {
                    "refund_amount": [20.0],
                    "refund_fees": [2.0],
                    "refund_shipping": [3.0],
                }
            ),
            qbo_adjustments_df=reports.pd.DataFrame({"cogs_reversal_estimate": [10.0]}),
        )
        drift = reports._build_accounting_period_drift_checks(
            close_summary={
                "sales_count": 1,
                "gross_sales": 120.0,
                "net_before_cogs": 105.0,
                "fifo_cogs": 60.0,
                "fifo_margin": 45.0,
                "returns_refund_total": 25.0,
                "returns_cogs_reversal_total": 10.0,
                "net_after_returns_and_cogs": 30.0,
            },
            qbo_sales_df=reports.pd.DataFrame(
                {
                    "amount": [120.0],
                    "net_amount": [105.0],
                    "cogs_input_estimate": [60.0],
                    "gross_margin_estimate": [45.0],
                }
            ),
            qbo_adjustments_df=reports.pd.DataFrame(),
            ai_accounting_snapshot_metrics=ai_metrics,
        )

        by_check = {row["check"]: row for row in drift.to_dict("records")}
        self.assertEqual(by_check["gross_sales_close_vs_ai_accounting_30d"]["status"], "pass")
        self.assertEqual(by_check["profit_before_returns_close_vs_ai_accounting_30d"]["observed"], 45.0)
        self.assertEqual(by_check["ai_accounting_30d_profit_before_returns_formula"]["status"], "pass")
        self.assertIn(
            "Ask/AI accounting snapshot",
            by_check["profit_before_returns_close_vs_ai_accounting_30d"]["observed_source"],
        )
        self.assertEqual(by_check["returns_refund_close_vs_ai_accounting_30d"]["status"], "pass")
        self.assertEqual(by_check["returns_cogs_reversal_close_vs_ai_accounting_30d"]["status"], "pass")
        self.assertEqual(by_check["ai_accounting_30d_return_profit_impact_formula"]["status"], "pass")
        self.assertEqual(by_check["net_after_returns_and_cogs_close_vs_ai_accounting_30d"]["observed"], 30.0)
        self.assertEqual(by_check["ai_accounting_30d_estimated_profit_after_returns_formula"]["status"], "pass")

        stale_metrics = dict(ai_metrics)
        stale_metrics["profit_before_returns"] = 44.0
        stale_drift = reports._build_accounting_period_drift_checks(
            close_summary={
                "sales_count": 1,
                "gross_sales": 120.0,
                "net_before_cogs": 105.0,
                "fifo_cogs": 60.0,
                "fifo_margin": 45.0,
                "returns_refund_total": 25.0,
                "returns_cogs_reversal_total": 10.0,
                "net_after_returns_and_cogs": 30.0,
            },
            qbo_sales_df=reports.pd.DataFrame(),
            qbo_adjustments_df=reports.pd.DataFrame(),
            ai_accounting_snapshot_metrics=stale_metrics,
        )

        stale_by_check = {row["check"]: row for row in stale_drift.to_dict("records")}
        self.assertEqual(stale_by_check["ai_accounting_30d_profit_before_returns_formula"]["status"], "warn")

    def test_accounting_period_drift_checks_accept_legacy_ai_estimated_margin(self):
        ai_metrics = {
            "window_label": "30d",
            "observed_source": "Ask/AI accounting snapshot",
            "sales_window_count": 1,
            "gross_window": 120.0,
            "net_window": 105.0,
            "cogs_window": 60.0,
            "estimated_margin": 45.0,
            "returns_refund_window": 0.0,
            "returns_cogs_reversal_window": 0.0,
            "returns_profit_impact_window": 0.0,
            "estimated_profit_after_returns": 45.0,
        }
        drift = reports._build_accounting_period_drift_checks(
            close_summary={
                "sales_count": 1,
                "gross_sales": 120.0,
                "net_before_cogs": 105.0,
                "fifo_cogs": 60.0,
                "fifo_margin": 45.0,
                "returns_refund_total": 0.0,
                "returns_cogs_reversal_total": 0.0,
                "net_after_returns_and_cogs": 45.0,
            },
            qbo_sales_df=reports.pd.DataFrame(),
            qbo_adjustments_df=reports.pd.DataFrame(),
            ai_accounting_snapshot_metrics=ai_metrics,
        )

        by_check = {row["check"]: row for row in drift.to_dict("records")}
        self.assertEqual(by_check["profit_before_returns_close_vs_ai_accounting_30d"]["status"], "pass")
        self.assertEqual(by_check["profit_before_returns_close_vs_ai_accounting_30d"]["observed"], 45.0)
        self.assertIn(
            "estimated_margin",
            by_check["profit_before_returns_close_vs_ai_accounting_30d"]["observed_source"],
        )
        self.assertEqual(by_check["ai_accounting_30d_profit_before_returns_formula"]["status"], "pass")

    def test_lot_allocation_source_summary_rolls_up_cost_basis(self):
        lots_df = reports.pd.DataFrame(
            [
                {
                    "cost_source": "assignment_unit_landed_cost",
                    "quantity_acquired": 2,
                    "resolved_landed_total_cost": 20.0,
                },
                {
                    "cost_source": "lot_allocation_weight",
                    "quantity_acquired": 1,
                    "resolved_landed_total_cost": 75.0,
                },
                {
                    "cost_source": "lot_equal_quantity_fallback",
                    "quantity_acquired": 3,
                    "resolved_landed_total_cost": 30.0,
                },
            ]
        )

        summary = reports._build_lot_allocation_source_summary(lots_df)
        by_source = {str(row["cost_source"]): row for row in summary.to_dict("records")}

        self.assertEqual(by_source["assignment_unit_landed_cost"]["assignment_count"], 1)
        self.assertEqual(by_source["lot_allocation_weight"]["quantity_acquired"], 1)
        self.assertEqual(by_source["lot_equal_quantity_fallback"]["resolved_landed_total_cost"], 30.0)
        self.assertAlmostEqual(by_source["lot_allocation_weight"]["cost_share_pct"], 60.0)

    def test_cogs_source_summary_rolls_up_sold_cogs_basis(self):
        cogs_margin_df = reports.pd.DataFrame(
            [
                {
                    "fifo_cost_source": "lot_expected_quantity_fallback",
                    "quantity": 1,
                    "gross_sales": 30.0,
                    "net_before_cogs": 28.0,
                    "fifo_cogs": 10.0,
                    "fifo_margin": 18.0,
                },
                {
                    "fifo_cost_source": "lot_allocation_weight",
                    "quantity": 2,
                    "gross_sales": 180.0,
                    "net_before_cogs": 170.0,
                    "fifo_cogs": 90.0,
                    "fifo_margin": 80.0,
                },
            ]
        )

        summary = reports._build_cogs_source_summary(cogs_margin_df)
        by_source = {str(row["fifo_cost_source"]): row for row in summary.to_dict("records")}

        self.assertEqual(by_source["lot_expected_quantity_fallback"]["sale_count"], 1)
        self.assertEqual(by_source["lot_allocation_weight"]["quantity"], 2)
        self.assertEqual(by_source["lot_allocation_weight"]["fifo_cogs"], 90.0)
        self.assertAlmostEqual(by_source["lot_allocation_weight"]["cogs_share_pct"], 90.0)

    def test_accounting_close_readiness_summary_review_needed(self):
        summary, checks = reports._build_accounting_close_readiness_summary(
            inventory_df=reports.pd.DataFrame(),
            cogs_margin_df=reports.pd.DataFrame(
                {"gross_sales": [30.0], "net_before_cogs": [20.0], "fifo_cogs": [25.0], "fifo_margin": [-5.0]}
            ),
            returns_df=reports.pd.DataFrame(),
            reconciliation_df=reports.pd.DataFrame({"reconcile_flag": [False]}),
            shipping_economics_df=reports.pd.DataFrame({"shipping_label_spend": [0.0, 3.0]}),
            ebay_fee_source_priority_df=reports.pd.DataFrame(
                {"actual_fee_source": ["sale_fees_field"], "sales_count": [1]}
            ),
            accounting_exceptions_df=reports.pd.DataFrame(
                {"severity": ["P1"], "exception_type": ["missing_shipping_label_spend"]}
            ),
            lot_allocation_source_summary_df=reports.pd.DataFrame(
                {
                    "cost_source": ["lot_equal_quantity_fallback"],
                    "assignment_count": [2],
                    "resolved_landed_total_cost": [50.0],
                }
            ),
            cogs_source_summary_df=reports.pd.DataFrame(
                {
                    "fifo_cost_source": ["lot_equal_quantity_fallback"],
                    "fifo_cogs": [25.0],
                }
            ),
        )

        self.assertEqual(summary["readiness_status"], "review_needed")
        self.assertEqual(summary["p1_exceptions"], 1)
        self.assertEqual(summary["negative_margin_rows"], 1)
        self.assertEqual(summary["lot_equal_fallback_assignments"], 2)
        self.assertEqual(summary["sold_equal_fallback_cogs"], 25.0)
        self.assertTrue(
            any(
                row.get("check") == "Lot Equal Fallback Assignments" and row.get("status") == "warn"
                for row in checks.to_dict("records")
            )
        )
        self.assertTrue(
            any(
                row.get("check") == "Sold Equal Fallback COGS" and row.get("status") == "warn"
                for row in checks.to_dict("records")
            )
        )
        self.assertIn("warn", set(checks["status"]))

    def test_accounting_close_readiness_summary_blocked(self):
        summary, checks = reports._build_accounting_close_readiness_summary(
            inventory_df=reports.pd.DataFrame(),
            cogs_margin_df=reports.pd.DataFrame({"fifo_margin": [10.0]}),
            returns_df=reports.pd.DataFrame(),
            reconciliation_df=reports.pd.DataFrame({"reconcile_flag": [True]}),
            shipping_economics_df=reports.pd.DataFrame(),
            ebay_fee_source_priority_df=reports.pd.DataFrame(),
            accounting_exceptions_df=reports.pd.DataFrame(
                {"severity": ["P0"], "exception_type": ["missing_cost_basis"]}
            ),
        )

        self.assertEqual(summary["readiness_status"], "blocked")
        self.assertEqual(summary["blocker_count"], 2)
        self.assertIn("fail", set(checks["status"]))

    def test_accounting_close_signoff_review_flags_stale_approved_signoff(self):
        review = reports._build_accounting_close_signoff_review(
            signoff_df=reports.pd.DataFrame(
                [
                    {
                        "close_period": "2026-04",
                        "signoff_type": "monthly_close_review",
                        "status": "approved",
                        "close_readiness_status": "close_ready",
                        "exception_count": 0,
                        "unresolved_blocker_count": 0,
                        "period_drift_warn_count": 0,
                        "accounting_packet_ref": "accounting_close_packet_2026-04.zip",
                        "owner": "Finance Owner",
                        "signoff_date": "2026-04-30",
                    }
                ]
            ),
            close_summary={
                "readiness_status": "blocked",
                "total_exceptions": 1,
                "blocker_count": 1,
                "period_drift_warn_count": 1,
            },
            from_date=date(2026, 4, 1),
            to_date=date(2026, 4, 30),
        )

        statuses = {row["check"]: row["status"] for row in review.to_dict("records")}
        self.assertEqual(statuses["Close Sign-Off Evidence Present"], "pass")
        self.assertEqual(statuses["Approved Sign-Off Readiness Match"], "warn")
        self.assertEqual(statuses["Approved Sign-Off Blocker Count"], "warn")
        self.assertEqual(statuses["Approved Sign-Off Exception Count"], "warn")
        self.assertEqual(statuses["Approved Sign-Off Drift Warning Count"], "warn")
        self.assertEqual(statuses["Approved Sign-Off Is Close Ready"], "warn")
        self.assertEqual(statuses["Approved Sign-Off Packet Evidence"], "pass")

    def test_accounting_close_signoff_review_flags_missing_packet_evidence(self):
        review = reports._build_accounting_close_signoff_review(
            signoff_df=reports.pd.DataFrame(
                [
                    {
                        "close_period": "2026-04",
                        "signoff_type": "monthly_close_review",
                        "status": "approved",
                        "close_readiness_status": "close_ready",
                        "exception_count": 0,
                        "unresolved_blocker_count": 0,
                        "period_drift_warn_count": 0,
                        "owner": "Finance Owner",
                        "signoff_date": "2026-04-30",
                    }
                ]
            ),
            close_summary={
                "readiness_status": "close_ready",
                "total_exceptions": 0,
                "blocker_count": 0,
                "period_drift_warn_count": 0,
            },
            from_date=date(2026, 4, 1),
            to_date=date(2026, 4, 30),
            current_packet_hash="abc123",
        )

        statuses = {row["check"]: row["status"] for row in review.to_dict("records")}
        self.assertEqual(statuses["Approved Sign-Off Packet Evidence"], "warn")
        self.assertEqual(statuses["Approved Sign-Off Packet Hash"], "warn")

    def test_accounting_close_signoff_review_flags_missing_packet_hash_with_packet_ref(self):
        review = reports._build_accounting_close_signoff_review(
            signoff_df=reports.pd.DataFrame(
                [
                    {
                        "close_period": "2026-04",
                        "signoff_type": "monthly_close_review",
                        "status": "approved",
                        "close_readiness_status": "close_ready",
                        "exception_count": 0,
                        "unresolved_blocker_count": 0,
                        "period_drift_warn_count": 0,
                        "accounting_packet_ref": "accounting_close_packet_2026-04.zip",
                        "owner": "Finance Owner",
                        "signoff_date": "2026-04-30",
                    }
                ]
            ),
            close_summary={
                "readiness_status": "close_ready",
                "total_exceptions": 0,
                "blocker_count": 0,
                "period_drift_warn_count": 0,
            },
            from_date=date(2026, 4, 1),
            to_date=date(2026, 4, 30),
            current_packet_hash="abc123",
        )

        statuses = {row["check"]: row["status"] for row in review.to_dict("records")}
        self.assertEqual(statuses["Approved Sign-Off Packet Evidence"], "pass")
        self.assertEqual(statuses["Approved Sign-Off Packet Hash"], "warn")

    def test_accounting_close_signoff_review_compares_packet_hash_when_present(self):
        review = reports._build_accounting_close_signoff_review(
            signoff_df=reports.pd.DataFrame(
                [
                    {
                        "close_period": "2026-04",
                        "signoff_type": "monthly_close_review",
                        "status": "approved",
                        "close_readiness_status": "close_ready",
                        "exception_count": 0,
                        "unresolved_blocker_count": 0,
                        "period_drift_warn_count": 0,
                        "accounting_packet_ref": "accounting_close_packet_2026-04.zip",
                        "accounting_packet_hash": "abc123",
                        "owner": "Finance Owner",
                        "signoff_date": "2026-04-30",
                    }
                ]
            ),
            close_summary={
                "readiness_status": "close_ready",
                "total_exceptions": 0,
                "blocker_count": 0,
                "period_drift_warn_count": 0,
            },
            from_date=date(2026, 4, 1),
            to_date=date(2026, 4, 30),
            current_packet_hash="abc123",
        )

        statuses = {row["check"]: row["status"] for row in review.to_dict("records")}
        self.assertEqual(statuses["Approved Sign-Off Owner Present"], "pass")
        self.assertEqual(statuses["Approved Sign-Off Date Present"], "pass")
        self.assertEqual(statuses["Approved Sign-Off Date Validity"], "pass")
        self.assertEqual(statuses["Approved Sign-Off Packet Hash"], "pass")

        stale_review = reports._build_accounting_close_signoff_review(
            signoff_df=reports.pd.DataFrame(
                [
                    {
                        "close_period": "2026-04",
                        "signoff_type": "monthly_close_review",
                        "status": "approved",
                        "close_readiness_status": "close_ready",
                        "exception_count": 0,
                        "unresolved_blocker_count": 0,
                        "period_drift_warn_count": 0,
                        "accounting_packet_ref": "accounting_close_packet_2026-04.zip",
                        "accounting_packet_hash": "stale",
                        "owner": "Finance Owner",
                        "signoff_date": "2026-04-30",
                    }
                ]
            ),
            close_summary={
                "readiness_status": "close_ready",
                "total_exceptions": 0,
                "blocker_count": 0,
                "period_drift_warn_count": 0,
            },
            from_date=date(2026, 4, 1),
            to_date=date(2026, 4, 30),
            current_packet_hash="abc123",
        )
        stale_statuses = {row["check"]: row["status"] for row in stale_review.to_dict("records")}
        self.assertEqual(stale_statuses["Approved Sign-Off Packet Hash"], "warn")

    def test_accounting_close_signoff_review_compares_ai_followup_count(self):
        review = reports._build_accounting_close_signoff_review(
            signoff_df=reports.pd.DataFrame(
                [
                    {
                        "close_period": "2026-04",
                        "signoff_type": "monthly_close_review",
                        "status": "approved",
                        "close_readiness_status": "blocked",
                        "exception_count": 0,
                        "unresolved_blocker_count": 1,
                        "period_drift_warn_count": 0,
                        "ai_review_followup_count": 0,
                        "accounting_packet_ref": "accounting_close_packet_2026-04.zip",
                        "accounting_packet_hash": "abc123",
                        "owner": "Finance Owner",
                        "signoff_date": "2026-04-30",
                    }
                ]
            ),
            close_summary={
                "readiness_status": "blocked",
                "total_exceptions": 0,
                "blocker_count": 1,
                "period_drift_warn_count": 0,
                "ai_review_followup_count": 1,
            },
            from_date=date(2026, 4, 1),
            to_date=date(2026, 4, 30),
            current_packet_hash="abc123",
        )

        statuses = {row["check"]: row["status"] for row in review.to_dict("records")}
        self.assertEqual(statuses["Approved Sign-Off AI Review Follow-Up Count"], "warn")

    def test_accounting_close_signoff_review_uses_latest_approved_signoff(self):
        review = reports._build_accounting_close_signoff_review(
            signoff_df=reports.pd.DataFrame(
                [
                    {
                        "recorded_at_utc": "2026-04-30T08:00:00+00:00",
                        "close_period": "2026-04",
                        "signoff_type": "monthly_close_review",
                        "status": "approved",
                        "close_readiness_status": "blocked",
                        "exception_count": 1,
                        "unresolved_blocker_count": 1,
                        "period_drift_warn_count": 1,
                        "accounting_packet_ref": "accounting_close_packet_2026-04-old.zip",
                        "accounting_packet_hash": "stale",
                        "owner": "Finance Owner",
                        "signoff_date": "2026-04-30",
                    },
                    {
                        "recorded_at_utc": "2026-05-01T08:00:00+00:00",
                        "close_period": "2026-04",
                        "signoff_type": "monthly_close_review",
                        "status": "approved",
                        "close_readiness_status": "close_ready",
                        "exception_count": 0,
                        "unresolved_blocker_count": 0,
                        "period_drift_warn_count": 0,
                        "accounting_packet_ref": "accounting_close_packet_2026-04.zip",
                        "accounting_packet_hash": "abc123",
                        "owner": "Finance Owner",
                        "signoff_date": "2026-05-01",
                    },
                ]
            ),
            close_summary={
                "readiness_status": "close_ready",
                "total_exceptions": 0,
                "blocker_count": 0,
                "period_drift_warn_count": 0,
            },
            from_date=date(2026, 4, 1),
            to_date=date(2026, 4, 30),
            current_packet_hash="abc123",
        )

        statuses = {row["check"]: row["status"] for row in review.to_dict("records")}
        self.assertEqual(statuses["Approved Sign-Off Readiness Match"], "pass")
        self.assertEqual(statuses["Approved Sign-Off Packet Hash"], "pass")

    def test_accounting_close_signoff_review_flags_missing_owner_and_date(self):
        review = reports._build_accounting_close_signoff_review(
            signoff_df=reports.pd.DataFrame(
                [
                    {
                        "close_period": "2026-04",
                        "signoff_type": "monthly_close_review",
                        "status": "approved",
                        "close_readiness_status": "close_ready",
                        "exception_count": 0,
                        "unresolved_blocker_count": 0,
                        "period_drift_warn_count": 0,
                        "accounting_packet_ref": "accounting_close_packet_2026-04.zip",
                        "accounting_packet_hash": "abc123",
                    }
                ]
            ),
            close_summary={
                "readiness_status": "close_ready",
                "total_exceptions": 0,
                "blocker_count": 0,
                "period_drift_warn_count": 0,
            },
            from_date=date(2026, 4, 1),
            to_date=date(2026, 4, 30),
            current_packet_hash="abc123",
        )

        statuses = {row["check"]: row["status"] for row in review.to_dict("records")}
        self.assertEqual(statuses["Approved Sign-Off Owner Present"], "warn")
        self.assertEqual(statuses["Approved Sign-Off Date Present"], "warn")
        self.assertEqual(statuses["Approved Sign-Off Date Validity"], "info")

    def test_accounting_close_signoff_review_flags_invalid_signoff_dates(self):
        def _statuses(signoff_date: str) -> dict[str, str]:
            review = reports._build_accounting_close_signoff_review(
                signoff_df=reports.pd.DataFrame(
                    [
                        {
                            "close_period": "2026-04",
                            "signoff_type": "monthly_close_review",
                            "status": "approved",
                            "close_readiness_status": "close_ready",
                            "exception_count": 0,
                            "unresolved_blocker_count": 0,
                            "period_drift_warn_count": 0,
                            "accounting_packet_ref": "accounting_close_packet_2026-04.zip",
                            "accounting_packet_hash": "abc123",
                            "owner": "Finance Owner",
                            "signoff_date": signoff_date,
                        }
                    ]
                ),
                close_summary={
                    "readiness_status": "close_ready",
                    "total_exceptions": 0,
                    "blocker_count": 0,
                    "period_drift_warn_count": 0,
                },
                from_date=date(2026, 4, 1),
                to_date=date(2026, 4, 30),
                current_packet_hash="abc123",
            )
            return {row["check"]: row["status"] for row in review.to_dict("records")}

        self.assertEqual(_statuses("2026-04-29")["Approved Sign-Off Date Validity"], "warn")
        self.assertEqual(_statuses("not-a-date")["Approved Sign-Off Date Validity"], "warn")
        self.assertEqual(_statuses("2999-01-01")["Approved Sign-Off Date Validity"], "warn")

    def test_build_accounting_close_export_packet_includes_core_evidence(self):
        packet = reports._build_accounting_close_export_packet(
            reports=[
                (
                    "Sales Detail",
                    reports.pd.DataFrame({"sale_id": [1], "gross_sales": [100.0]}),
                    "sales_detail",
                ),
                (
                    "Accounting Exception Queue",
                    reports.pd.DataFrame({"severity": ["P0"], "exception_type": ["missing_cost_basis"]}),
                    "accounting_exception_queue",
                ),
                (
                    "Accounting Close Readiness Checks",
                    reports.pd.DataFrame({"check": ["P0 Exceptions"], "status": ["fail"], "value": [1]}),
                    "accounting_close_readiness_checks",
                ),
                (
                    "Accounting Close Formula Checks",
                    reports.pd.DataFrame({"check": ["net_before_cogs_minus_fifo_cogs_equals_fifo_margin"], "status": ["pass"]}),
                    "accounting_close_formula_checks",
                ),
                (
                    "Accounting Sales Component Checks",
                    reports.pd.DataFrame({"check": ["sales_detail_net_matches_cogs_margin"], "status": ["pass"]}),
                    "accounting_sales_component_checks",
                ),
                (
                    "Accounting Return Tie-Out Checks",
                    reports.pd.DataFrame({"check": ["return_profit_impact_matches_close_summary"], "status": ["pass"]}),
                    "accounting_return_tieout_checks",
                ),
                (
                    "Accounting Inventory Valuation Checks",
                    reports.pd.DataFrame({"check": ["close_inventory_value_matches_inventory_snapshot"], "status": ["pass"]}),
                    "accounting_inventory_valuation_checks",
                ),
                (
                    "Accounting Fee Evidence Checks",
                    reports.pd.DataFrame({"check": ["fee_reconciliation_total_matches_sales_detail"], "status": ["pass"]}),
                    "accounting_fee_evidence_checks",
                ),
                (
                    "Accounting Shipping Evidence Checks",
                    reports.pd.DataFrame({"check": ["shipping_economics_delta_formula"], "status": ["pass"]}),
                    "accounting_shipping_evidence_checks",
                ),
                (
                    "Accounting Reconciliation Tie-Out Checks",
                    reports.pd.DataFrame({"check": ["reconciliation_sales_gross_matches_sales_detail"], "status": ["pass"]}),
                    "accounting_reconciliation_tieout_checks",
                ),
                (
                    "Accounting COGS Source Checks",
                    reports.pd.DataFrame({"check": ["cogs_source_fifo_cogs_matches_close_summary"], "status": ["pass"]}),
                    "accounting_cogs_source_checks",
                ),
                (
                    "Accounting Lot Allocation Checks",
                    reports.pd.DataFrame({"check": ["lot_allocation_resolved_cost_matches_detail"], "status": ["pass"]}),
                    "accounting_lot_allocation_checks",
                ),
                (
                    "Accounting Exception Queue Checks",
                    reports.pd.DataFrame({"check": ["p0_exception_count_matches_close_summary"], "status": ["pass"]}),
                    "accounting_exception_queue_checks",
                ),
                (
                    "Accounting Margin Anomaly Checks",
                    reports.pd.DataFrame({"check": ["nonpositive_fifo_margin_rows_have_exception"], "status": ["pass"]}),
                    "accounting_margin_anomaly_checks",
                ),
                (
                    "Accounting Close Consistency Checks",
                    reports.pd.DataFrame({"check": ["blocker_count_matches_blocker_list"], "status": ["pass"]}),
                    "accounting_close_consistency_checks",
                ),
                (
                    "Accounting Close Packet Completeness Checks",
                    reports.pd.DataFrame({"artifact": ["sales_detail.csv"], "status": ["pass"]}),
                    "accounting_close_packet_completeness_checks",
                ),
                (
                    "Accounting Close Packet Manifest Checks",
                    reports.pd.DataFrame({"artifact": ["sales_detail.csv"], "status": ["pass"]}),
                    "accounting_close_packet_manifest_checks",
                ),
                (
                    "Accounting Close Packet Hash Checks",
                    reports.pd.DataFrame({"artifact": ["sales_detail.csv"], "status": ["pass"]}),
                    "accounting_close_packet_hash_checks",
                ),
                (
                    "Accounting Close Packet Evidence Hash",
                    reports.pd.DataFrame({"hash_key": ["accounting_close_packet_evidence_hash_sha256"], "sha256": ["a" * 64]}),
                    "accounting_close_packet_evidence_hash",
                ),
                (
                    "Accounting Close Sign-Off Evidence",
                    reports.pd.DataFrame({"close_period": ["2026-04"], "status": ["approved"]}),
                    "accounting_close_signoffs",
                ),
                (
                    "Accounting Close Sign-Off Review",
                    reports.pd.DataFrame({"check": ["Approved Sign-Off Readiness Match"], "status": ["pass"]}),
                    "accounting_close_signoff_review",
                ),
                (
                    "Tax Exceptions / Advisor Review",
                    reports.pd.DataFrame({"exception_type": ["missing_tax_jurisdiction"]}),
                    "tax_exceptions_advisor_review",
                ),
                (
                    "Tax Reporting Sign-Off Evidence",
                    reports.pd.DataFrame({"tax_period": ["2026-04"], "status": ["approved"]}),
                    "tax_reporting_signoffs",
                ),
                (
                    "Tax Reporting Sign-Off Review",
                    reports.pd.DataFrame({"check": ["Approved Tax Sign-Off Packet Hash"], "status": ["pass"]}),
                    "tax_reporting_signoff_review",
                ),
                (
                    "AI Review Outcome Evidence",
                    reports.pd.DataFrame({"review_type": ["ai_accountant_review"], "outcome": ["accepted"]}),
                    "ai_review_outcomes",
                ),
                (
                    "Unrelated",
                    reports.pd.DataFrame({"x": [1]}),
                    "not_in_close_packet",
                ),
            ],
            close_summary={
                "readiness_status": "blocked",
                "p0_exceptions": 1,
                "net_after_returns_and_cogs": 30.0,
            },
            from_date="2026-04-01",
            to_date="2026-04-26",
        )

        with zipfile.ZipFile(BytesIO(packet), mode="r") as zf:
            names = set(zf.namelist())
            self.assertIn("manifest.csv", names)
            self.assertIn("README.txt", names)
            self.assertIn("sales_detail.csv", names)
            self.assertIn("accounting_exception_queue.csv", names)
            self.assertIn("accounting_close_readiness_checks.csv", names)
            self.assertIn("accounting_close_formula_checks.csv", names)
            self.assertIn("accounting_sales_component_checks.csv", names)
            self.assertIn("accounting_return_tieout_checks.csv", names)
            self.assertIn("accounting_inventory_valuation_checks.csv", names)
            self.assertIn("accounting_fee_evidence_checks.csv", names)
            self.assertIn("accounting_shipping_evidence_checks.csv", names)
            self.assertIn("accounting_reconciliation_tieout_checks.csv", names)
            self.assertIn("accounting_cogs_source_checks.csv", names)
            self.assertIn("accounting_lot_allocation_checks.csv", names)
            self.assertIn("accounting_exception_queue_checks.csv", names)
            self.assertIn("accounting_margin_anomaly_checks.csv", names)
            self.assertIn("accounting_close_consistency_checks.csv", names)
            self.assertIn("accounting_close_packet_completeness_checks.csv", names)
            self.assertIn("accounting_close_packet_manifest_checks.csv", names)
            self.assertIn("accounting_close_packet_hash_checks.csv", names)
            self.assertIn("accounting_close_packet_evidence_hash.csv", names)
            self.assertIn("accounting_close_signoffs.csv", names)
            self.assertIn("accounting_close_signoff_review.csv", names)
            self.assertIn("tax_exceptions_advisor_review.csv", names)
            self.assertIn("tax_reporting_signoffs.csv", names)
            self.assertIn("tax_reporting_signoff_review.csv", names)
            self.assertIn("ai_review_outcomes.csv", names)
            self.assertNotIn("not_in_close_packet.csv", names)
            manifest = zf.read("manifest.csv").decode("utf-8")
            self.assertIn("readiness_status,blocked", manifest)
            self.assertIn("net_after_returns_and_cogs,30.0", manifest)
            self.assertIn("accounting_close_packet_evidence_hash_sha256,", manifest)
            self.assertIn("sha256_sales_detail,", manifest)
            readme = zf.read("README.txt").decode("utf-8")
            self.assertIn("Profit convention before returns", readme)
            self.assertIn("Estimated profit after returns", readme)
            self.assertIn("profit_before_returns_estimate", readme)

    def test_accounting_close_packet_evidence_hash_is_stable_for_same_inputs(self):
        kwargs = {
            "reports": [
                (
                    "Sales Detail",
                    reports.pd.DataFrame({"sale_id": [1], "gross_sales": [100.0]}),
                    "sales_detail",
                ),
                (
                    "Accounting Close Readiness Checks",
                    reports.pd.DataFrame({"check": ["P0 Exceptions"], "status": ["pass"], "value": [0]}),
                    "accounting_close_readiness_checks",
                ),
            ],
            "close_summary": {"readiness_status": "close_ready", "p0_exceptions": 0},
            "from_date": "2026-04-01",
            "to_date": "2026-04-30",
        }
        first = reports._build_accounting_close_export_packet(**kwargs)
        second = reports._build_accounting_close_export_packet(**kwargs)

        def _hash(packet: bytes) -> str:
            with zipfile.ZipFile(BytesIO(packet), mode="r") as zf:
                manifest = zf.read("manifest.csv").decode("utf-8")
            for line in manifest.splitlines():
                if line.startswith("accounting_close_packet_evidence_hash_sha256,"):
                    return line.split(",", 1)[1].strip()
            return ""

        self.assertEqual(_hash(first), _hash(second))
        self.assertEqual(len(_hash(first)), 64)

    def test_build_tax_review_export_packet_includes_tax_evidence(self):
        packet = reports._build_tax_review_export_packet(
            reports=[
                (
                    "Tax Summary (Estimated)",
                    reports.pd.DataFrame({"jurisdiction": ["Golden, Colorado"], "estimated_tax_collected": [7.5]}),
                    "tax_summary_estimated",
                ),
                (
                    "Tax by Marketplace (Estimated)",
                    reports.pd.DataFrame({"marketplace": ["local"], "estimated_tax_collected": [7.5]}),
                    "tax_by_marketplace_estimated",
                ),
                (
                    "Tax Detail (Estimated)",
                    reports.pd.DataFrame({"sale_id": [1], "taxable_subtotal": [100.0]}),
                    "tax_detail_estimated",
                ),
                (
                    "Tax Exceptions / Advisor Review",
                    reports.pd.DataFrame({"exception_type": ["exempt_category_review_needed"]}),
                    "tax_exceptions_advisor_review",
                ),
                (
                    "Tax Reporting Sign-Off Evidence",
                    reports.pd.DataFrame({"tax_period": ["2026-04"], "status": ["approved"]}),
                    "tax_reporting_signoffs",
                ),
                (
                    "Tax Reporting Sign-Off Review",
                    reports.pd.DataFrame({"check": ["Approved Tax Sign-Off Packet Hash"], "status": ["pass"]}),
                    "tax_reporting_signoff_review",
                ),
                (
                    "Sales Detail",
                    reports.pd.DataFrame({"sale_id": [1]}),
                    "sales_detail",
                ),
            ],
            from_date="2026-04-01",
            to_date="2026-04-30",
            tax_jurisdiction="Golden, Colorado",
            tax_rate_percent=7.5,
            shipping_taxable=True,
            marketplace_scope="local",
            facilitator_channels={"ebay"},
            tax_exempt_categories={"bullion", "coins"},
            extra_artifacts=[("colorado_suts_upload_2026-04-01_2026-04-30.xlsx", b"xlsx")],
        )

        with zipfile.ZipFile(BytesIO(packet), mode="r") as zf:
            names = set(zf.namelist())
            self.assertIn("manifest.csv", names)
            self.assertIn("README.txt", names)
            self.assertIn("tax_summary_estimated.csv", names)
            self.assertIn("tax_by_marketplace_estimated.csv", names)
            self.assertIn("tax_detail_estimated.csv", names)
            self.assertIn("tax_exceptions_advisor_review.csv", names)
            self.assertIn("tax_reporting_signoffs.csv", names)
            self.assertIn("tax_reporting_signoff_review.csv", names)
            self.assertIn("colorado_suts_upload_2026-04-01_2026-04-30.xlsx", names)
            self.assertNotIn("sales_detail.csv", names)
            manifest = zf.read("manifest.csv").decode("utf-8")
            self.assertIn("tax_jurisdiction,\"Golden, Colorado\"", manifest)
            self.assertIn("shipping_taxable,true", manifest)
            self.assertIn("tax_packet_evidence_hash_sha256,", manifest)
            self.assertIn("sha256_colorado_suts_upload_2026-04-01_2026-04-30.xlsx,", manifest)
            readme = zf.read("README.txt").decode("utf-8")
            self.assertIn("tax advisor", readme)

    def test_tax_review_packet_evidence_hash_is_stable_for_same_inputs(self):
        kwargs = {
            "reports": [
                (
                    "Tax Detail (Estimated)",
                    reports.pd.DataFrame({"sale_id": [1], "taxable_subtotal": [100.0]}),
                    "tax_detail_estimated",
                )
            ],
            "from_date": "2026-04-01",
            "to_date": "2026-04-30",
            "tax_jurisdiction": "Golden, Colorado",
            "tax_rate_percent": 7.5,
            "shipping_taxable": True,
            "marketplace_scope": "local",
            "facilitator_channels": {"ebay"},
            "tax_exempt_categories": {"bullion", "coins"},
        }
        first = reports._build_tax_review_export_packet(**kwargs)
        second = reports._build_tax_review_export_packet(**kwargs)

        def _hash(packet: bytes) -> str:
            with zipfile.ZipFile(BytesIO(packet), mode="r") as zf:
                manifest = zf.read("manifest.csv").decode("utf-8")
            for line in manifest.splitlines():
                if line.startswith("tax_packet_evidence_hash_sha256,"):
                    return line.split(",", 1)[1].strip()
            return ""

        self.assertEqual(_hash(first), _hash(second))
        self.assertEqual(len(_hash(first)), 64)

    def test_tax_review_packet_hash_helper_matches_manifest(self):
        kwargs = {
            "reports": [
                (
                    "Tax Detail (Estimated)",
                    reports.pd.DataFrame({"sale_id": [1], "taxable_subtotal": [100.0]}),
                    "tax_detail_estimated",
                )
            ],
            "from_date": "2026-04-01",
            "to_date": "2026-04-30",
            "tax_jurisdiction": "Golden, Colorado",
            "tax_rate_percent": 7.5,
            "shipping_taxable": True,
            "marketplace_scope": "local",
            "facilitator_channels": {"ebay"},
            "tax_exempt_categories": {"bullion", "coins"},
        }
        packet = reports._build_tax_review_export_packet(**kwargs)
        helper_hash = reports._tax_review_packet_evidence_hash_from_reports(**kwargs)

        with zipfile.ZipFile(BytesIO(packet), mode="r") as zf:
            manifest = zf.read("manifest.csv").decode("utf-8")
        manifest_hash = ""
        for line in manifest.splitlines():
            if line.startswith("tax_packet_evidence_hash_sha256,"):
                manifest_hash = line.split(",", 1)[1].strip()
                break

        self.assertEqual(helper_hash, manifest_hash)
        self.assertEqual(len(helper_hash), 64)

    def test_tax_review_packet_manifest_includes_selected_tax_profile(self):
        packet = reports._build_tax_review_export_packet(
            reports=[
                (
                    "Tax Detail (Estimated)",
                    reports.pd.DataFrame({"sale_id": [1], "taxable_subtotal": [100.0]}),
                    "tax_detail_estimated",
                )
            ],
            from_date="2026-04-01",
            to_date="2026-04-30",
            tax_jurisdiction="Golden, Colorado",
            tax_rate_percent=7.5,
            shipping_taxable=True,
            marketplace_scope="local",
            facilitator_channels={"ebay"},
            tax_exempt_categories={"bullion", "coins"},
            tax_profile={
                "profile_key": "local_default",
                "profile_name": "Local default",
                "human_validation_status": "advisor_validated",
                "advisor_evidence_link": "ticket-123",
            },
        )

        with zipfile.ZipFile(BytesIO(packet), mode="r") as zf:
            manifest = zf.read("manifest.csv").decode("utf-8")
            self.assertIn("tax_profile_profile_key,local_default", manifest)
            self.assertIn("tax_profile_human_validation_status,advisor_validated", manifest)

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

    def test_sale_net_before_cogs_uses_shipping_charged_minus_label_spend(self):
        sale = SimpleNamespace(
            sold_price=100.0,
            shipping_cost=8.0,
            fees=12.0,
            shipping_label_cost=5.0,
        )
        self.assertAlmostEqual(reports._sale_net_before_cogs_from_fields(sale), 91.0)

    def test_qbo_sales_export_prefers_actual_economics_allocations(self):
        sale = SimpleNamespace(
            id=11,
            sold_at=datetime(2026, 1, 5),
            external_order_id="EO-11",
            marketplace="ebay",
            product=SimpleNamespace(sku="SKU-1", title="Item"),
            listing=SimpleNamespace(
                marketplace_details=json.dumps(
                    {
                        "bundle": {
                            "enabled": True,
                            "kind": "mixed_product_bundle",
                            "components": [
                                {"product_id": 1, "quantity_per_listing": 2},
                                {"product_id": 2, "quantity_per_listing": 3},
                            ],
                        }
                    }
                )
            ),
            quantity_sold=1,
            sold_price=100.0,
            fees=10.0,
            shipping_cost=4.0,
            shipping_label_cost=5.0,
            tracking_number="TRK",
            tracking_status="delivered",
        )
        rows = reports._build_qbo_sales_export_rows(
            [sale],
            {11: 40.0},
            fifo_unit_cost_source_by_sale={11: "lot_expected_quantity_fallback"},
            actual_econ_by_sale_id={
                11: {
                    "allocated_fee_actual": 7.5,
                    "allocated_shipping_charged": 4.0,
                    "allocated_shipping_actual": 4.25,
                    "net_before_cogs_actual": 92.25,
                    "actual_fee_source": "normalized_order_finance_entries_marketplace_fee_sum",
                    "actual_shipping_source": "normalized_order_finance_entries_shipping_label_sum",
                }
            },
        )
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["fees"], 7.5)
        self.assertAlmostEqual(rows[0]["shipping_label_cost"], 4.25)
        self.assertAlmostEqual(rows[0]["net_amount"], 92.25)
        self.assertAlmostEqual(rows[0]["gross_margin_estimate"], 52.25)
        self.assertAlmostEqual(rows[0]["profit_before_returns_estimate"], 52.25)
        self.assertEqual(rows[0]["cogs_source"], "lot_expected_quantity_fallback")
        self.assertEqual(rows[0]["fee_source"], "normalized_order_finance_entries_marketplace_fee_sum")
        self.assertEqual(rows[0]["item_product_source"], "sale_product")
        self.assertTrue(rows[0]["listing_is_bundle"])
        self.assertEqual(rows[0]["listing_bundle_kind"], "mixed_product_bundle")
        self.assertEqual(rows[0]["listing_bundle_component_count"], 2)
        self.assertEqual(rows[0]["listing_bundle_units_per_listing"], 5)
        self.assertEqual(rows[0]["listing_bundle_inventory_units_sold"], 5)

    def test_report_context_caption_explains_qbo_profit_fields(self):
        caption = reports._report_context_caption("qbo_sales_export")
        self.assertIn("profit_before_returns_estimate", caption)
        self.assertIn("gross_margin_estimate", caption)

        cogs_caption = reports._report_context_caption("cogs_margin_detail")
        self.assertIn("before-return profit", cogs_caption)
        self.assertIn("Est. Profit After Returns", cogs_caption)

    def test_qbo_sales_export_uses_listing_product_for_productless_bundle_sale(self):
        sale = SimpleNamespace(
            id=11,
            sold_at=datetime(2026, 1, 5),
            external_order_id="EO-11",
            marketplace="ebay",
            product=None,
            listing=SimpleNamespace(
                product=SimpleNamespace(sku="SKU-LISTING", title="Listing Product"),
                marketplace_details=json.dumps(
                    {
                        "bundle": {
                            "enabled": True,
                            "kind": "mixed_product_bundle",
                            "components": [
                                {"product_id": 1, "quantity_per_listing": 2},
                                {"product_id": 2, "quantity_per_listing": 3},
                            ],
                        }
                    }
                ),
            ),
            quantity_sold=1,
            sold_price=100.0,
            fees=10.0,
            shipping_cost=4.0,
            shipping_label_cost=5.0,
            tracking_number="TRK",
            tracking_status="delivered",
        )

        rows = reports._build_qbo_sales_export_rows(
            [sale],
            {11: 40.0},
            fifo_unit_cost_source_by_sale={11: "mixed_fifo_cost"},
        )

        self.assertEqual(rows[0]["item_sku"], "SKU-LISTING")
        self.assertEqual(rows[0]["item_description"], "Listing Product")
        self.assertEqual(rows[0]["item_product_source"], "listing_product")
        self.assertTrue(rows[0]["listing_is_bundle"])
        self.assertEqual(rows[0]["cogs_source"], "mixed_fifo_cost")

    def test_qbo_adjustment_export_includes_return_cogs_reversal_source(self):
        rows = reports._build_qbo_adjustment_export_rows(
            [
                {
                    "return_id": 5,
                    "returned_at": "2026-01-09T12:00:00",
                    "external_return_id": "RET-5",
                    "source_order": "EO-5",
                    "marketplace": "ebay",
                    "sku": "SKU-5",
                    "reason": "buyer_return",
                    "sale_id": 11,
                    "quantity": 2,
                    "refund_amount": 30.0,
                    "refund_fees": 2.0,
                    "refund_shipping": 1.0,
                    "status": "processed",
                    "restocked": True,
                    "listing_is_bundle": True,
                    "listing_bundle_kind": "mixed_product_bundle",
                    "listing_bundle_component_count": 2,
                    "listing_bundle_units_per_return": 5,
                    "listing_bundle_inventory_units_returned": 10,
                }
            ],
            {11: 12.5},
            fifo_unit_cost_source_by_sale={11: "lot_expected_quantity_fallback"},
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cogs_source"], "lot_expected_quantity_fallback")
        self.assertEqual(rows[0]["returned_listing_units"], 2)
        self.assertEqual(rows[0]["returned_inventory_units"], 10)
        self.assertAlmostEqual(rows[0]["cogs_per_returned_listing"], 12.5)
        self.assertAlmostEqual(rows[0]["returned_cogs_estimate"], 25.0)
        self.assertAlmostEqual(rows[0]["cogs_reversal_estimate"], 25.0)
        self.assertAlmostEqual(rows[0]["net_adjustment"], -33.0)
        self.assertAlmostEqual(rows[0]["estimated_profit_impact"], -8.0)
        self.assertTrue(rows[0]["listing_is_bundle"])
        self.assertEqual(rows[0]["listing_bundle_kind"], "mixed_product_bundle")
        self.assertEqual(rows[0]["listing_bundle_component_count"], 2)
        self.assertEqual(rows[0]["listing_bundle_units_per_return"], 5)
        self.assertEqual(rows[0]["listing_bundle_inventory_units_returned"], 10)

    def test_qbo_adjustment_export_uses_listing_product_for_productless_bundle_return(self):
        ret = SimpleNamespace(
            id=5,
            returned_at=datetime(2026, 1, 9, 12, 0, 0),
            external_return_id="RET-5",
            marketplace="ebay",
            product=None,
            sale=SimpleNamespace(
                external_order_id="EO-5",
                listing=SimpleNamespace(
                    product=SimpleNamespace(sku="SKU-LISTING", title="Listing Product"),
                    marketplace_details=json.dumps(
                        {
                            "bundle": {
                                "enabled": True,
                                "kind": "mixed_product_bundle",
                                "components": [
                                    {"product_id": 1, "quantity_per_listing": 2},
                                    {"product_id": 2, "quantity_per_listing": 3},
                                ],
                            }
                        }
                    ),
                ),
            ),
            sale_id=11,
            quantity=1,
            refund_amount=30.0,
            refund_fees=2.0,
            refund_shipping=1.0,
            reason="buyer_return",
            return_status="processed",
            restocked=True,
        )

        rows = reports._build_qbo_adjustment_export_rows(
            [ret],
            {11: 40.0},
            fifo_unit_cost_source_by_sale={11: "mixed_fifo_cost"},
        )

        self.assertEqual(rows[0]["source_order"], "EO-5")
        self.assertEqual(rows[0]["sku"], "SKU-LISTING")
        self.assertEqual(rows[0]["sku_source"], "listing_product")
        self.assertTrue(rows[0]["listing_is_bundle"])
        self.assertEqual(rows[0]["listing_bundle_inventory_units_returned"], 5)
        self.assertEqual(rows[0]["cogs_source"], "mixed_fifo_cost")

    def test_marketplace_reconciliation_fallback_prefers_actual_economics(self):
        sale = SimpleNamespace(
            id=11,
            marketplace="ebay",
            sold_price=100.0,
            fees=10.0,
            shipping_cost=5.0,
            shipping_label_cost=9.0,
        )
        order = SimpleNamespace(marketplace="ebay", total_amount=105.0)
        returns_df = reports.pd.DataFrame(
            [{"marketplace": "ebay", "refund_amount": 10.0, "refund_fees": 1.0, "refund_shipping": 2.0}]
        )

        rows = reports._build_marketplace_reconciliation_fallback_rows(
            [sale],
            [order],
            returns_df,
            actual_econ_by_sale_id={
                11: {
                    "allocated_fee_actual": 7.5,
                    "allocated_shipping_charged": 5.0,
                    "allocated_shipping_actual": 4.25,
                    "net_before_cogs_actual": 93.25,
                }
            },
        )

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertAlmostEqual(row["sales_fees"], 7.5)
        self.assertAlmostEqual(row["sales_shipping_cost"], 5.0)
        self.assertAlmostEqual(row["sales_shipping_label_cost"], 4.25)
        self.assertAlmostEqual(row["sales_net_before_returns"], 93.25)
        self.assertAlmostEqual(row["net_after_returns"], 80.25)

    def test_build_fifo_unit_cost_map_ignores_lots_acquired_after_sale(self):
        assignments = [
            SimpleNamespace(id=1, product_id=1, acquired_at=datetime(2026, 1, 1), quantity_acquired=1, unit_cost=10.0, allocated_cost=None),
            SimpleNamespace(id=2, product_id=1, acquired_at=datetime(2026, 1, 3), quantity_acquired=3, unit_cost=20.0, allocated_cost=None),
        ]
        sales = [
            SimpleNamespace(id=11, product_id=1, sold_at=datetime(2026, 1, 2), quantity_sold=2),
            SimpleNamespace(id=12, product_id=1, sold_at=datetime(2026, 1, 4), quantity_sold=1),
        ]
        out = reports._build_fifo_unit_cost_map(sales, assignments, {1: 0.0})
        remaining = reports._build_fifo_remaining_unit_cost_map(sales, assignments, {1: 0.0})
        self.assertAlmostEqual(out[11], 5.0)
        self.assertAlmostEqual(out[12], 20.0)
        self.assertAlmostEqual(remaining[1], 20.0)

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

    def test_lot_total_cost_allocates_when_assignment_costs_are_blank(self):
        lot = SimpleNamespace(
            id=7,
            total_cost=120.0,
            total_tax_paid=12.0,
            total_shipping_paid=0.0,
            total_handling_paid=0.0,
        )
        assignments = [
            SimpleNamespace(
                id=1,
                product_id=1,
                lot_id=7,
                lot=lot,
                acquired_at=datetime(2026, 1, 1),
                quantity_acquired=2,
                unit_cost=None,
                unit_tax_paid=None,
                unit_shipping_paid=None,
                unit_handling_paid=None,
                allocated_cost=None,
                allocated_tax_paid=None,
                allocated_shipping_paid=None,
                allocated_handling_paid=None,
            ),
            SimpleNamespace(
                id=2,
                product_id=2,
                lot_id=7,
                lot=lot,
                acquired_at=datetime(2026, 1, 1),
                quantity_acquired=4,
                unit_cost=None,
                unit_tax_paid=None,
                unit_shipping_paid=None,
                unit_handling_paid=None,
                allocated_cost=None,
                allocated_tax_paid=None,
                allocated_shipping_paid=None,
                allocated_handling_paid=None,
            ),
        ]
        out = reports._build_lot_weighted_unit_cost_map(assignments, {})
        self.assertAlmostEqual(out[1], 22.0)
        self.assertAlmostEqual(out[2], 22.0)

    def test_lot_total_cost_allocates_only_remaining_cost_to_blank_assignments(self):
        lot = SimpleNamespace(
            id=8,
            total_cost=100.0,
            total_tax_paid=0.0,
            total_shipping_paid=0.0,
            total_handling_paid=0.0,
        )
        assignments = [
            SimpleNamespace(
                id=1,
                product_id=1,
                lot_id=8,
                lot=lot,
                acquired_at=datetime(2026, 1, 1),
                quantity_acquired=2,
                unit_cost=30.0,
                unit_tax_paid=None,
                unit_shipping_paid=None,
                unit_handling_paid=None,
                allocated_cost=None,
                allocated_tax_paid=None,
                allocated_shipping_paid=None,
                allocated_handling_paid=None,
            ),
            SimpleNamespace(
                id=2,
                product_id=2,
                lot_id=8,
                lot=lot,
                acquired_at=datetime(2026, 1, 1),
                quantity_acquired=4,
                unit_cost=None,
                unit_tax_paid=None,
                unit_shipping_paid=None,
                unit_handling_paid=None,
                allocated_cost=None,
                allocated_tax_paid=None,
                allocated_shipping_paid=None,
                allocated_handling_paid=None,
            ),
        ]
        out = reports._build_lot_weighted_unit_cost_map(assignments, {})
        self.assertAlmostEqual(out[1], 30.0)
        self.assertAlmostEqual(out[2], 10.0)

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
            SimpleNamespace(
                id=11,
                product_id=1,
                sold_at=datetime(2026, 1, 1, 12),
                quantity_sold=1,
                sold_price=25.0,
                fees=1.0,
                shipping_cost=2.0,
                shipping_label_cost=1.0,
            ),
            SimpleNamespace(id=12, product_id=2, sold_at=datetime(2026, 1, 4), quantity_sold=1, sold_price=10.0, fees=0.5, shipping_cost=1.0),
        ]
        rows = reports._build_inventory_cycle_rows(products, movements, sales)
        self.assertEqual(len(rows), 2)
        closed = next(r for r in rows if r["sku"] == "SKU1")
        self.assertEqual(closed["cycle_status"], "closed")
        self.assertEqual(closed["sale_count"], 1)
        self.assertAlmostEqual(closed["shipping_label_cost"], 1.0)
        self.assertAlmostEqual(closed["net_sales"], 25.0)
        self.assertAlmostEqual(closed["estimated_margin_vs_known_cost"], 5.0)

        open_row = next(r for r in rows if r["sku"] == "SKU2")
        self.assertEqual(open_row["cycle_status"], "open")
        self.assertEqual(open_row["qty_in"], 3)

    def test_build_inventory_cycle_rows_prefers_actual_economics(self):
        products = [SimpleNamespace(id=1, sku="SKU1", title="Coin A")]
        movements = [
            SimpleNamespace(
                id=1,
                product_id=1,
                occurred_at=datetime(2026, 1, 1),
                quantity_before=0,
                quantity_after=1,
                quantity_delta=1,
                unit_cost=20.0,
            ),
            SimpleNamespace(
                id=2,
                product_id=1,
                occurred_at=datetime(2026, 1, 2),
                quantity_before=1,
                quantity_after=0,
                quantity_delta=-1,
                unit_cost=None,
            ),
        ]
        sales = [
            SimpleNamespace(
                id=11,
                product_id=1,
                sold_at=datetime(2026, 1, 1, 12),
                quantity_sold=1,
                sold_price=100.0,
                fees=10.0,
                shipping_cost=5.0,
                shipping_label_cost=9.0,
            ),
        ]

        rows = reports._build_inventory_cycle_rows(
            products,
            movements,
            sales,
            actual_econ_by_sale_id={
                11: {
                    "allocated_fee_actual": 7.5,
                    "allocated_shipping_charged": 5.0,
                    "allocated_shipping_actual": 4.25,
                    "net_before_cogs_actual": 93.25,
                }
            },
        )

        row = rows[0]
        self.assertAlmostEqual(row["fees"], 7.5)
        self.assertAlmostEqual(row["shipping_cost"], 5.0)
        self.assertAlmostEqual(row["shipping_label_cost"], 4.25)
        self.assertAlmostEqual(row["net_sales"], 93.25)
        self.assertAlmostEqual(row["estimated_margin_vs_known_cost"], 73.25)

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
