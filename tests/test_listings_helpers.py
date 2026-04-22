import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
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
    for name in ("shared", "workspace_shell", "entity_ops"):
        full_name = f"app.components.views.{name}"
        if full_name in sys.modules:
            continue
        mod_path = root / "app" / "components" / "views" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(full_name, mod_path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        sys.modules[full_name] = module


def _load_module():
    _bootstrap_views_package()
    root = Path(__file__).resolve().parents[1]
    module_path = root / "app" / "components" / "views" / "listings.py"
    spec = importlib.util.spec_from_file_location("test_listings_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


listings = _load_module()


class _FakeRepo:
    def __init__(self):
        self.upsert_calls = []
        self.update_calls = []

    def upsert_runtime_setting(self, **kwargs):
        self.upsert_calls.append(kwargs)

    def update_listing(self, listing_id, updates, actor):
        self.update_calls.append(
            {
                "listing_id": listing_id,
                "updates": updates,
                "actor": actor,
            }
        )


class ListingsHelperTests(unittest.TestCase):
    def test_orchestration_dependency_caption_when_queue_deferred(self):
        msg = listings._orchestration_dependency_caption(
            load_orchestration_queue=False,
            load_readiness_queue=True,
            load_readiness_evaluation=True,
        )
        self.assertIn("Load Listing Orchestration Queue", msg)

    def test_orchestration_dependency_caption_when_readiness_disabled(self):
        msg = listings._orchestration_dependency_caption(
            load_orchestration_queue=True,
            load_readiness_queue=False,
            load_readiness_evaluation=True,
        )
        self.assertIn("Load eBay Readiness Queue", msg)

    def test_orchestration_dependency_caption_when_readiness_eval_deferred(self):
        msg = listings._orchestration_dependency_caption(
            load_orchestration_queue=True,
            load_readiness_queue=True,
            load_readiness_evaluation=False,
        )
        self.assertIn("Load Readiness Evaluation", msg)

    def test_orchestration_dependency_caption_empty_when_ready(self):
        msg = listings._orchestration_dependency_caption(
            load_orchestration_queue=True,
            load_readiness_queue=True,
            load_readiness_evaluation=True,
        )
        self.assertEqual(msg, "")

    def test_known_unit_cost_sums_landed_components(self):
        product = types.SimpleNamespace(
            acquisition_cost=10.0,
            acquisition_tax_paid=1.5,
            acquisition_shipping_paid=0.75,
            acquisition_handling_paid=0.25,
        )
        self.assertEqual(listings._known_unit_cost(product), 12.5)
        self.assertEqual(listings._known_unit_cost(None), 0.0)

    def test_expected_net_score_computes_variance_bands(self):
        score = listings._expected_net_score(
            fee_estimate={
                "gross_total": 100.0,
                "estimated_total_fees": 12.0,
                "estimated_net_payout_before_shipping_cost": 88.0,
            },
            quantity=2,
            known_unit_cost=20.0,
            estimated_local_shipping_cost_per_item=5.0,
        )
        self.assertEqual(float(score.get("known_cogs_total") or 0.0), 40.0)
        self.assertEqual(float(score.get("estimated_local_shipping_total") or 0.0), 10.0)
        self.assertEqual(float(score.get("expected_net") or 0.0), 38.0)
        self.assertEqual(str(score.get("score") or ""), "strong")

    def test_photo_comp_created_listing_ids_prefers_supplied_rows_and_parses_ids(self):
        class _Repo:
            def list_audit_logs(self, limit=5000):  # pragma: no cover - should not be called
                raise AssertionError("list_audit_logs should not be called when audit_rows is supplied")

        rows = [
            types.SimpleNamespace(
                entity_type="navigation",
                action="photo_comp_product_draft_created",
                changes_json=json.dumps({"draft_listing_ids": [101, "102", "bad", None]}),
            ),
            types.SimpleNamespace(
                entity_type="navigation",
                action="photo_comp_product_draft_created",
                changes_json=json.dumps({"draft_listing_ids": ["103"]}),
            ),
            types.SimpleNamespace(
                entity_type="navigation",
                action="other_action",
                changes_json=json.dumps({"draft_listing_ids": [999]}),
            ),
            types.SimpleNamespace(
                entity_type="listing",
                action="photo_comp_product_draft_created",
                changes_json=json.dumps({"draft_listing_ids": [998]}),
            ),
        ]
        out = listings._photo_comp_created_listing_ids(_Repo(), audit_rows=rows)
        self.assertEqual(out, {101, 102, 103})

    def test_photo_comp_created_listing_ids_honors_explicit_empty_audit_rows(self):
        class _Repo:
            def list_audit_logs(self, limit=5000):  # pragma: no cover - should not be called
                raise AssertionError("list_audit_logs should not be called when explicit empty audit_rows is supplied")

        out = listings._photo_comp_created_listing_ids(_Repo(), audit_rows=[])
        self.assertEqual(out, set())

    def test_external_listing_id_owner_prefers_precomputed_map(self):
        class _Repo:
            def list_listings(self):  # pragma: no cover - should not be called
                raise AssertionError("list_listings should not be called when owner map is supplied")

        owner_map = {
            ("ebay", "A-123"): 44,
            ("shopify", "A-123"): 77,
        }
        owner = listings._external_listing_id_owner(
            _Repo(),
            marketplace="ebay",
            external_listing_id="A-123",
            exclude_listing_id=12,
            owner_by_market_and_external_id=owner_map,
        )
        self.assertEqual(owner, 44)

        same_listing = listings._external_listing_id_owner(
            _Repo(),
            marketplace="ebay",
            external_listing_id="A-123",
            exclude_listing_id=44,
            owner_by_market_and_external_id=owner_map,
        )
        self.assertIsNone(same_listing)

        missing = listings._external_listing_id_owner(
            _Repo(),
            marketplace="ebay",
            external_listing_id="MISSING",
            exclude_listing_id=1,
            owner_by_market_and_external_id=owner_map,
        )
        self.assertIsNone(missing)

    def test_publish_draft_contract_keyset_includes_business_critical_fields(self):
        keyset = set(listings.LISTINGS_EBAY_PUBLISH_DRAFT_SESSION_KEYS)
        self.assertIn("ebay_pub_category_id", keyset)
        self.assertIn("ebay_pub_dependency_preflight_result", keyset)
        self.assertIn("ebay_pub_category_query_seed_product_id", keyset)
        self.assertIn("ebay_pub_volume_pricing_json", keyset)
        self.assertIn("ebay_pub_access_token", keyset)

    def test_load_custom_listing_html_blocks_filters_invalid_entries(self):
        payload = json.dumps({"A": "<p>x</p>", "  ": "<p>bad</p>", "B": "", "C": " <p>y</p> "})
        with patch.object(listings, "get_runtime_str", return_value=payload):
            blocks = listings._load_custom_listing_html_blocks(_FakeRepo())
        self.assertEqual(blocks, {"A": "<p>x</p>", "C": "<p>y</p>"})

    def test_save_custom_listing_html_blocks_normalizes_and_persists(self):
        repo = _FakeRepo()
        listings._save_custom_listing_html_blocks(
            repo,
            actor="tester",
            blocks={" Header ": " <h1>hi</h1> ", "": "<p>x</p>", "Footer": ""},
        )
        self.assertEqual(len(repo.upsert_calls), 1)
        call = repo.upsert_calls[0]
        self.assertEqual(call["key"], listings.LISTING_HTML_BLOCKS_RUNTIME_KEY)
        self.assertEqual(call["value_type"], "json")
        self.assertEqual(call["actor"], "tester")
        saved = json.loads(call["value"])
        self.assertEqual(saved, {"Header": "<h1>hi</h1>"})

    def test_merged_listing_html_block_library_prefers_custom_over_defaults(self):
        custom = {"Golden Stackers Header": "<div>custom</div>", "Custom Block": "<p>ok</p>"}
        with patch.object(listings, "_load_custom_listing_html_blocks", return_value=custom):
            merged, loaded_custom = listings._merged_listing_html_block_library(_FakeRepo())
        self.assertEqual(loaded_custom, custom)
        self.assertEqual(merged["Golden Stackers Header"], "<div>custom</div>")
        self.assertEqual(merged["Custom Block"], "<p>ok</p>")
        self.assertIn("Shipping Policy", merged)

    def test_with_ai_grading_notes_appends_once(self):
        out = listings._with_ai_grading_notes(
            "Base description",
            grading_description="Likely MS63 with light contact marks.",
        )
        self.assertIn("AI Grading Notes:", out)
        self.assertIn("Likely MS63", out)

        out2 = listings._with_ai_grading_notes(
            out,
            grading_description="Should not duplicate",
        )
        self.assertEqual(out2.count("AI Grading Notes:"), 1)

    def test_product_ai_grading_description_handles_none(self):
        self.assertEqual(listings._product_ai_grading_description(None), "")
        row = types.SimpleNamespace(ai_grading_description="  UNC details, hairlines present  ")
        self.assertEqual(
            listings._product_ai_grading_description(row),
            "UNC details, hairlines present",
        )

    def test_ai_grading_prefill_status_messages(self):
        self.assertEqual(
            listings._ai_grading_prefill_status(current_value="", default_value=""),
            "",
        )
        self.assertIn(
            "prefilled",
            listings._ai_grading_prefill_status(
                current_value="MS63 details",
                default_value="MS63 details",
            ).lower(),
        )
        self.assertIn(
            "edited",
            listings._ai_grading_prefill_status(
                current_value="MS62 details",
                default_value="MS63 details",
            ).lower(),
        )
        self.assertIn(
            "available",
            listings._ai_grading_prefill_status(
                current_value="",
                default_value="MS63 details",
            ).lower(),
        )

    def test_queue_updates_preserving_form_updates_only_target_fields_by_default(self):
        fake_st = types.SimpleNamespace(
            session_state={
                "ebay_pub_category_id": "16679",
                "ebay_pub_fixed_price": 5.0,
                "ebay_pub_condition": "NEW",
                "ebay_pub_aspects_json": '{"Certification":["Uncertified"]}',
            }
        )
        with patch.object(listings, "st", fake_st):
            listings._queue_ebay_publish_updates_preserving_form(
                {"ebay_pub_aspects_json": '{"Certification":["PCGS"]}'},
                flash="updated",
            )
        pending = fake_st.session_state.get("ebay_pub_pending_updates") or {}
        self.assertNotIn("ebay_pub_category_id", pending)
        self.assertNotIn("ebay_pub_fixed_price", pending)
        self.assertNotIn("ebay_pub_condition", pending)
        self.assertEqual(pending.get("ebay_pub_aspects_json"), '{"Certification":["PCGS"]}')
        self.assertEqual(fake_st.session_state.get("ebay_pub_draft_flash"), "updated")
        self.assertTrue(bool(fake_st.session_state.get("ebay_pub_skip_signature_reset_once")))

    def test_queue_updates_preserving_form_keeps_explicit_preserve_keys(self):
        fake_st = types.SimpleNamespace(
            session_state={
                "ebay_pub_category_id": "16679",
                "ebay_pub_fixed_price": 5.0,
                "ebay_pub_condition": "NEW",
                "ebay_pub_aspects_json": '{"Certification":["Uncertified"]}',
            }
        )
        with patch.object(listings, "st", fake_st):
            listings._queue_ebay_publish_updates_preserving_form(
                {"ebay_pub_aspects_json": '{"Certification":["PCGS"]}'},
                flash="updated",
                preserve_keys=["ebay_pub_category_id", "ebay_pub_fixed_price", "ebay_pub_condition"],
            )
        pending = fake_st.session_state.get("ebay_pub_pending_updates") or {}
        self.assertEqual(pending.get("ebay_pub_category_id"), "16679")
        self.assertEqual(pending.get("ebay_pub_fixed_price"), 5.0)
        self.assertEqual(pending.get("ebay_pub_condition"), "NEW")
        self.assertEqual(pending.get("ebay_pub_aspects_json"), '{"Certification":["PCGS"]}')

    def test_build_ebay_offer_payload_fixed_price_includes_quantity_and_best_offer(self):
        payload = listings._build_ebay_offer_payload(
            sku="SKU-1",
            marketplace_id="EBAY_US",
            format_type="FIXED_PRICE",
            available_quantity=5,
            category_id="123",
            merchant_location_key="goldenstackers-main",
            listing_description="desc",
            listing_duration="GTC",
            payment_policy_id="pay",
            fulfillment_policy_id="ship",
            return_policy_id="ret",
            currency="USD",
            fixed_price=10.0,
            best_offer_enabled=True,
            best_offer_auto_accept=9.5,
            best_offer_minimum=9.0,
            auction_start_price=0.0,
            auction_reserve_price=0.0,
            auction_buy_now_price=0.0,
        )
        self.assertEqual(payload.get("format"), "FIXED_PRICE")
        self.assertEqual(payload.get("availableQuantity"), 5)
        self.assertEqual((payload.get("pricingSummary") or {}).get("price", {}).get("value"), "10.0")
        self.assertEqual(
            ((payload.get("listingPolicies") or {}).get("bestOfferTerms") or {}).get("bestOfferEnabled"),
            True,
        )

    def test_build_ebay_offer_payload_auction_omits_quantity_and_uses_auction_prices(self):
        payload = listings._build_ebay_offer_payload(
            sku="SKU-2",
            marketplace_id="EBAY_US",
            format_type="AUCTION",
            available_quantity=99,
            category_id="456",
            merchant_location_key="goldenstackers-main",
            listing_description="desc",
            listing_duration="DAYS_7",
            payment_policy_id="pay",
            fulfillment_policy_id="ship",
            return_policy_id="ret",
            currency="USD",
            fixed_price=0.0,
            best_offer_enabled=False,
            best_offer_auto_accept=0.0,
            best_offer_minimum=0.0,
            auction_start_price=12.0,
            auction_reserve_price=15.0,
            auction_buy_now_price=20.0,
        )
        self.assertEqual(payload.get("format"), "AUCTION")
        self.assertNotIn("availableQuantity", payload)
        pricing = payload.get("pricingSummary") or {}
        self.assertEqual((pricing.get("auctionStartPrice") or {}).get("value"), "12.0")
        self.assertEqual((pricing.get("auctionReservePrice") or {}).get("value"), "15.0")
        self.assertEqual((pricing.get("price") or {}).get("value"), "20.0")

    def test_listing_publish_meta_reads_publish_dict(self):
        row = types.SimpleNamespace(
            marketplace_details=json.dumps(
                {
                    "notes": "x",
                    "ebay_publish": {
                        "offer_id": "123",
                        "last_publish_error": "bad",
                    },
                }
            )
        )
        payload = listings._listing_publish_meta(row)
        self.assertEqual(payload.get("offer_id"), "123")
        self.assertEqual(payload.get("last_publish_error"), "bad")

    def test_persist_listing_publish_error_writes_metadata(self):
        repo = _FakeRepo()
        listing = types.SimpleNamespace(
            id=77,
            marketplace_details=json.dumps({"notes": "x", "ebay_publish": {"offer_id": "abc"}}),
        )
        listings._persist_listing_publish_error(
            repo,
            listing,
            actor="tester",
            error_message="failed stage",
            stage="create_offer",
            context={"post_mode": "Publish Live Listing"},
        )
        self.assertEqual(len(repo.update_calls), 1)
        call = repo.update_calls[0]
        self.assertEqual(call["listing_id"], 77)
        self.assertEqual(call["actor"], "tester")
        updates = call["updates"]
        self.assertIn("marketplace_details", updates)
        payload = json.loads(str(updates["marketplace_details"]))
        publish = payload.get("ebay_publish") or {}
        self.assertEqual(publish.get("offer_id"), "abc")
        self.assertEqual(publish.get("last_publish_error"), "failed stage")
        self.assertEqual(publish.get("last_publish_error_stage"), "create_offer")
        self.assertEqual(publish.get("last_publish_error_context"), {"post_mode": "Publish Live Listing"})
        self.assertTrue(str(publish.get("last_publish_error_at") or "").strip())

    def test_build_publish_draft_payload_uses_shared_contract_and_filters_state_keys(self):
        class _FakeSt:
            def __init__(self):
                self.session_state = {
                    "ebay_pub_title": "Draft Title",
                    "ebay_pub_category_id": "16679",
                    "ignore_me": "x",
                }

        fake_st = _FakeSt()
        with patch.object(listings, "st", fake_st):
            payload = listings._listings_build_ebay_publish_draft_payload(
                listing_id=42,
                listing_signature="sig-42",
                state_keys=["ebay_pub_title", "ebay_pub_category_id"],
            )

        contract = payload.get("contract") or {}
        self.assertEqual(contract.get("type"), "listing_draft")
        self.assertEqual(contract.get("version"), 1)
        self.assertEqual(payload.get("signature"), "sig-42")
        self.assertEqual((payload.get("context") or {}).get("selected_listing_id"), 42)
        self.assertEqual((payload.get("state") or {}).get("ebay_pub_title"), "Draft Title")
        self.assertNotIn("ignore_me", payload.get("state") or {})

    def test_apply_publish_draft_payload_only_sets_allowed_keys(self):
        class _FakeSt:
            def __init__(self):
                self.session_state = {"existing": 1}

        fake_st = _FakeSt()
        payload = {
            "contract": {"type": "listing_draft", "version": 1},
            "state": {
                "ebay_pub_title": "Applied Title",
                "ebay_pub_category_id": "11111",
                "forbidden": "nope",
            },
            "context": {"selected_listing_id": 77},
            "signature": "sig-77",
        }
        with patch.object(listings, "st", fake_st):
            listings._listings_apply_ebay_publish_draft_payload(
                payload,
                state_keys=["ebay_pub_title", "ebay_pub_category_id"],
            )
        self.assertEqual(fake_st.session_state.get("ebay_pub_title"), "Applied Title")
        self.assertEqual(fake_st.session_state.get("ebay_pub_category_id"), "11111")
        self.assertNotIn("forbidden", fake_st.session_state)

    def test_apply_publish_draft_payload_defers_locked_widget_keys(self):
        class _LockedSessionState(dict):
            def __setitem__(self, key, value):
                if key == "ebay_pub_category_id":
                    raise listings.StreamlitAPIException("locked")
                return super().__setitem__(key, value)

        class _FakeSt:
            def __init__(self):
                self.session_state = _LockedSessionState()

        payload = {
            "contract": {"type": "listing_draft", "version": 1},
            "state": {
                "ebay_pub_title": "Applied Title",
                "ebay_pub_category_id": "11111",
            },
            "context": {"selected_listing_id": 77},
            "signature": "sig-77",
        }
        with patch.object(listings, "st", _FakeSt()) as fake_st:
            listings._listings_apply_ebay_publish_draft_payload(
                payload,
                state_keys=["ebay_pub_title", "ebay_pub_category_id"],
            )
            self.assertEqual(fake_st.session_state.get("ebay_pub_title"), "Applied Title")
            self.assertEqual(
                fake_st.session_state.get("ebay_pub_pending_updates"),
                {"ebay_pub_category_id": "11111"},
            )
            self.assertIn("deferred", str(fake_st.session_state.get("ebay_pub_draft_flash") or "").lower())

    def test_apply_pending_publish_updates_filters_allowed_keys(self):
        class _FakeSt:
            def __init__(self):
                self.session_state = {
                    "ebay_pub_pending_updates": {
                        "ebay_pub_category_id": "16679",
                        "ebay_pub_fixed_price": 12.5,
                        "bad_key": "drop",
                    }
                }

        fake_st = _FakeSt()
        with patch.object(listings, "st", fake_st):
            listings._listings_apply_pending_ebay_publish_updates(
                allowed_keys={"ebay_pub_category_id", "ebay_pub_fixed_price"}
            )
        self.assertEqual(fake_st.session_state.get("ebay_pub_category_id"), "16679")
        self.assertEqual(fake_st.session_state.get("ebay_pub_fixed_price"), 12.5)
        self.assertNotIn("bad_key", fake_st.session_state)
        self.assertNotIn("ebay_pub_pending_updates", fake_st.session_state)

    def test_pending_updates_override_prior_draft_values_for_allowed_keys(self):
        class _FakeSt:
            def __init__(self):
                self.session_state = {
                    "ebay_pub_title": "Old Draft Title",
                    "ebay_pub_category_id": "100",
                    "ebay_pub_pending_updates": {
                        "ebay_pub_title": "Pending Title",
                        "ebay_pub_category_id": "16679",
                    },
                }

        fake_st = _FakeSt()
        with patch.object(listings, "st", fake_st):
            listings._listings_apply_pending_ebay_publish_updates(
                allowed_keys={"ebay_pub_title", "ebay_pub_category_id"}
            )
        self.assertEqual(fake_st.session_state.get("ebay_pub_title"), "Pending Title")
        self.assertEqual(fake_st.session_state.get("ebay_pub_category_id"), "16679")

    def test_apply_pending_publish_updates_noop_when_missing_or_invalid(self):
        class _FakeSt:
            def __init__(self):
                self.session_state = {"ebay_pub_title": "Keep Me"}

        fake_st = _FakeSt()
        with patch.object(listings, "st", fake_st):
            listings._listings_apply_pending_ebay_publish_updates(
                allowed_keys={"ebay_pub_title", "ebay_pub_category_id"}
            )
        self.assertEqual(fake_st.session_state.get("ebay_pub_title"), "Keep Me")

    def test_apply_pending_publish_updates_defers_locked_widget_keys(self):
        class _LockedSessionState(dict):
            def __setitem__(self, key, value):
                if key == "ebay_pub_category_id":
                    raise listings.StreamlitAPIException("locked")
                return super().__setitem__(key, value)

        class _FakeSt:
            def __init__(self):
                self.session_state = _LockedSessionState(
                    {
                        "ebay_pub_pending_updates": {
                            "ebay_pub_title": "Pending Title",
                            "ebay_pub_category_id": "16679",
                        }
                    }
                )

        fake_st = _FakeSt()
        with patch.object(listings, "st", fake_st):
            listings._listings_apply_pending_ebay_publish_updates(
                allowed_keys={"ebay_pub_title", "ebay_pub_category_id"}
            )
        self.assertEqual(fake_st.session_state.get("ebay_pub_title"), "Pending Title")
        self.assertEqual(
            fake_st.session_state.get("ebay_pub_pending_updates"),
            {"ebay_pub_category_id": "16679"},
        )
        self.assertIn("deferred", str(fake_st.session_state.get("ebay_pub_draft_flash") or "").lower())

        fake_st.session_state["ebay_pub_pending_updates"] = "invalid"
        with patch.object(listings, "st", fake_st):
            listings._listings_apply_pending_ebay_publish_updates(
                allowed_keys={"ebay_pub_title", "ebay_pub_category_id"}
            )
        self.assertEqual(fake_st.session_state.get("ebay_pub_title"), "Pending Title")

    def test_queue_ebay_publish_category_id_update_sets_pending_only(self):
        class _FakeSt:
            def __init__(self):
                self.session_state = {"ebay_pub_title": "Current"}

        fake_st = _FakeSt()
        with patch.object(listings, "st", fake_st):
            listings._queue_ebay_publish_category_id_update("16679")
        self.assertEqual(fake_st.session_state.get("ebay_pub_title"), "Current")
        self.assertEqual(
            fake_st.session_state.get("ebay_pub_pending_updates"),
            {
                "ebay_pub_category_id": "16679",
            },
        )
        self.assertIn("16679", str(fake_st.session_state.get("ebay_pub_draft_flash") or ""))
        self.assertTrue(bool(fake_st.session_state.get("ebay_pub_skip_signature_reset_once")))

    def test_queue_ebay_publish_updates_merges_and_sets_flash(self):
        class _FakeSt:
            def __init__(self):
                self.session_state = {"ebay_pub_pending_updates": {"ebay_pub_title": "Prior"}}

        fake_st = _FakeSt()
        with patch.object(listings, "st", fake_st):
            listings._queue_ebay_publish_updates(
                {"ebay_pub_category_id": "16679", "  ": "drop"},
                flash="Queued update.",
            )
        self.assertEqual(
            fake_st.session_state.get("ebay_pub_pending_updates"),
            {"ebay_pub_title": "Prior", "ebay_pub_category_id": "16679"},
        )
        self.assertEqual(fake_st.session_state.get("ebay_pub_draft_flash"), "Queued update.")

    def test_safe_session_set_sets_missing_and_handles_locked_keys(self):
        class _LockedSessionState(dict):
            def __setitem__(self, key, value):
                if key == "locked_key":
                    raise listings.StreamlitAPIException("locked")
                return super().__setitem__(key, value)

        class _FakeSt:
            def __init__(self):
                self.session_state = _LockedSessionState({"existing": 1})

        fake_st = _FakeSt()
        with patch.object(listings, "st", fake_st):
            self.assertTrue(listings._safe_session_set("new_key", 2))
            self.assertFalse(listings._safe_session_set("existing", 3, only_if_missing=True))
            self.assertFalse(listings._safe_session_set("locked_key", 9))
        self.assertEqual(fake_st.session_state.get("new_key"), 2)
        self.assertEqual(fake_st.session_state.get("existing"), 1)
        self.assertNotIn("locked_key", fake_st.session_state)

    def test_filter_listing_rows_base_applies_query_marketplace_status_origin_and_archive(self):
        rows = [
            {
                "id": 1,
                "title": "Alpha Coin",
                "external_listing_id": "A-1",
                "marketplace": "ebay",
                "status": "draft",
                "origin": "photo_comp_draft",
                "archived": False,
            },
            {
                "id": 2,
                "title": "Beta Coin",
                "external_listing_id": "B-2",
                "marketplace": "ebay",
                "status": "active",
                "origin": "other",
                "archived": False,
            },
            {
                "id": 3,
                "title": "Gamma Coin",
                "external_listing_id": "C-3",
                "marketplace": "shopify",
                "status": "draft",
                "origin": "photo_comp_draft",
                "archived": True,
            },
        ]
        filtered = listings._filter_listing_rows_base(
            rows,
            query="alpha",
            marketplaces={"ebay"},
            statuses={"draft"},
            origin_filter="photo_comp_draft",
            include_archived=False,
        )
        self.assertEqual([int(r["id"]) for r in filtered], [1])

    def test_filter_listing_rows_base_treats_missing_origin_as_other(self):
        rows = [
            {
                "id": 10,
                "title": "No origin row",
                "external_listing_id": "N-10",
                "marketplace": "ebay",
                "status": "draft",
                "archived": False,
            }
        ]
        filtered_other = listings._filter_listing_rows_base(
            rows,
            query="",
            marketplaces=set(),
            statuses=set(),
            origin_filter="other",
            include_archived=False,
        )
        filtered_photo_comp = listings._filter_listing_rows_base(
            rows,
            query="",
            marketplaces=set(),
            statuses=set(),
            origin_filter="photo_comp_draft",
            include_archived=False,
        )
        self.assertEqual([int(r["id"]) for r in filtered_other], [10])
        self.assertEqual(filtered_photo_comp, [])

    def test_filter_listing_objects_base_applies_filters_and_origin_resolver(self):
        rows = [
            types.SimpleNamespace(
                id=1,
                listing_title="Alpha",
                external_listing_id="A-1",
                marketplace="ebay",
                listing_status="draft",
                marketplace_details="{}",
            ),
            types.SimpleNamespace(
                id=2,
                listing_title="Beta",
                external_listing_id="B-2",
                marketplace="shopify",
                listing_status="active",
                marketplace_details="{}",
            ),
        ]
        filtered = listings._filter_listing_objects_base(
            rows,
            query="alpha",
            marketplaces={"ebay"},
            statuses={"draft"},
            origin_filter="photo_comp_draft",
            include_archived=False,
            resolve_origin=lambda obj: "photo_comp_draft" if int(getattr(obj, "id", 0) or 0) == 1 else "other",
        )
        self.assertEqual([int(getattr(r, "id", 0)) for r in filtered], [1])

    def test_maybe_hydrate_listing_format_diagnostics_runs_only_when_required(self):
        rows = [{"id": 10, "format_hint": ""}, {"id": 11, "format_hint": ""}]
        hydrated_ids: list[int] = []

        def _hydrate(target_rows):
            hydrated_ids.extend(int(r["id"]) for r in target_rows)
            for r in target_rows:
                r["format_hint"] = "Fixed Missing BIN" if int(r["id"]) == 10 else ""

        listings._maybe_hydrate_listing_format_diagnostics(
            rows,
            diagnostics_required=False,
            hydrate_rows=_hydrate,
        )
        self.assertEqual(hydrated_ids, [])
        self.assertEqual([str(r.get("format_hint") or "") for r in rows], ["", ""])

        listings._maybe_hydrate_listing_format_diagnostics(
            rows,
            diagnostics_required=True,
            hydrate_rows=_hydrate,
        )
        self.assertEqual(hydrated_ids, [10, 11])
        self.assertEqual([str(r.get("format_hint") or "") for r in rows], ["Fixed Missing BIN", ""])

    def test_filter_listing_rows_with_format_issues_keeps_only_rows_with_hint(self):
        rows = [
            {"id": 1, "format_hint": ""},
            {"id": 2, "format_hint": "Auction Missing Start"},
            {"id": 3, "format_hint": "  "},
            {"id": 4, "format_hint": "Reserve < Start"},
        ]
        filtered = listings._filter_listing_rows_with_format_issues(rows)
        self.assertEqual([int(r["id"]) for r in filtered], [2, 4])


if __name__ == "__main__":
    unittest.main()
