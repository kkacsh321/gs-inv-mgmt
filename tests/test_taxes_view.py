import unittest
import importlib.util
import sys
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch


def _load_taxes_view():
    root = Path(__file__).resolve().parents[1]
    views_dir = root / "app" / "components" / "views"
    if "boto3" not in sys.modules:
        fake_boto3 = type(sys)("boto3")
        fake_boto3.session = SimpleNamespace(Session=lambda *args, **kwargs: None)
        sys.modules["boto3"] = fake_boto3
    if "botocore" not in sys.modules:
        sys.modules["botocore"] = type(sys)("botocore")
    if "botocore.config" not in sys.modules:
        fake_botocore_config = type(sys)("botocore.config")
        fake_botocore_config.Config = lambda *args, **kwargs: None
        sys.modules["botocore.config"] = fake_botocore_config
    if "botocore.exceptions" not in sys.modules:
        fake_botocore_exceptions = type(sys)("botocore.exceptions")
        fake_botocore_exceptions.BotoCoreError = Exception
        fake_botocore_exceptions.ClientError = Exception
        sys.modules["botocore.exceptions"] = fake_botocore_exceptions
    pkg_name = "app.components.views"
    if pkg_name not in sys.modules:
        pkg = type(sys)(pkg_name)
        pkg.__path__ = [str(views_dir)]
        sys.modules[pkg_name] = pkg

    reports_path = root / "app" / "components" / "views" / "reports.py"
    reports_spec = importlib.util.spec_from_file_location("app.components.views.reports", reports_path)
    reports_module = importlib.util.module_from_spec(reports_spec)
    assert reports_spec and reports_spec.loader
    reports_spec.loader.exec_module(reports_module)
    sys.modules["app.components.views.reports"] = reports_module

    taxes_path = root / "app" / "components" / "views" / "taxes.py"
    taxes_spec = importlib.util.spec_from_file_location("app.components.views.taxes", taxes_path)
    taxes_module = importlib.util.module_from_spec(taxes_spec)
    assert taxes_spec and taxes_spec.loader
    taxes_spec.loader.exec_module(taxes_module)
    return taxes_module


taxes = _load_taxes_view()


class _FakeExpander:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeSt:
    def __init__(self):
        self.calls = []
        self.session_state = {}

    def subheader(self, *args, **kwargs):
        self.calls.append(("subheader", args, kwargs))

    def caption(self, *args, **kwargs):
        self.calls.append(("caption", args, kwargs))

    def info(self, *args, **kwargs):
        self.calls.append(("info", args, kwargs))

    def expander(self, *args, **kwargs):
        self.calls.append(("expander", args, kwargs))
        return _FakeExpander()

    def markdown(self, *args, **kwargs):
        self.calls.append(("markdown", args, kwargs))


class TaxesViewTests(unittest.TestCase):
    def test_render_taxes_uses_reports_tax_workspace_mode(self):
        repo = SimpleNamespace()
        with patch.object(taxes, "render_reports") as render_reports:
            taxes.render_taxes(repo)

        render_reports.assert_called_once_with(repo, tax_workspace=True)

    def test_tax_support_constants_and_packet_prefixes_are_available(self):
        from app.components.views.tax_support import (
            COLORADO_SUTS_ACCOUNT_NUMBER,
            COLORADO_SUTS_GOLDEN_STATE_ACCOUNT_NUMBER,
            COLORADO_SUTS_TEMPLATE_PATH,
            tax_review_packet_prefixes,
        )

        self.assertEqual(COLORADO_SUTS_ACCOUNT_NUMBER, "080390")
        self.assertEqual(COLORADO_SUTS_GOLDEN_STATE_ACCOUNT_NUMBER, "970074130001")
        self.assertEqual(COLORADO_SUTS_TEMPLATE_PATH.name, "CO-SUTS-Excel-Template-127596.xlsx")
        prefixes = tax_review_packet_prefixes()
        self.assertIn("tax_detail_estimated", prefixes)
        self.assertIn("quarterly_estimated_tax_fee_summary", prefixes)
        self.assertIn("quarterly_estimated_tax_fee_detail", prefixes)
        self.assertIn("quarterly_estimated_tax_payment_review", prefixes)
        self.assertNotIn("sales_detail", prefixes)

    def test_taxes_workspace_intro_renders_tax_guidance(self):
        from app.components.views.tax_support import render_taxes_workspace_intro

        fake_st = _FakeSt()
        help_calls = []

        def fake_help_panel(**kwargs):
            help_calls.append(kwargs)

        render_taxes_workspace_intro(st=fake_st, render_help_panel=fake_help_panel)

        rendered = "\n".join(
            str(arg)
            for _kind, args, _kwargs in fake_st.calls
            for arg in args
        )
        self.assertIn("Taxes", rendered)
        self.assertIn("Colorado SUTS", rendered)
        self.assertIn("Golden State `110042`", rendered)
        self.assertIn("50/50 spouse-owned partnership LLC", rendered)
        self.assertEqual(help_calls[0]["section_title"], "Taxes")
        self.assertEqual(help_calls[0]["roadmap_phase"], "GS Tax Reporting + Accounting Hardening")

    def test_tax_scope_controls_default_to_non_facilitator_marketplaces(self):
        from app.components.views.tax_support import render_tax_reporting_scope_controls

        fake_st = _FakeSt()
        result = render_tax_reporting_scope_controls(
            st=fake_st,
            tax_workspace=False,
            sales=[
                SimpleNamespace(marketplace="ebay"),
                SimpleNamespace(marketplace="local"),
                SimpleNamespace(marketplace="POS"),
            ],
            tax_default_jurisdiction="Golden, Colorado",
            tax_default_rate=7.5,
            tax_shipping_taxable_default=False,
            tax_exempt_categories_default_csv="bullion,coins",
            facilitator_channels_default_csv="ebay",
            tax_profile_rows=[],
        )

        self.assertEqual(result["tax_exempt_categories"], {"bullion", "coins"})
        self.assertEqual(result["facilitator_channels"], {"ebay"})
        self.assertEqual(result["sales_marketplace_options"], ["ebay", "local", "pos"])
        self.assertEqual(result["selected_tax_marketplace_set"], {"local", "pos"})
        self.assertEqual(result["tax_query_marketplace_set"], {"local", "pos"})
        self.assertEqual(result["tax_marketplace_scope_label"], "local,pos")
        self.assertEqual(fake_st.calls, [])


if __name__ == "__main__":
    unittest.main()
