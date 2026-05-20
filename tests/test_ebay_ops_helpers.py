import importlib.util
import json
import sys
import types
import unittest
from datetime import datetime, date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class _DBStub:
    def __init__(self, products_by_id):
        self._products_by_id = products_by_id

    def get(self, _model, product_id):
        return self._products_by_id.get(product_id)



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
    for name in ("shared", "entity_ops", "workspace_shell", "ebay_context"):
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
    path = root / "app" / "components" / "views" / "ebay_ops.py"
    spec = importlib.util.spec_from_file_location("test_ebay_ops_module", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


ebay_ops = _load_module()


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
        self._text_area_map = {}
        self._button_map = {}
        self._selectbox_map = {}

    def set_text_area(self, label, value):
        self._text_area_map[label] = value

    def set_button(self, key_or_label, value):
        self._button_map[key_or_label] = bool(value)

    def set_selectbox(self, key_or_label, value):
        self._selectbox_map[key_or_label] = value

    def subheader(self, *a, **k):
        self.calls.append(("subheader", a, k))

    def caption(self, *a, **k):
        self.calls.append(("caption", a, k))

    def warning(self, *a, **k):
        self.calls.append(("warning", a, k))

    def info(self, *a, **k):
        self.calls.append(("info", a, k))

    def markdown(self, *a, **k):
        self.calls.append(("markdown", a, k))

    def text_area(self, label, **kwargs):
        key = kwargs.get("key")
        if key and key in self.session_state:
            return str(self.session_state.get(key) or "")
        return self._text_area_map.get(label, "")

    def multiselect(self, _label, options, **_kwargs):
        return list(options)

    def columns(self, n):
        count = n if isinstance(n, int) else len(n)
        return [_Ctx() for _ in range(int(count))]

    def checkbox(self, _label, value=False, **_kwargs):
        return value

    def date_input(self, _label, value=None, **_kwargs):
        return value

    def text_input(self, _label, value="", **_kwargs):
        return value

    def tabs(self, labels):
        return [_Ctx() for _ in labels]

    def dataframe(self, *a, **k):
        self.calls.append(("dataframe", a, k))

    def expander(self, _label, **_kwargs):
        return _Ctx()

    def json(self, *a, **k):
        self.calls.append(("json", a, k))

    def rerun(self):
        self.calls.append(("rerun", (), {}))

    def selectbox(self, label, options, **kwargs):
        key = kwargs.get("key")
        lookup = key or label
        if lookup in self._selectbox_map:
            return self._selectbox_map[lookup]
        return options[0]

    def button(self, label, **kwargs):
        key = kwargs.get("key")
        lookup = key or label
        return self._button_map.get(lookup, False)

    def success(self, *a, **k):
        self.calls.append(("success", a, k))


class EbayOpsHelpersTests(unittest.TestCase):
    def test_parse_offer_and_details(self):
        details = json.dumps({"ebay_publish": {"offer_id": "OFF-1"}})
        self.assertEqual(ebay_ops._parse_offer_id(details), "OFF-1")
        self.assertEqual(ebay_ops._parse_offer_id(json.dumps({"ebay_publish": {}})), "")
        self.assertEqual(ebay_ops._parse_offer_id(json.dumps({"x": 1})), "")
        self.assertEqual(ebay_ops._parse_offer_id("{"), "")

        self.assertEqual(ebay_ops._parse_details_obj('{"a":1}')["a"], 1)
        self.assertEqual(ebay_ops._parse_details_obj("raw notes")["notes"], "raw notes")
        self.assertEqual(ebay_ops._parse_details_obj("[1,2,3]"), {})
        self.assertEqual(ebay_ops._parse_details_obj(""), {})

    def test_merge_defaults(self):
        merged = ebay_ops._merge_defaults_into_listing_details('{"x":1}', {"loc": "A"})
        payload = json.loads(merged)
        self.assertEqual(payload["x"], 1)
        self.assertEqual(payload["ebay_ops_defaults"]["loc"], "A")

    def test_resolve_offer_id(self):
        listing_known = SimpleNamespace(marketplace_details=json.dumps({"ebay_publish": {"offer_id": "KNOWN"}}), external_listing_id="")
        out_known = ebay_ops._resolve_offer_id(SimpleNamespace(), "tok", listing_known, "SKU", {})
        self.assertEqual(out_known, "KNOWN")

        listing_no_sku = SimpleNamespace(marketplace_details="", external_listing_id="")
        self.assertEqual(ebay_ops._resolve_offer_id(SimpleNamespace(), "tok", listing_no_sku, "", {}), "")

        class _Client:
            @staticmethod
            def get_offers(access_token, sku):
                return {"offers": [{"listingId": "L-2", "offerId": "O-2"}, {"listingId": "L-X", "offerId": "O-X"}]}

        cache = {}
        listing_match = SimpleNamespace(marketplace_details="", external_listing_id="L-2")
        self.assertEqual(ebay_ops._resolve_offer_id(_Client(), "tok", listing_match, "SKU2", cache), "O-2")
        self.assertIn("SKU2", cache)

        class _SingleClient:
            @staticmethod
            def get_offers(access_token, sku):
                return {"offers": [{"listingId": "", "offerId": "ONLY"}]}

        listing_single = SimpleNamespace(marketplace_details="", external_listing_id="")
        self.assertEqual(ebay_ops._resolve_offer_id(_SingleClient(), "tok", listing_single, "SKU3", {}), "ONLY")

        class _NoMatchClient:
            @staticmethod
            def get_offers(access_token, sku):
                return {"offers": [{"listingId": "A", "offerId": "OA"}, {"listingId": "B", "offerId": "OB"}]}

        listing_no_match = SimpleNamespace(marketplace_details="", external_listing_id="Z")
        self.assertEqual(ebay_ops._resolve_offer_id(_NoMatchClient(), "tok", listing_no_match, "SKU4", {}), "")

        class _ShouldNotCallClient:
            @staticmethod
            def get_offers(access_token, sku):
                raise AssertionError("cache should have been used")

        listing_cached = SimpleNamespace(marketplace_details="", external_listing_id="L-CACHED")
        cache = {"SKU5": [{"listingId": "L-CACHED", "offerId": "O-CACHED"}]}
        self.assertEqual(ebay_ops._resolve_offer_id(_ShouldNotCallClient(), "tok", listing_cached, "SKU5", cache), "O-CACHED")

        class _ErrorClient:
            @staticmethod
            def get_offers(access_token, sku):
                raise RuntimeError("boom")

        listing_error = SimpleNamespace(marketplace_details="", external_listing_id="")
        with self.assertRaisesRegex(RuntimeError, "boom"):
            ebay_ops._resolve_offer_id(_ErrorClient(), "tok", listing_error, "SKU6", {})

    def test_publish_blockers_for_offer_flags_immediate_pay_auction_without_bin(self):
        class _Client:
            @staticmethod
            def get_payment_policy(access_token, payment_policy_id, marketplace_id):
                return {"paymentPolicyId": payment_policy_id, "immediatePay": True}

            @staticmethod
            def payment_policy_requires_immediate_payment(payload):
                return bool(payload.get("immediatePay"))

        blockers = ebay_ops._publish_blockers_for_offer(
            _Client(),
            "tok",
            {
                "format": "AUCTION",
                "marketplaceId": "EBAY_US",
                "listingPolicies": {"paymentPolicyId": "PAY1"},
                "pricingSummary": {"auctionStartPrice": {"value": "9.99"}},
            },
        )

        self.assertEqual(
            blockers,
            ["Immediate-payment payment policy requires an Auction Buy It Now price before live publish."],
        )

    def test_publish_blockers_for_offer_allows_auction_with_bin(self):
        class _Client:
            @staticmethod
            def get_payment_policy(access_token, payment_policy_id, marketplace_id):
                raise AssertionError("BIN auction should not need payment-policy lookup")

            @staticmethod
            def payment_policy_requires_immediate_payment(payload):
                return True

        blockers = ebay_ops._publish_blockers_for_offer(
            _Client(),
            "tok",
            {
                "format": "AUCTION",
                "marketplaceId": "EBAY_US",
                "listingPolicies": {"paymentPolicyId": "PAY1"},
                "pricingSummary": {
                    "auctionStartPrice": {"value": "9.99"},
                    "price": {"value": "19.99"},
                },
            },
        )

        self.assertEqual(blockers, [])

    def test_frame_and_labels(self):
        frame = ebay_ops._listings_frame([])
        self.assertIn("listing_id", frame.columns)
        frame2 = ebay_ops._listings_frame([{"listing_id": 1}])
        self.assertEqual(int(frame2.iloc[0]["listing_id"]), 1)

        label = ebay_ops._policy_label(
            {
                "paymentPolicyId": "P1",
                "name": "Policy",
                "categoryTypes": [{"name": "ALL_EXCLUDING_MOTORS_VEHICLES"}],
                "default": True,
            }
        )
        self.assertIn("P1", label)
        self.assertIn("default", label)

        self.assertEqual(ebay_ops._policy_label({}), "")
        self.assertEqual(ebay_ops._policy_label({"policyName": "Named"}), "Named")

        loc_label = ebay_ops._location_label(
            {
                "merchantLocationKey": "L1",
                "location": {"address": {"city": "Golden", "stateOrProvince": "CO", "country": "US"}},
                "status": "ENABLED",
            }
        )
        self.assertIn("Golden", loc_label)
        self.assertEqual(ebay_ops._location_label({}), "")

    def test_filtered_rows(self):
        p1 = SimpleNamespace(id=1, sku="SKU-1")
        p2 = SimpleNamespace(id=2, sku="SKU-2")
        listings = [
            SimpleNamespace(
                id=11,
                product_id=1,
                marketplace="ebay",
                listing_status="draft",
                external_listing_id="",
                listing_title="Silver Bar",
                listed_at=datetime(2026, 4, 1, 10, 0, 0),
            ),
            SimpleNamespace(
                id=12,
                product_id=2,
                marketplace="ebay",
                listing_status="active",
                external_listing_id="EB-12",
                listing_title="Gold Coin",
                listed_at=datetime(2026, 4, 2, 10, 0, 0),
            ),
            SimpleNamespace(
                id=13,
                product_id=2,
                marketplace="local",
                listing_status="active",
                external_listing_id="LOC-13",
                listing_title="Local",
                listed_at=datetime(2026, 4, 2, 10, 0, 0),
            ),
        ]
        repo = SimpleNamespace(
            list_listings=lambda: listings,
            db=_DBStub({1: p1, 2: p2}),
        )

        rows = ebay_ops._filtered_ebay_listing_rows(
            repo,
            status_filter=["draft", "active"],
            linked_only=False,
            query="",
            use_date_filter=False,
            listed_date_range=None,
        )
        self.assertEqual(len(rows), 2)

        linked_rows = ebay_ops._filtered_ebay_listing_rows(
            repo,
            status_filter=["active"],
            linked_only=True,
            query="gold",
            use_date_filter=True,
            listed_date_range=(date(2026, 4, 2), date(2026, 4, 2)),
        )
        self.assertEqual(len(linked_rows), 1)
        self.assertEqual(linked_rows[0][0].id, 12)

    def test_render_ebay_ops_not_configured(self):
        fake_st = _FakeSt()
        user = SimpleNamespace(username="admin", role="admin")
        repo = SimpleNamespace()
        client = SimpleNamespace(is_configured=lambda: False)
        with patch.object(ebay_ops, "st", fake_st), \
            patch.object(ebay_ops, "current_user", return_value=user), \
            patch.object(ebay_ops, "render_help_panel", return_value=None), \
            patch.object(ebay_ops, "render_active_ebay_context_banner", return_value=None), \
            patch.object(ebay_ops, "EbayClient", return_value=client):
            ebay_ops.render_ebay_ops(repo)
        self.assertTrue(any(c[0] == "warning" and "credentials are not configured" in str(c[1][0]).lower() for c in fake_st.calls))

    def test_render_ebay_ops_token_missing_after_sandbox_warning(self):
        fake_st = _FakeSt()
        fake_st.session_state["ebay_workspace_runbook_ready"] = False
        fake_st.set_text_area("User Access Token", "")
        user = SimpleNamespace(username="admin", role="admin")
        repo = SimpleNamespace()
        client = SimpleNamespace(is_configured=lambda: True, environment="sandbox")

        def _runtime_bool(_repo, key, default):
            if key == "ebay_allow_sandbox_seller_ops":
                return False
            if key == "ebay_require_runbook_for_bulk_ops":
                return True
            return default

        with patch.object(ebay_ops, "st", fake_st), \
            patch.object(ebay_ops, "current_user", return_value=user), \
            patch.object(ebay_ops, "render_help_panel", return_value=None), \
            patch.object(ebay_ops, "render_active_ebay_context_banner", return_value=None), \
            patch.object(ebay_ops, "EbayClient", return_value=client), \
            patch.object(ebay_ops, "get_runtime_bool", side_effect=_runtime_bool), \
            patch.object(ebay_ops, "get_runtime_str", return_value=""):
            ebay_ops.render_ebay_ops(repo)

        warning_messages = [str(c[1][0]) for c in fake_st.calls if c[0] == "warning" and c[1]]
        self.assertTrue(any("sandbox mode detected" in msg.lower() for msg in warning_messages))
        self.assertTrue(any("runbook checklist is not complete" in msg.lower() for msg in warning_messages))
        self.assertTrue(any("bulk operation guard is enabled" in msg.lower() for msg in warning_messages))
        self.assertTrue(any(c[0] == "info" and "provide an ebay user access token" in str(c[1][0]).lower() for c in fake_st.calls))

    def test_render_ebay_ops_profile_apply_updates_tokens_and_filters(self):
        fake_st = _FakeSt()
        fake_st.session_state["ebay_workspace_runbook_ready"] = True
        fake_st.session_state["ebay_workspace_saved_profiles"] = {
            "Ops Team": {
                "access_token": "tok-123",
                "status_filter": ["active"],
                "linked_only": True,
                "search": "silver",
                "use_date_filter": True,
                "listed_date_range": ["2026-04-01", "2026-04-02"],
            }
        }
        fake_st.set_selectbox("ebay_ops_profile_quick_select", "Ops Team")
        fake_st.set_button("ebay_ops_profile_apply_btn", True)
        user = SimpleNamespace(username="admin", role="admin")
        repo = SimpleNamespace()
        client = SimpleNamespace(is_configured=lambda: True, environment="production")
        fake_st.rerun = lambda: (_ for _ in ()).throw(RuntimeError("rerun"))

        with patch.object(ebay_ops, "st", fake_st), \
            patch.object(ebay_ops, "current_user", return_value=user), \
            patch.object(ebay_ops, "render_help_panel", return_value=None), \
            patch.object(ebay_ops, "render_active_ebay_context_banner", return_value=None), \
            patch.object(ebay_ops, "EbayClient", return_value=client), \
            patch.object(ebay_ops, "get_runtime_bool", return_value=False), \
            patch.object(ebay_ops, "get_runtime_str", return_value=""):
            with self.assertRaisesRegex(RuntimeError, "rerun"):
                ebay_ops.render_ebay_ops(repo)

        self.assertEqual(fake_st.session_state.get("ebay_workspace_access_token"), "tok-123")
        self.assertEqual(fake_st.session_state.get("ebay_ops_access_token"), "tok-123")
        self.assertEqual(fake_st.session_state.get("ebay_ops_status_filter"), ["active"])
        self.assertEqual(fake_st.session_state.get("ebay_ops_search_query"), "silver")
        self.assertEqual(fake_st.session_state.get("ebay_ops_use_date_filter"), True)
        self.assertEqual(fake_st.session_state.get("ebay_workspace_active_profile"), "Ops Team")
        self.assertTrue(any(c[0] == "success" and "Applied profile" in str(c[1][0]) for c in fake_st.calls))

    def test_render_listing_side_panel_empty_lineage(self):
        fake_st = _FakeSt()
        listing = SimpleNamespace(
            id=31,
            product_id=7,
            marketplace="ebay",
            external_listing_id="",
            marketplace_url="",
            marketplace_details="",
            listing_title="Coin",
            listing_price=12.0,
            review_status="pending",
            reviewed_at=None,
            reviewed_by="",
            listed_at=None,
            created_at=None,
            updated_at=None,
            listing_status="draft",
            quantity_listed=1,
        )
        repo = SimpleNamespace(
            db=SimpleNamespace(get=lambda _model, _id: SimpleNamespace(title="P")),
            list_media_assets_for_listing=lambda _id: [],
            list_sync_events_for_entity=lambda **kwargs: [],
            list_sync_runs=lambda **kwargs: [],
            list_sync_errors=lambda *_args, **_kwargs: [],
        )
        with patch.object(ebay_ops, "st", fake_st), \
            patch.object(ebay_ops, "render_workspace_empty_state", return_value=None) as empty_state, \
            patch.object(ebay_ops, "render_entity_timeline", return_value=None) as timeline:
            ebay_ops._render_listing_side_panel(repo, listing=listing, sku="SKU-1", panel_key_prefix="k")
        self.assertTrue(empty_state.called)
        self.assertTrue(timeline.called)
        self.assertTrue(any(c[0] == "caption" and "No marketplace details payload" in str(c[1][0]) for c in fake_st.calls))

    def test_render_listing_side_panel_with_lineage_and_media(self):
        fake_st = _FakeSt()
        listing = SimpleNamespace(
            id=41,
            product_id=8,
            marketplace="ebay",
            external_listing_id="E-41",
            marketplace_url="https://ebay.test/item/41",
            marketplace_details='{"x":1}',
            listing_title="Bar",
            listing_price=99.0,
            review_status="approved",
            reviewed_at=None,
            reviewed_by="ops",
            listed_at=None,
            created_at=None,
            updated_at=None,
            listing_status="active",
            quantity_listed=2,
        )
        event = SimpleNamespace(id=1, sync_run_id=77, action="push", status="success", message="ok", created_at=None)
        run = SimpleNamespace(id=77, provider="ebay", job_name="job", status="failed", retry_count=1)
        unresolved = [SimpleNamespace(resolved_at=None), SimpleNamespace(resolved_at=datetime(2026, 4, 2, 10, 0, 0))]
        media_row = SimpleNamespace(
            id=1,
            media_type="image",
            original_filename="a.jpg",
            content_type="image/jpeg",
            size_bytes=10,
            s3_url="https://x",
            uploaded_by="ops",
            created_at=None,
        )
        repo = SimpleNamespace(
            db=SimpleNamespace(get=lambda _model, _id: SimpleNamespace(title="P2")),
            list_media_assets_for_listing=lambda _id: [media_row],
            list_sync_events_for_entity=lambda **kwargs: [event],
            list_sync_runs=lambda **kwargs: [run],
            list_sync_errors=lambda *_args, **_kwargs: unresolved,
        )
        with patch.object(ebay_ops, "st", fake_st), \
            patch.object(ebay_ops, "render_workspace_empty_state", return_value=None), \
            patch.object(ebay_ops, "render_entity_timeline", return_value=None):
            ebay_ops._render_listing_side_panel(repo, listing=listing, sku="SKU-2", panel_key_prefix="k2")
        self.assertTrue(any(c[0] == "json" for c in fake_st.calls))
        self.assertGreaterEqual(sum(1 for c in fake_st.calls if c[0] == "dataframe"), 2)


if __name__ == "__main__":
    unittest.main()
