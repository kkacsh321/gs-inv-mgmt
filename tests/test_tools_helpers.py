import importlib.util
import json
import sys
import types
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pandas as pd


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
    path = root / "app" / "components" / "views" / "tools.py"
    spec = importlib.util.spec_from_file_location("test_tools_module", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


tools = _load_module()


class _Uploaded:
    def __init__(self, data: bytes, content_type: str = "image/jpeg", name: str = "x.jpg"):
        self._data = data
        self.type = content_type
        self.name = name

    def getvalue(self):
        return self._data


class ToolsHelpersTests(unittest.TestCase):
    def test_coin_csv_helpers(self):
        df = pd.DataFrame([{"Coin Name": "A", "year from": "1900"}])
        out = tools._coin_csv_normalize_columns(df)
        self.assertIn("coin_name", out.columns)
        self.assertIn("year_start", out.columns)

        self.assertEqual(tools._coin_csv_cell_str(float("nan")), "")
        self.assertEqual(tools._coin_csv_cell_int("1901.0"), 1901)
        self.assertIsNone(tools._coin_csv_cell_int("bad"))
        self.assertEqual(tools._coin_csv_cell_decimal("$1,234.50"), Decimal("1234.50"))
        self.assertIsNone(tools._coin_csv_cell_decimal("-1"))
        self.assertTrue(tools._coin_csv_cell_bool("yes"))
        self.assertFalse(tools._coin_csv_cell_bool("off"))

    def test_coin_ref_payload_and_key(self):
        row = pd.Series(
            {
                "coin_name": "Morgan Dollar",
                "country": "US",
                "series": "Morgan",
                "year_start": "1878",
                "mint_mark": "CC",
                "is_active": "true",
            }
        )
        payload = tools._coin_ref_payload_from_csv_row(row)
        self.assertEqual(payload["coin_name"], "Morgan Dollar")
        self.assertTrue(payload["is_active"])
        self.assertEqual(payload["_match_key"], "morgan dollar|us|morgan|1878|cc")

    def test_parse_and_query_helpers(self):
        domains = tools._parse_domain_csv("https://www.APMEX.com, jmbullion.com,\n")
        self.assertIn("apmex.com", domains)
        self.assertIn("jmbullion.com", domains)

        tokens = tools._tokenize_query("1_oz-silver bar bar")
        self.assertEqual(tokens[0].lower(), "oz")
        variants = tools._query_variants("very long silver bar query words")
        self.assertGreaterEqual(len(variants), 3)

    def test_uploaded_helpers_and_retry_preset(self):
        img = _Uploaded(b"img", "image/png", "i.png")
        payload, ctype = tools._uploaded_image_to_bytes(img)
        self.assertEqual(payload, b"img")
        self.assertEqual(ctype, "image/png")

        non_img = _Uploaded(b"vid", "video/mp4", "v.mp4")
        _, non_img_type = tools._uploaded_image_to_bytes(non_img)
        self.assertEqual(non_img_type, "image/jpeg")

        self.assertEqual(tools._uploaded_file_name(non_img), "v.mp4")
        self.assertEqual(tools._parse_photo_comp_retry_preset('{"a":1}')["a"], 1)
        self.assertEqual(tools._parse_photo_comp_retry_preset("bad"), {})

    def test_media_persist(self):
        created = []

        class _Storage:
            enabled = True

            @staticmethod
            def upload_file(file_name, file_bytes, content_type):
                if file_name == "bad.jpg":
                    raise RuntimeError("fail")
                return SimpleNamespace(
                    content_type=content_type,
                    size_bytes=len(file_bytes),
                    bucket="b",
                    key=f"k/{file_name}",
                    url=f"https://u/{file_name}",
                )

        class _Repo:
            @staticmethod
            def create_media_asset(**kwargs):
                created.append(kwargs)

        uploaded, errors = tools._persist_ai_input_media(
            repo=_Repo(),
            storage=_Storage(),
            files=[(b"1", "image/jpeg", "a.jpg"), (b"2", "video/mp4", "v.mp4"), (b"3", "image/jpeg", "bad.jpg")],
            product_id=1,
            listing_id=2,
            uploaded_by="admin",
        )
        self.assertEqual(uploaded, 2)
        self.assertEqual(len(errors), 1)
        self.assertEqual(created[0]["media_type"], "image")
        self.assertEqual(created[1]["media_type"], "video")

    def test_json_extract_and_repair(self):
        self.assertEqual(tools._extract_first_json_object('{"a":1}')["a"], 1)
        self.assertEqual(tools._extract_first_json_object("```json\n{\"a\":2}\n```")["a"], 2)
        self.assertTrue(tools._looks_like_truncated_json_output('{"a":'))
        repaired = tools._repair_json_object_text('{"a":1')
        self.assertTrue(repaired.endswith("}"))
        self.assertEqual(tools._extract_or_repair_first_json_object('{"a":1')["a"], 1)

    def test_comp_math_helpers(self):
        rows = [
            {"sold_price": 10, "shipping_cost": 2, "listed_price": 10, "total_price": 0},
            {"sold_price": 0, "shipping_cost": 1, "listed_price": 5, "total_price": 0},
        ]
        self.assertEqual(tools._effective_total_price(rows[0]), 12)
        self.assertEqual(tools._representative_price([1, 3, 2, 4]), 2.5)

        stats = tools._comp_stats(rows)
        self.assertEqual(stats["count"], 2)
        breakdown = tools._comp_cost_breakdown(rows)
        self.assertGreater(breakdown["total_avg"], 0)

    def test_confidence_and_parser_helpers(self):
        self.assertEqual(tools._detect_metal_from_query("1 oz silver round"), "silver")
        self.assertEqual(tools._price_confidence_label(0.9), "high")

        score, label = tools._web_price_confidence(
            price_source="page_fetch",
            price_count=3,
            tier_count=1,
            parser_source="domain_specific",
            domain="apmex.com",
            dealer_domains=("apmex.com",),
        )
        self.assertGreaterEqual(score, 0.85)
        self.assertIn(label, {"high", "medium"})
        self.assertEqual(
            tools._best_page_parser_source([1], [], [], [], 0),
            "html_general",
        )

    def test_price_extraction_helpers(self):
        hints = tools._extract_price_hints("US $11.99 as low as 7.99 only 5")
        self.assertIn(11.99, hints)
        self.assertIn(7.99, hints)

        html = '<meta property="og:price:amount" content="9.99"><span class="price">$12.50</span>'
        html_prices = tools._extract_price_hints_from_html(html)
        self.assertIn(9.99, html_prices)
        self.assertIn(12.5, html_prices)

    def test_json_ld_and_embedded_and_tiers(self):
        html = '''
        <script type="application/ld+json">{"offers":{"price":15.00,"lowPrice":12.50}}</script>
        <script>window.__STATE__={"priceString":"$9.99","priceCents":1250}</script>
        <div>1-19 $8.99</div><div>20-99 $8.69</div>
        '''
        ld = tools._extract_json_ld_prices(html)
        self.assertIn(15.0, ld)
        self.assertIn(12.5, ld)
        emb = tools._extract_json_embedded_prices(html)
        self.assertIn(15.0, emb)
        self.assertIn(12.5, emb)
        tiers = tools._extract_tier_prices_from_html(html)
        self.assertGreaterEqual(tiers["high"], 8.99)

    def test_domain_specific_prices(self):
        ebay_html = '<span class="x-price-primary">US $11.99</span>'
        prices = tools._extract_domain_specific_prices("https://www.ebay.com/itm/1", ebay_html)
        self.assertIn(11.99, prices)

        amz_html = '<span class="a-offscreen">$9.99</span>'
        prices2 = tools._extract_domain_specific_prices("https://www.amazon.com/dp/x", amz_html)
        self.assertIn(9.99, prices2)


if __name__ == "__main__":
    unittest.main()
