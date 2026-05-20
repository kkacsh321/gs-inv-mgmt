import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def __getattr__(self, _name):
        def _noop(*_args, **_kwargs):
            return None

        return _noop


class _FakeSt:
    def __init__(self):
        self.session_state = {}
        self.calls = []
        self._button_key_map = {}
        self._checkbox_key_map = {}
        self.rerun_called = False

    def set_button_key_value(self, key: str, value: bool):
        self._button_key_map[key] = bool(value)

    def set_checkbox_key_value(self, key: str, value: bool):
        self._checkbox_key_map[key] = bool(value)

    def subheader(self, *a, **k):
        self.calls.append(("subheader", a, k))

    def caption(self, *a, **k):
        self.calls.append(("caption", a, k))

    def markdown(self, *a, **k):
        self.calls.append(("markdown", a, k))

    def info(self, *a, **k):
        self.calls.append(("info", a, k))

    def warning(self, *a, **k):
        self.calls.append(("warning", a, k))

    def success(self, *a, **k):
        self.calls.append(("success", a, k))

    def error(self, *a, **k):
        self.calls.append(("error", a, k))

    def dataframe(self, *a, **k):
        self.calls.append(("dataframe", a, k))

    def download_button(self, *a, **k):
        self.calls.append(("download_button", a, k))

    def code(self, *a, **k):
        self.calls.append(("code", a, k))

    def metric(self, *a, **k):
        self.calls.append(("metric", a, k))

    def line_chart(self, *a, **k):
        self.calls.append(("line_chart", a, k))

    def columns(self, n):
        count = int(n) if isinstance(n, int) else len(n)
        return [_Ctx() for _ in range(count)]

    def expander(self, *_a, **_k):
        return _Ctx()

    def date_input(self, _label, value=None, **_kwargs):
        return value

    def text_input(self, _label, value="", **_kwargs):
        return value

    def number_input(self, _label, min_value=None, value=0.0, step=None, **_kwargs):
        _ = (min_value, step)
        return value

    def checkbox(self, _label, value=False, **_kwargs):
        key = _kwargs.get("key")
        if key in self._checkbox_key_map:
            return bool(self._checkbox_key_map[key])
        return bool(value)

    def selectbox(self, _label, options, index=0, **_kwargs):
        opts = list(options)
        if not opts:
            return None
        idx = max(0, min(int(index), len(opts) - 1))
        return opts[idx]

    def multiselect(self, _label, options, default=None, **_kwargs):
        if default is not None:
            return list(default)
        return list(options)

    def button(self, _label, **kwargs):
        key = kwargs.get("key")
        if key in self._button_key_map:
            return bool(self._button_key_map[key])
        return False

    def rerun(self):
        self.rerun_called = True

    def stop(self):
        raise RuntimeError("streamlit_stop")


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
    spec = importlib.util.spec_from_file_location("test_reports_view_module", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


reports_view = _load_module()


class ReportsViewTests(unittest.TestCase):
    def _repo_stub(self):
        return SimpleNamespace(
            list_products=lambda: [],
            list_listings=lambda: [],
            list_sales=lambda: [],
            list_orders=lambda: [],
            list_order_items=lambda: [],
            list_returns=lambda: [],
            list_product_lot_assignments=lambda: [],
            list_inventory_movements=lambda limit=5000: [],
        )

    def test_render_reports_empty(self):
        fake_st = _FakeSt()
        repo = self._repo_stub()
        user = SimpleNamespace(username="admin", role="admin")
        with patch.object(reports_view, "st", fake_st), patch.object(
            reports_view, "current_user", return_value=user
        ), patch.object(
            reports_view, "render_help_panel", return_value=None
        ), patch.object(
            reports_view, "render_workspace_feedback", return_value=None
        ), patch.object(
            reports_view, "get_runtime_str", return_value=""
        ), patch.object(
            reports_view, "get_runtime_bool", return_value=False
        ), patch.object(
            reports_view, "utc_today", return_value=reports_view.datetime(2026, 4, 30).date()
        ):
            reports_view.render_reports(repo)
        self.assertTrue(any(c[0] == "info" for c in fake_st.calls))

    def test_render_reports_rolls_back_after_failed_optional_rollup(self):
        fake_st = _FakeSt()
        fake_st.set_checkbox_key_value("reports_load_inventory_cycle_analytics", True)
        rollback_calls = []
        inventory_cycle_calls = []

        def _broken_products_rollup(**_kwargs):
            raise RuntimeError("simulated failed rollup")

        repo = self._repo_stub()
        repo.db = SimpleNamespace(rollback=lambda: rollback_calls.append("rollback"))
        repo.report_products_rows = _broken_products_rollup
        repo.report_inventory_cycle_rows = lambda **_kwargs: inventory_cycle_calls.append("cycle") or []

        user = SimpleNamespace(username="admin", role="admin")
        with patch.object(reports_view, "st", fake_st), patch.object(
            reports_view, "current_user", return_value=user
        ), patch.object(
            reports_view, "render_help_panel", return_value=None
        ), patch.object(
            reports_view, "render_workspace_feedback", return_value=None
        ), patch.object(
            reports_view, "get_runtime_str", return_value=""
        ), patch.object(
            reports_view, "get_runtime_bool", return_value=False
        ), patch.object(
            reports_view, "utc_today", return_value=reports_view.datetime(2026, 4, 30).date()
        ):
            reports_view.render_reports(repo)

        self.assertEqual(rollback_calls, ["rollback"])
        self.assertEqual(inventory_cycle_calls, ["cycle"])

    def test_render_reports_cogs_margin_includes_cost_source_columns(self):
        fake_st = _FakeSt()
        product = SimpleNamespace(
            id=1,
            sku="GS-REPORT-COST-SOURCE",
            title="Report Cost Source Coin",
            description="",
            category="bullion",
            metal_type="silver",
            current_quantity=1,
            acquisition_cost=None,
            acquisition_tax_paid=None,
            acquisition_shipping_paid=None,
            acquisition_handling_paid=None,
            product_cost=None,
            acquired_at=None,
            weight_oz=None,
            package_weight_oz=None,
            package_length_in=None,
            package_width_in=None,
            package_height_in=None,
        )
        sale = SimpleNamespace(
            id=11,
            sold_at=reports_view.datetime(2026, 4, 20, 12, 0, 0),
            marketplace="ebay",
            order_id=None,
            product_id=product.id,
            product=product,
            listing_id=None,
            external_order_id="",
            quantity_sold=1,
            sold_price=100.0,
            fees=10.0,
            shipping_cost=5.0,
            shipping_label_cost=4.0,
            shipping_provider="",
            shipping_service="",
            shipping_package_type="",
            tracking_number="",
            tracking_status="",
            shipping_exception_code="",
            shipping_exception_action="",
            shipping_exception_notes="",
            shipping_exception_resolved_at=None,
            shipping_exception_resolved_by="",
            shipment_exported_at=None,
            shipped_at=None,
            delivered_at=None,
        )
        repo = self._repo_stub()
        repo.list_products = lambda: [product]
        repo.list_sales = lambda: [sale]
        repo.report_sale_unit_cost_maps = lambda **_kwargs: {
            "fifo_unit_cost_by_sale": {sale.id: 25.0},
            "fifo_unit_cost_source_by_sale": {sale.id: "lot_expected_quantity_fallback"},
            "fifo_cogs_evidence_by_sale": {
                sale.id: [
                    {
                        "product_id": product.id,
                        "lot_id": 7,
                        "assignment_id": 9,
                        "quantity": 1,
                        "unit_cost": 25.0,
                        "total_cost": 25.0,
                        "cost_source": "lot_expected_quantity_fallback",
                    }
                ]
            },
            "lot_weighted_unit_cost_by_product": {product.id: 25.0},
            "lot_weighted_unit_cost_source_by_product": {product.id: "lot_expected_quantity_fallback"},
            "fifo_remaining_unit_cost_by_product": {product.id: 25.0},
        }

        user = SimpleNamespace(username="admin", role="admin")
        with patch.object(reports_view, "st", fake_st), patch.object(
            reports_view, "current_user", return_value=user
        ), patch.object(
            reports_view, "render_help_panel", return_value=None
        ), patch.object(
            reports_view, "render_workspace_feedback", return_value=None
        ), patch.object(
            reports_view, "get_runtime_str", return_value=""
        ), patch.object(
            reports_view, "get_runtime_bool", return_value=False
        ), patch.object(
            reports_view, "utc_today", return_value=reports_view.datetime(2026, 4, 30).date()
        ):
            reports_view.render_reports(repo)

        dataframes = [call[1][0] for call in fake_st.calls if call[0] == "dataframe" and call[1]]
        cogs_frames = [
            df
            for df in dataframes
            if hasattr(df, "columns") and {"fifo_cost_source", "lot_cost_source"}.issubset(set(df.columns))
        ]
        self.assertTrue(cogs_frames)
        cogs_df = cogs_frames[0]
        self.assertEqual(cogs_df.iloc[0]["fifo_cost_source"], "lot_expected_quantity_fallback")
        self.assertEqual(cogs_df.iloc[0]["lot_cost_source"], "lot_expected_quantity_fallback")
        self.assertEqual(int(cogs_df.iloc[0]["fifo_cogs_evidence_rows"]), 1)
        evidence_frames = [
            df
            for df in dataframes
            if hasattr(df, "columns") and {"sale_id", "lot_id", "assignment_id", "cost_source"}.issubset(set(df.columns))
        ]
        self.assertTrue(evidence_frames)
        evidence_df = evidence_frames[0]
        self.assertEqual(int(evidence_df.iloc[0]["sale_id"]), int(sale.id))
        self.assertEqual(int(evidence_df.iloc[0]["lot_id"]), 7)
        self.assertEqual(evidence_df.iloc[0]["cost_source"], "lot_expected_quantity_fallback")

    def test_render_reports_rolls_back_after_failed_inventory_cycle_rollup(self):
        fake_st = _FakeSt()
        fake_st.set_checkbox_key_value("reports_load_inventory_cycle_analytics", True)
        rollback_calls = []
        rebuy_calls = []

        repo = self._repo_stub()
        repo.db = SimpleNamespace(rollback=lambda: rollback_calls.append("rollback"))
        repo.report_inventory_cycle_rows = lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("cycle failed"))
        repo.report_rebuy_cost_trend_rows = lambda **_kwargs: rebuy_calls.append("rebuy") or []

        user = SimpleNamespace(username="admin", role="admin")
        with patch.object(reports_view, "st", fake_st), patch.object(
            reports_view, "current_user", return_value=user
        ), patch.object(
            reports_view, "render_help_panel", return_value=None
        ), patch.object(
            reports_view, "render_workspace_feedback", return_value=None
        ), patch.object(
            reports_view, "get_runtime_str", return_value=""
        ), patch.object(
            reports_view, "get_runtime_bool", return_value=False
        ):
            reports_view.render_reports(repo)

        self.assertEqual(rollback_calls, ["rollback"])
        self.assertEqual(rebuy_calls, ["rebuy"])

    def test_render_reports_rolls_back_after_failed_extended_analytics_rollup(self):
        fake_st = _FakeSt()
        fake_st.set_checkbox_key_value("reports_load_extended_analytics", True)
        rollback_calls = []
        listing_format_calls = []

        repo = self._repo_stub()
        repo.db = SimpleNamespace(rollback=lambda: rollback_calls.append("rollback"))
        repo.report_listing_review_activity_rows = lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("review failed"))
        repo.report_listing_format_outcome_rows = lambda **_kwargs: listing_format_calls.append("format") or []

        user = SimpleNamespace(username="admin", role="admin")
        with patch.object(reports_view, "st", fake_st), patch.object(
            reports_view, "current_user", return_value=user
        ), patch.object(
            reports_view, "render_help_panel", return_value=None
        ), patch.object(
            reports_view, "render_workspace_feedback", return_value=None
        ), patch.object(
            reports_view, "get_runtime_str", return_value=""
        ), patch.object(
            reports_view, "get_runtime_bool", return_value=False
        ):
            reports_view.render_reports(repo)

        self.assertEqual(rollback_calls, ["rollback"])
        self.assertEqual(listing_format_calls, ["format"])

    def test_render_reports_copilot_permission_denied_and_success(self):
        repo = self._repo_stub()
        user = SimpleNamespace(username="admin", role="admin")

        # Permission denied branch calls st.stop().
        fake_st_denied = _FakeSt()
        fake_st_denied.set_button_key_value("reports_copilot_analyze_btn", True)
        with patch.object(reports_view, "st", fake_st_denied), patch.object(
            reports_view, "current_user", return_value=user
        ), patch.object(
            reports_view, "render_help_panel", return_value=None
        ), patch.object(
            reports_view, "render_workspace_feedback", return_value=None
        ), patch.object(
            reports_view, "get_runtime_str", return_value=""
        ), patch.object(
            reports_view, "get_runtime_bool", return_value=False
        ), patch.object(
            reports_view, "ensure_permission", return_value=False
        ):
            with self.assertRaises(RuntimeError):
                reports_view.render_reports(repo)

        # Success branch sets result and reruns.
        fake_st_ok = _FakeSt()
        fake_st_ok.set_button_key_value("reports_copilot_analyze_btn", True)
        audit_calls = []
        repo.log_ai_chat_interaction = lambda **kwargs: audit_calls.append(kwargs)
        with patch.object(reports_view, "st", fake_st_ok), patch.object(
            reports_view, "current_user", return_value=user
        ), patch.object(
            reports_view, "render_help_panel", return_value=None
        ), patch.object(
            reports_view, "render_workspace_feedback", return_value=None
        ), patch.object(
            reports_view, "get_runtime_str", return_value=""
        ), patch.object(
            reports_view, "get_runtime_bool", return_value=False
        ), patch.object(
            reports_view, "ensure_permission", return_value=True
        ), patch.object(
            reports_view,
            "execute_comp_summary",
            return_value=SimpleNamespace(
                text=(
                    '{"executive_summary":["Review needed"],'
                    '"tax_review_findings":["Tax sign-off evidence matches current packet"],'
                    '"next_actions":["Record advisor link"]}'
                )
            ),
        ) as execute_summary:
            reports_view.render_reports(repo)
        self.assertIn("reports_copilot_raw", fake_st_ok.session_state)
        rendered_markdown = "\n".join(str(args[0]) for name, args, _kwargs in fake_st_ok.calls if name == "markdown" and args)
        self.assertIn("Tax Review Findings", rendered_markdown)
        self.assertIn("Tax sign-off evidence matches current packet", rendered_markdown)
        copilot_context = execute_summary.call_args.kwargs["spot_context"]
        copilot_instruction = execute_summary.call_args.kwargs["instruction"]
        self.assertIn("tax_review_summary", copilot_context)
        self.assertIn("tax_profile_evidence", copilot_context)
        self.assertIn("tax_reporting_signoff_rows", copilot_context)
        self.assertIn("tax_reporting_signoff_review", copilot_context)
        self.assertIn("tax_review_findings", copilot_instruction)
        self.assertEqual(len(audit_calls), 1)
        self.assertEqual(audit_calls[0]["intent"], "reports_copilot_review")
        self.assertEqual(audit_calls[0]["metadata"]["event_type"], "reports_copilot_review")
        self.assertEqual(len(audit_calls[0]["metadata"]["prompt_hash_sha256"]), 64)
        self.assertEqual(len(audit_calls[0]["metadata"]["data_scope_hash_sha256"]), 64)
        self.assertIn("reports_copilot_metadata", fake_st_ok.session_state)
        self.assertEqual(audit_calls[0]["metadata"]["data_scope"]["date_range"], copilot_context["date_range"])
        self.assertIn("tax", audit_calls[0]["allowed_domains"])
        citation_tables = {row["table"] for row in audit_calls[0]["citations"]}
        self.assertIn("tax_reporting_signoff_review", citation_tables)
        self.assertIn("accounting_period_drift_checks", citation_tables)
        self.assertTrue(fake_st_ok.rerun_called)

    def test_render_reports_accountant_permission_denied_and_success(self):
        repo = self._repo_stub()
        user = SimpleNamespace(username="admin", role="admin")

        fake_st_denied = _FakeSt()
        fake_st_denied.set_checkbox_key_value("reports_load_extended_analytics", True)
        fake_st_denied.set_button_key_value("reports_accountant_review_btn", True)
        with patch.object(reports_view, "st", fake_st_denied), patch.object(
            reports_view, "current_user", return_value=user
        ), patch.object(
            reports_view, "render_help_panel", return_value=None
        ), patch.object(
            reports_view, "render_workspace_feedback", return_value=None
        ), patch.object(
            reports_view, "get_runtime_str", return_value=""
        ), patch.object(
            reports_view, "get_runtime_bool", return_value=False
        ), patch.object(
            reports_view, "ensure_permission", return_value=False
        ):
            with self.assertRaises(RuntimeError):
                reports_view.render_reports(repo)

        fake_st_ok = _FakeSt()
        fake_st_ok.set_checkbox_key_value("reports_load_extended_analytics", True)
        fake_st_ok.set_button_key_value("reports_accountant_review_btn", True)
        audit_calls = []
        repo.log_ai_chat_interaction = lambda **kwargs: audit_calls.append(kwargs)
        with patch.object(reports_view, "st", fake_st_ok), patch.object(
            reports_view, "current_user", return_value=user
        ), patch.object(
            reports_view, "render_help_panel", return_value=None
        ), patch.object(
            reports_view, "render_workspace_feedback", return_value=None
        ), patch.object(
            reports_view, "get_runtime_str", return_value=""
        ), patch.object(
            reports_view, "get_runtime_bool", return_value=False
        ), patch.object(
            reports_view, "ensure_permission", return_value=True
        ), patch.object(
            reports_view,
            "execute_comp_summary",
            return_value=SimpleNamespace(
                text=(
                    '{"close_status":"review_needed",'
                    '"profit_basis_notes":["FIFO lot basis used"],'
                    '"unsupported_tax_or_legal_items":["Confirm bullion exemption with advisor"]}'
                ),
                citation={"provider": "test"},
            ),
        ) as execute_summary:
            reports_view.render_reports(repo)
        self.assertIn("reports_accountant_raw", fake_st_ok.session_state)
        rendered_markdown = "\n".join(str(args[0]) for name, args, _kwargs in fake_st_ok.calls if name == "markdown" and args)
        self.assertIn("Profit Basis Notes", rendered_markdown)
        self.assertIn("FIFO lot basis used", rendered_markdown)
        self.assertIn("Unsupported Tax or Legal Items", rendered_markdown)
        self.assertIn("Confirm bullion exemption with advisor", rendered_markdown)
        self.assertEqual(execute_summary.call_count, 1)
        accountant_context = execute_summary.call_args.kwargs["spot_context"]
        self.assertIn("accounting_close_packet_evidence_hash", accountant_context)
        self.assertEqual(len(accountant_context["accounting_close_packet_evidence_hash"]), 64)
        self.assertIn("accounting_period_drift_checks", accountant_context)
        self.assertGreaterEqual(len(accountant_context["accounting_period_drift_checks"]), 1)
        self.assertIn("accounting_close_formula_checks", accountant_context)
        self.assertIn("accounting_sales_component_checks", accountant_context)
        self.assertIn("accounting_return_tieout_checks", accountant_context)
        self.assertIn("accounting_inventory_valuation_checks", accountant_context)
        self.assertIn("accounting_fee_evidence_checks", accountant_context)
        self.assertIn("accounting_shipping_evidence_checks", accountant_context)
        self.assertIn("accounting_reconciliation_tieout_checks", accountant_context)
        self.assertIn("accounting_cogs_source_checks", accountant_context)
        self.assertIn("sale_fifo_cogs_evidence_rows", accountant_context)
        self.assertIn("accounting_lot_allocation_checks", accountant_context)
        self.assertIn("accounting_exception_queue_checks", accountant_context)
        self.assertIn("accounting_margin_anomaly_checks", accountant_context)
        self.assertIn("accounting_close_consistency_checks", accountant_context)
        self.assertIn("accounting_close_packet_completeness_checks", accountant_context)
        self.assertIn("accounting_close_packet_manifest_checks", accountant_context)
        self.assertIn("accounting_close_packet_hash_checks", accountant_context)
        self.assertIn("accounting_close_packet_evidence_hash_rows", accountant_context)
        self.assertIn("accounting_period_drift_summary", accountant_context)
        self.assertIn("tax_review_summary", accountant_context)
        self.assertIn("tax_profile_evidence", accountant_context)
        self.assertIn("tax_reporting_signoff_rows", accountant_context)
        self.assertIn("tax_reporting_signoff_review", accountant_context)
        self.assertIn("accounting_close_signoff_rows", accountant_context)
        self.assertIn("accounting_close_signoff_review", accountant_context)
        self.assertIn("ai_review_outcome_rows", accountant_context)
        self.assertEqual(len(audit_calls), 1)
        self.assertEqual(audit_calls[0]["intent"], "reports_ai_accountant_review")
        self.assertEqual(audit_calls[0]["metadata"]["event_type"], "ai_accountant_review")
        self.assertTrue(audit_calls[0]["metadata"]["read_only"])
        self.assertEqual(len(audit_calls[0]["metadata"]["prompt_hash_sha256"]), 64)
        self.assertEqual(len(audit_calls[0]["metadata"]["data_scope_hash_sha256"]), 64)
        self.assertIn("reports_accountant_metadata", fake_st_ok.session_state)
        data_scope = audit_calls[0]["metadata"]["data_scope"]
        self.assertEqual(data_scope["date_range"], accountant_context["date_range"])
        self.assertEqual(len(data_scope["context_hash_sha256"]), 64)
        self.assertIn("tax_packet_evidence_hash", data_scope)
        self.assertIn("accounting_close_packet_evidence_hash", data_scope)
        self.assertIn("tax_reporting_signoff_review", data_scope["row_counts"])
        self.assertIn("sale_fifo_cogs_evidence", data_scope["row_counts"])
        self.assertIn("tax", audit_calls[0]["allowed_domains"])
        citation_tables = {row["table"] for row in audit_calls[0]["citations"]}
        self.assertIn("accounting_period_drift_checks", citation_tables)
        self.assertIn("accounting_close_formula_checks", citation_tables)
        self.assertIn("accounting_sales_component_checks", citation_tables)
        self.assertIn("accounting_return_tieout_checks", citation_tables)
        self.assertIn("accounting_inventory_valuation_checks", citation_tables)
        self.assertIn("accounting_fee_evidence_checks", citation_tables)
        self.assertIn("accounting_shipping_evidence_checks", citation_tables)
        self.assertIn("accounting_reconciliation_tieout_checks", citation_tables)
        self.assertIn("accounting_cogs_source_checks", citation_tables)
        self.assertIn("sale_fifo_cogs_evidence", citation_tables)
        self.assertIn("accounting_lot_allocation_checks", citation_tables)
        self.assertIn("accounting_exception_queue_checks", citation_tables)
        self.assertIn("accounting_margin_anomaly_checks", citation_tables)
        self.assertIn("accounting_close_consistency_checks", citation_tables)
        self.assertIn("accounting_close_packet_completeness_checks", citation_tables)
        self.assertIn("accounting_close_packet_manifest_checks", citation_tables)
        self.assertIn("accounting_close_packet_hash_checks", citation_tables)
        self.assertIn("accounting_close_packet_evidence_hash", citation_tables)
        self.assertIn("accounting_close_signoffs", citation_tables)
        self.assertIn("accounting_close_signoff_review", citation_tables)
        self.assertIn("ai_review_outcomes", citation_tables)
        self.assertIn("tax_profile", citation_tables)
        self.assertIn("tax_reporting_signoffs", citation_tables)
        self.assertIn("tax_reporting_signoff_review", citation_tables)
        self.assertTrue(fake_st_ok.rerun_called)


if __name__ == "__main__":
    unittest.main()
