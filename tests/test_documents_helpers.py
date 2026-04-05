import importlib.util
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


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
    path = root / "app" / "components" / "views" / "documents.py"
    spec = importlib.util.spec_from_file_location("test_documents_module", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


docs = _load_module()


class _FakeSt:
    def __init__(self):
        self.calls = []

    def markdown(self, *args, **kwargs):
        self.calls.append(("markdown", args, kwargs))

    def caption(self, *args, **kwargs):
        self.calls.append(("caption", args, kwargs))

    def dataframe(self, *args, **kwargs):
        self.calls.append(("dataframe", args, kwargs))

    def selectbox(self, _label, options, **_kwargs):
        return options[0]

    def warning(self, *args, **kwargs):
        self.calls.append(("warning", args, kwargs))

    def download_button(self, *args, **kwargs):
        self.calls.append(("download_button", args, kwargs))


class _FakeStMissingSelect(_FakeSt):
    def selectbox(self, _label, options, **_kwargs):
        _ = options
        return 999999


class DocumentHelpersTests(unittest.TestCase):
    def test_file_to_data_url_and_logo_resolution(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "logo.jpg"
            p.write_bytes(b"jpeg-bytes")
            data_url = docs._file_to_data_url(p)
            self.assertTrue(data_url.startswith("data:image/jpeg;base64,"))

            via_file = docs._resolve_logo_src("Modern", str(p))
            self.assertTrue(via_file.startswith("data:image/jpeg;base64,"))

        self.assertEqual(docs._resolve_logo_src("Classic", "https://x/y.jpg"), "https://x/y.jpg")
        self.assertEqual(docs._resolve_logo_src("Classic", "missing/file.jpg"), "missing/file.jpg")

    def test_item_builders_and_tax_helpers(self):
        order = SimpleNamespace(
            items=[
                SimpleNamespace(quantity=2, unit_price=3.5, line_total=None, product=SimpleNamespace(sku="A", title="Alpha", category="bullion")),
                SimpleNamespace(quantity=1, unit_price=10, line_total=12.5, product=None),
            ]
        )
        order_items = docs._build_items_for_order(order)
        self.assertEqual(order_items[0]["line_total"], 7.0)
        self.assertEqual(order_items[1]["line_total"], 12.5)

        sale = SimpleNamespace(quantity_sold=2, sold_price=20.0, product=SimpleNamespace(sku="B", title="Beta", category="collectible"))
        sale_items = docs._build_items_for_sale(sale)
        self.assertEqual(sale_items[0]["unit_price"], 10.0)

        listing = SimpleNamespace(listing_title="", product=SimpleNamespace(sku="C", title="Gamma", category="coin"))
        listing_items = docs._build_items_for_listing(listing, quantity=0, unit_price=5)
        self.assertEqual(listing_items[0]["qty"], 1)
        self.assertEqual(listing_items[0]["title"], "Gamma")

        exempt = docs._parse_csv_set("Bullion, Coin")
        taxable = docs._taxable_subtotal_auto(
            [
                {"category": "bullion", "line_total": 10},
                {"category": "collectible", "line_total": 25},
                {"category": "", "line_total": 5},
                {"category": "coin", "line_total": -1},
            ],
            exempt,
        )
        self.assertEqual(taxable, 30.0)

        presets = docs._default_tax_presets(
            default_jurisdiction="Golden, Colorado",
            default_tax_rate_percent=8.9,
            default_shipping_taxable=True,
        )
        self.assertIn("Bullion/Coin Exempt", presets)

        basis = docs._derive_tax_exemption_basis(
            tax_mode="Auto (Category Rules)",
            exempt_categories={"coin", "bullion"},
            shipping_is_taxable=False,
            use_line_item_taxability=True,
        )
        self.assertIn("auto_category_exemptions", basis)
        self.assertIn("shipping_exempt", basis)
        self.assertEqual(
            docs._derive_tax_exemption_basis(
                tax_mode="No Tax",
                exempt_categories=set(),
                shipping_is_taxable=True,
                use_line_item_taxability=False,
            ),
            "tax_mode_no_tax",
        )
        manual_basis = docs._derive_tax_exemption_basis(
            tax_mode="Manual Taxable Subtotal",
            exempt_categories=set(),
            shipping_is_taxable=True,
            use_line_item_taxability=False,
        )
        self.assertIn("manual_taxable_subtotal_override", manual_basis)
        self.assertIn("shipping_taxable", manual_basis)
        auto_min_basis = docs._derive_tax_exemption_basis(
            tax_mode="Auto (Category Rules)",
            exempt_categories=set(),
            shipping_is_taxable=False,
            use_line_item_taxability=False,
        )
        self.assertEqual(auto_min_basis, "shipping_exempt")

    def test_resolve_logo_src_classic_default(self):
        with patch.object(docs, "DEFAULT_LOGO_PATH", Path(__file__).resolve()):
            data_url = docs._resolve_logo_src("Classic", "")
        self.assertTrue(data_url.startswith("data:image/jpeg;base64,"))

    def test_render_retained_artifacts(self):
        fake_st = _FakeSt()
        rows = [
            SimpleNamespace(
                id=1,
                doc_type="invoice",
                document_number="INV-1",
                artifact_kind="pdf",
                file_name="inv.pdf",
                mime_type="application/pdf",
                size_bytes=4,
                content_sha256="abcd1234",
                storage_backend="s3",
                storage_ref="s3://x",
                created_by="admin",
                created_at=datetime(2026, 4, 2, 10, 0, 0),
            )
        ]
        repo = SimpleNamespace(
            list_document_artifacts_for_source=lambda **kwargs: rows,
            get_document_artifact_content=lambda _id: b"data",
        )
        with patch.object(docs, "st", fake_st):
            docs._render_retained_artifacts(repo, source_type="sale", source_id=1)
        self.assertTrue(any(c[0] == "download_button" for c in fake_st.calls))

        fake_st2 = _FakeSt()
        repo_none = SimpleNamespace(
            list_document_artifacts_for_source=lambda **kwargs: [],
        )
        with patch.object(docs, "st", fake_st2):
            docs._render_retained_artifacts(repo_none, source_type="sale", source_id=1)
        self.assertTrue(any(c[0] == "caption" for c in fake_st2.calls))

        fake_st3 = _FakeSt()
        repo_err = SimpleNamespace(
            list_document_artifacts_for_source=lambda **kwargs: rows,
            get_document_artifact_content=lambda _id: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        with patch.object(docs, "st", fake_st3):
            docs._render_retained_artifacts(repo_err, source_type="sale", source_id=1)
        self.assertTrue(any(c[0] == "warning" for c in fake_st3.calls))

    def test_render_retained_artifacts_missing_selected_row_returns(self):
        fake_st = _FakeStMissingSelect()
        rows = [
            SimpleNamespace(
                id=1,
                doc_type="invoice",
                document_number="INV-1",
                artifact_kind="pdf",
                file_name="inv.pdf",
                mime_type="application/pdf",
                size_bytes=4,
                content_sha256="abcd1234",
                storage_backend="s3",
                storage_ref="s3://x",
                created_by="admin",
                created_at=datetime(2026, 4, 2, 10, 0, 0),
            )
        ]
        repo = SimpleNamespace(
            list_document_artifacts_for_source=lambda **kwargs: rows,
            get_document_artifact_content=lambda _id: b"data",
        )
        with patch.object(docs, "st", fake_st):
            docs._render_retained_artifacts(repo, source_type="sale", source_id=1)
        self.assertFalse(any(c[0] == "download_button" for c in fake_st.calls))


if __name__ == "__main__":
    unittest.main()
