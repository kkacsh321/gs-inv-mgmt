import importlib.util
import json
import sys
import types
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd


def _bootstrap_views_package() -> None:
    root = Path(__file__).resolve().parents[1]
    views_path = str(root / "app" / "components" / "views")
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
        pkg.__path__ = [views_path]
        sys.modules["app.components.views"] = pkg
    else:
        existing_path = list(getattr(sys.modules["app.components.views"], "__path__", []) or [])
        if views_path not in existing_path:
            sys.modules["app.components.views"].__path__ = [views_path, *existing_path]

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


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeSt:
    def __init__(self, *, button_value: bool = False):
        self.button_value = bool(button_value)
        self.captions = []
        self.markdowns = []
        self.successes = []
        self.codes = []
        self.dataframes = []
        self.rerun_called = False

    def expander(self, *_a, **_k):
        return _Ctx()

    def caption(self, msg):
        self.captions.append(str(msg))

    def markdown(self, msg, **_k):
        self.markdowns.append(str(msg))

    def button(self, *_a, **_k):
        return self.button_value

    def success(self, msg):
        self.successes.append(str(msg))

    def rerun(self):
        self.rerun_called = True

    def dataframe(self, data, **_k):
        self.dataframes.append(data)

    def code(self, data, **_k):
        self.codes.append(str(data))


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
        self.assertEqual(
            tools._resolve_duckduckgo_result_url(
                "https://www.bing.com/ck/a?u=a1aHR0cHM6Ly9leGFtcGxlLmNvbS9tcG0"
            ),
            "https://example.com/mpm",
        )

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

    def test_render_grader_summary_text_renders_plain_code_block(self):
        fake_st = _FakeSt()
        original_st = tools.st
        try:
            tools.st = fake_st
            tools._render_grader_summary_text("Line one\n*Line two*")
        finally:
            tools.st = original_st
        self.assertIn("##### Grader Summary", fake_st.markdowns)
        self.assertEqual(fake_st.codes, ["Line one\n*Line two*"])

    def test_render_grader_summary_text_ignores_empty(self):
        fake_st = _FakeSt()
        original_st = tools.st
        try:
            tools.st = fake_st
            tools._render_grader_summary_text("   ")
        finally:
            tools.st = original_st
        self.assertEqual(fake_st.markdowns, [])
        self.assertEqual(fake_st.codes, [])

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
        relevance = tools._comp_row_relevance(
            "MPM Crown Monarch Precious Metals 3 Troy oz .999 Fine Silver Ingot Bar Silver",
            {"title": "10 oz .999 Fine Silver Bar - Monarch Poured", "view_url": "https://example.com/10-oz"},
        )
        self.assertLess(relevance["score"], 0.55)
        self.assertIn("weight_mismatch", relevance["flags"])
        qualified_rows, qualification = tools._qualified_comp_rows(
            [
                {"listed_price": 15, "relevance_score": 1.0},
                {"listed_price": 220, "relevance_score": 1.0},
                {"listed_price": 235, "relevance_score": 1.0},
                {"listed_price": 250, "relevance_score": 1.0},
                {"listed_price": 365, "relevance_score": 1.0},
                {"listed_price": 210, "relevance_score": 0.2, "relevance_flags": "weight_mismatch"},
            ]
        )
        self.assertEqual(len(qualified_rows), 4)
        self.assertEqual(qualification["removed_rows"], 2)
        self.assertEqual(qualification["relevance_removed_rows"], 1)
        self.assertEqual(qualification["method"], "median_band_45_185_pct")
        self.assertEqual(qualification["removed_samples"][0]["price"], 15)
        breakdown = tools._comp_cost_breakdown(rows)
        self.assertGreater(breakdown["total_avg"], 0)
        quality = tools._comp_evidence_quality([], [{"source": "web", "listed_price": 12, "price_confidence_score": 0.8}])
        self.assertEqual(quality["label"], "active_market_priced")
        self.assertEqual(quality["web_priced_rows"], 1)

        ai_rows = tools._filter_ai_web_comp_rows(
            [
                {"source": "web", "listed_price": 0, "price_confidence_label": "very_low"},
                {"source": "web", "listed_price": 12, "price_confidence_label": "low"},
                {"source": "web", "listed_price": 0, "search_scope": "configured_dealer"},
            ]
        )
        self.assertEqual(len(ai_rows), 2)
        diagnostics = tools._comp_quality_diagnostics(
            [{"note": "ebay_sold_html_HTTPError: status=403"}],
            [],
            [
                {"domain": "priced.example", "listed_price": 10},
                {"domain": "blank.example", "listed_price": 0},
            ],
        )
        self.assertIn("403", diagnostics["ebay_status"])
        self.assertEqual(diagnostics["top_priced_domains"][0][0], "priced.example")
        self.assertEqual(diagnostics["top_unpriced_domains"][0][0], "blank.example")
        diag_rows = tools._comp_quality_diagnostics_rows(diagnostics)
        self.assertTrue(any(row["Signal"] == "eBay sold status" for row in diag_rows))
        formatted_attempts = tools._format_attempt_rows(
            [{"note": "ebay_sold_html_HTTPError: status=403"}, {"note": "ebay_finding_error: RuntimeError"}]
        )
        self.assertIn("blocked", formatted_attempts[0]["status"])
        self.assertIn("fallback failed", formatted_attempts[1]["status"])
        export_payload = tools._comp_evidence_export_payload(
            query="mpm silver",
            attempts=[{"note": "ebay_sold_html_HTTPError: status=403"}],
            rows=[],
            web_rows=[{"domain": "priced.example", "listed_price": 10}],
            stats={"count": 1, "median": 10},
            cost_breakdown={"total_avg": 10},
            evidence_quality=quality,
            diagnostics=diagnostics,
            spot_context={"detected_metal": "silver"},
        )
        self.assertEqual(export_payload["query"], "mpm silver")
        self.assertEqual(len(export_payload["web_rows"]), 1)
        self.assertEqual(export_payload["spot_context"]["detected_metal"], "silver")
        self.assertTrue(export_payload["notes"])

    def test_web_comp_search_falls_back_to_bing_when_duckduckgo_anomaly_has_no_results(self):
        class _Resp:
            def __init__(self, status_code: int, text: str):
                self.status_code = status_code
                self.text = text

            def raise_for_status(self):
                return None

        duck_html = "<html><body>anomaly</body></html>"
        bing_html = """
        <li class="b_algo">
          <h2><a href="https://example.com/mpm">MPM 3 Troy oz Silver Ingot</a></h2>
          <p>Dealer result with price $149.95.</p>
        </li>
        """
        with patch.object(
            tools.requests,
            "get",
            side_effect=[_Resp(202, duck_html), _Resp(200, bing_html)],
        ), patch.object(
            tools,
            "_fetch_page_details_batch",
            return_value={},
        ):
            rows = tools._web_comp_search("mpm 3 oz silver ingot", limit=5, page_fetch_limit=1)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["search_provider"], "bing")
        self.assertEqual(rows[0]["domain"], "example.com")
        self.assertAlmostEqual(float(rows[0]["listed_price"]), 149.95, places=2)

    def test_web_comp_search_adds_configured_dealer_targeted_rows_when_broad_rows_are_weak(self):
        calls = []

        def _fake_get(_url, params=None, **_kwargs):
            q = str((params or {}).get("q") or "")
            calls.append(q)
            if q.startswith("site:apmex.com"):
                return SimpleNamespace(
                    status_code=200,
                    text=(
                        '<a class="result__a" href="https://apmex.com/product/1">Dealer row</a>'
                        '<div class="result__snippet">Dealer price $99.95</div>'
                    ),
                    raise_for_status=lambda: None,
                )
            return SimpleNamespace(
                status_code=200,
                text='<a class="result__a" href="https://example.com/weak">Weak row</a>',
                raise_for_status=lambda: None,
            )

        with patch.object(tools.requests, "get", side_effect=_fake_get), patch.object(
            tools,
            "_fetch_page_details_batch",
            return_value={},
        ):
            rows = tools._web_comp_search(
                "mpm 3 oz silver ingot",
                limit=5,
                page_fetch_limit=1,
                dealer_domains=("apmex.com",),
            )

        self.assertTrue(any(str(call).startswith("site:apmex.com") for call in calls))
        self.assertTrue(any(row.get("search_scope") == "configured_dealer" for row in rows))
        self.assertTrue(any(str(row.get("domain") or "") == "apmex.com" for row in rows))

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

    def test_parse_ebay_product_research_csv_marks_sold_market(self):
        csv_bytes = (
            "Item ID,Title,Sold Price,Shipping,Item URL,Sold Date,Condition\n"
            "123,MPM 3 oz silver bar,$180.00,$5.99,https://www.ebay.com/itm/123,2026-05-01,Used\n"
        ).encode("utf-8")
        rows = tools._parse_ebay_product_research_csv(csv_bytes)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "ebay_product_research")
        self.assertEqual(rows[0]["evidence"], "sold_market")
        self.assertEqual(rows[0]["item_id"], "123")
        self.assertEqual(rows[0]["sold_price"], 180.0)
        self.assertEqual(rows[0]["shipping_cost"], 5.99)
        self.assertAlmostEqual(rows[0]["total_price"], 185.99)


    def test_build_inventory_mode_query_prefill_applies_once(self):
        product = SimpleNamespace(title="Credit Suisse 100 gram bar", sku="CS-100G", metal_type="silver")
        derived_query = tools._build_inventory_mode_query(
            selected_product=product,
            use_title=True,
            use_sku=False,
            use_metal=True,
            prefill_query="stale prefill query",
            prefill_apply_once=False,
        )
        self.assertIn("Credit Suisse", derived_query)
        self.assertIn("silver", derived_query)
        self.assertNotIn("stale prefill query", derived_query)

        prefilled_query = tools._build_inventory_mode_query(
            selected_product=product,
            use_title=True,
            use_sku=False,
            use_metal=True,
            prefill_query="apply once prefill",
            prefill_apply_once=True,
        )
        self.assertEqual(prefilled_query, "apply once prefill")

if __name__ == "__main__":
    unittest.main()
