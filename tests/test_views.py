from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace
import unittest


class ViewsCompatibilityTests(unittest.TestCase):
    def test_views_module_re_exports_expected_symbols(self):
        sys.modules.pop("app.views", None)
        fake_components_views = SimpleNamespace(
            MARKETPLACES=["ebay", "facebook_marketplace"],
            MEDIA_UPLOAD_TYPES=["image", "video"],
            as_money=lambda v: v,
            dataframe_to_xlsx_bytes=lambda _df: b"",
            generate_sku=lambda: "SKU-1",
            infer_media_type=lambda _x: "image",
            pretty_json=lambda obj: str(obj),
            render_admin=lambda *_a, **_k: None,
            render_dashboard=lambda *_a, **_k: None,
            render_documents=lambda *_a, **_k: None,
            render_ebay=lambda *_a, **_k: None,
            render_inventory_movements=lambda *_a, **_k: None,
            render_listings=lambda *_a, **_k: None,
            render_lots=lambda *_a, **_k: None,
            render_media=lambda *_a, **_k: None,
            render_orders=lambda *_a, **_k: None,
            render_products=lambda *_a, **_k: None,
            render_reports=lambda *_a, **_k: None,
            render_returns=lambda *_a, **_k: None,
            render_sales=lambda *_a, **_k: None,
            render_search_edit=lambda *_a, **_k: None,
            render_shipping=lambda *_a, **_k: None,
            render_sources=lambda *_a, **_k: None,
            render_tools=lambda *_a, **_k: None,
            upload_media_for_listing=lambda *_a, **_k: [],
        )
        with unittest.mock.patch.dict(sys.modules, {"app.components.views": fake_components_views}):
            views = importlib.import_module("app.views")

        self.assertTrue(callable(views.render_dashboard))
        self.assertTrue(callable(views.render_products))
        self.assertTrue(callable(views.render_listings))
        self.assertTrue(callable(views.render_tools))
        self.assertTrue(callable(views.upload_media_for_listing))
        self.assertIn("ebay", [m.lower() for m in views.MARKETPLACES])
        self.assertIn("image", [m.lower() for m in views.MEDIA_UPLOAD_TYPES])


if __name__ == "__main__":
    unittest.main()
