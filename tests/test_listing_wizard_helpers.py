import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


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
    module_path = root / "app" / "components" / "views" / "listing_wizard.py"
    spec = importlib.util.spec_from_file_location("test_listing_wizard_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


listing_wizard = _load_module()


class ListingWizardHelperTests(unittest.TestCase):
    def test_condition_options_use_loaded_category_policy_and_preserve_current_invalid_value(self):
        rows = [
            {"condition": "USED_EXCELLENT", "label": "Used", "condition_id": "3000"},
            {"condition": "USED_ACCEPTABLE", "label": "Acceptable", "condition_id": "6000"},
        ]

        self.assertEqual(
            listing_wizard._wizard_condition_options(rows, "NEW"),
            ["USED_EXCELLENT", "USED_ACCEPTABLE", "NEW"],
        )
        labels = listing_wizard._wizard_condition_option_labels(rows, "NEW")
        self.assertIn("eBay ID 3000", labels["USED_EXCELLENT"])
        self.assertIn("not in loaded category policy", labels["NEW"])
        self.assertFalse(listing_wizard._wizard_is_condition_valid_for_loaded_policy(rows, "NEW"))
        self.assertTrue(listing_wizard._wizard_is_condition_valid_for_loaded_policy(rows, "USED_ACCEPTABLE"))

    def test_load_listing_wizard_product_rows_merges_recent_search_and_selected(self):
        recent = [
            types.SimpleNamespace(id=3, sku="RECENT-3", title="Recent 3"),
            types.SimpleNamespace(id=2, sku="RECENT-2", title="Recent 2"),
        ]
        search = [
            types.SimpleNamespace(id=9, sku="OLD-9", title="Older Search Match"),
            types.SimpleNamespace(id=2, sku="RECENT-2", title="Recent 2"),
        ]
        selected = [types.SimpleNamespace(id=20, sku="OLD-20", title="Saved Draft Product")]

        class Repo:
            def __init__(self):
                self.calls = []

            def list_products(self, **kwargs):
                self.calls.append(kwargs)
                if kwargs.get("product_ids"):
                    return selected
                if kwargs.get("search_query"):
                    return search
                return recent

        repo = Repo()
        rows = listing_wizard._load_listing_wizard_product_rows(
            repo,
            search_query="older",
            selected_product_id=20,
            recent_limit=2,
            search_limit=10,
        )

        self.assertEqual([row.id for row in rows], [20, 9, 2, 3])
        self.assertEqual(repo.calls[0]["limit"], 2)
        self.assertEqual(repo.calls[1]["search_query"], "older")
        self.assertEqual(repo.calls[2]["product_ids"], [20])

    def test_load_listing_wizard_product_rows_skips_search_when_blank(self):
        class Repo:
            def __init__(self):
                self.calls = []

            def list_products(self, **kwargs):
                self.calls.append(kwargs)
                return [types.SimpleNamespace(id=1, sku="SKU", title="Title")]

        repo = Repo()
        rows = listing_wizard._load_listing_wizard_product_rows(
            repo,
            search_query="",
            selected_product_id=None,
            recent_limit=75,
        )

        self.assertEqual([row.id for row in rows], [1])
        self.assertEqual(len(repo.calls), 1)
        self.assertEqual(repo.calls[0]["limit"], 75)

    def test_wizard_promote_direct_post_retry_metadata_keeps_retry_identity(self):
        publish_meta = {"format": "AUCTION", "offer_id": "old-offer"}

        updated = listing_wizard._wizard_promote_direct_post_retry_metadata(
            publish_meta,
            {
                "inventory_sku": "GS-CO-CO-26120-604B-L140",
                "product_sku": "GS-CO-CO-26120-604B",
                "offer_id": "new-offer",
            },
        )

        self.assertEqual(updated["format"], "AUCTION")
        self.assertEqual(updated["inventory_sku"], "GS-CO-CO-26120-604B-L140")
        self.assertEqual(updated["product_sku"], "GS-CO-CO-26120-604B")
        self.assertEqual(updated["offer_id"], "new-offer")
        self.assertEqual(publish_meta["offer_id"], "old-offer")

    def test_wizard_promote_direct_post_retry_metadata_ignores_blank_context_values(self):
        updated = listing_wizard._wizard_promote_direct_post_retry_metadata(
            {
                "inventory_sku": "EXISTING-SKU",
                "product_sku": "EXISTING-PRODUCT",
                "offer_id": "EXISTING-OFFER",
            },
            {"inventory_sku": "", "product_sku": None, "offer_id": "   "},
        )

        self.assertEqual(updated["inventory_sku"], "EXISTING-SKU")
        self.assertEqual(updated["product_sku"], "EXISTING-PRODUCT")
        self.assertEqual(updated["offer_id"], "EXISTING-OFFER")

    def test_build_listing_bundle_metadata_for_single_product_lot(self):
        product = types.SimpleNamespace(
            id=42,
            sku="COIN-42",
            title="Copper Coin",
            current_quantity=20,
        )

        payload = listing_wizard._build_listing_bundle_metadata(
            enabled=True,
            primary_product=product,
            units_per_listing=10,
            available_lots=2,
        )

        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["kind"], "single_product_lot")
        self.assertEqual(payload["primary_product_id"], 42)
        self.assertEqual(payload["units_per_listing_total"], 10)
        self.assertEqual(payload["available_lots"], 2)
        self.assertEqual(payload["inventory_units_committed"], 20)
        self.assertEqual(payload["components"][0]["quantity_per_listing"], 10)

    def test_build_listing_bundle_metadata_for_mixed_product_bundle(self):
        product = types.SimpleNamespace(
            id=42,
            sku="COIN-42",
            title="Copper Coin",
            current_quantity=20,
        )

        payload = listing_wizard._build_listing_bundle_metadata(
            enabled=True,
            primary_product=product,
            units_per_listing=2,
            available_lots=3,
            additional_components=[
                {
                    "product_id": 43,
                    "sku": "ROUND-43",
                    "title": "Copper Round",
                    "quantity_per_listing": 4,
                    "current_quantity": 20,
                }
            ],
        )

        self.assertEqual(payload["kind"], "mixed_product_bundle")
        self.assertEqual(payload["units_per_listing_total"], 6)
        self.assertEqual(payload["inventory_units_committed"], 18)
        self.assertEqual([row["product_id"] for row in payload["components"]], [42, 43])

    def test_bundle_expected_unit_cost_sums_components(self):
        payload = {
            "enabled": True,
            "components": [
                {"product_id": 1, "quantity_per_listing": 2},
                {"product_id": 2, "quantity_per_listing": 3},
            ],
        }
        product_by_id = {
            1: types.SimpleNamespace(
                acquisition_cost=5,
                acquisition_tax_paid=1,
                acquisition_shipping_paid=0,
                acquisition_handling_paid=0,
            ),
            2: types.SimpleNamespace(
                acquisition_cost=10,
                acquisition_tax_paid=0,
                acquisition_shipping_paid=2,
                acquisition_handling_paid=0,
            ),
        }

        self.assertEqual(listing_wizard._bundle_expected_unit_cost(payload, product_by_id), 48.0)

    def test_build_listing_bundle_metadata_disabled_shape(self):
        payload = listing_wizard._build_listing_bundle_metadata(
            enabled=False,
            primary_product=None,
            units_per_listing=10,
            available_lots=3,
        )

        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["components"], [])
        self.assertEqual(payload["available_lots"], 3)

    def test_resolve_vision_mime_normalizes_octet_stream_to_detected_jpeg(self):
        jpeg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00"
        self.assertEqual(
            listing_wizard._resolve_vision_mime(jpeg_bytes, "application/octet-stream"),
            "image/jpeg",
        )

    def test_resolve_vision_mime_rejects_svg(self):
        svg_bytes = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"
        self.assertEqual(
            listing_wizard._resolve_vision_mime(svg_bytes, "image/svg+xml"),
            "",
        )

    def test_ai_grading_prefill_status_messages(self):
        self.assertEqual(
            listing_wizard._ai_grading_prefill_status(current_value="", default_value=""),
            "",
        )
        self.assertIn(
            "prefilled",
            listing_wizard._ai_grading_prefill_status(
                current_value="MS63 details",
                default_value="MS63 details",
            ).lower(),
        )
        self.assertIn(
            "edited",
            listing_wizard._ai_grading_prefill_status(
                current_value="MS62 details",
                default_value="MS63 details",
            ).lower(),
        )
        self.assertIn(
            "available",
            listing_wizard._ai_grading_prefill_status(
                current_value="",
                default_value="MS63 details",
            ).lower(),
        )

    def test_safe_price_float_parses_currency_and_ranges(self):
        self.assertEqual(listing_wizard._safe_price_float("$12.50"), 12.5)
        self.assertEqual(listing_wizard._safe_price_float("12.50 to 15.00"), 12.5)
        self.assertEqual(listing_wizard._safe_price_float("12.50-15.00"), 12.5)
        self.assertEqual(listing_wizard._safe_price_float("not-a-number", default=7.0), 7.0)

    def test_known_unit_cost_sums_landed_components(self):
        product = types.SimpleNamespace(
            product_cost=0.0,
            acquisition_cost=10.0,
            acquisition_tax_paid=1.5,
            acquisition_shipping_paid=0.75,
            acquisition_handling_paid=0.25,
        )
        explicit = types.SimpleNamespace(
            product_cost=9.25,
            acquisition_cost=10.0,
            acquisition_tax_paid=1.5,
            acquisition_shipping_paid=0.75,
            acquisition_handling_paid=0.25,
        )
        self.assertEqual(listing_wizard._known_unit_cost(product), 12.5)
        self.assertEqual(listing_wizard._known_unit_cost(explicit), 9.25)
        self.assertEqual(listing_wizard._known_unit_cost(None), 0.0)

    def test_order_media_rows_for_primary_prefers_media_id_or_upload_filename(self):
        rows = [
            types.SimpleNamespace(id=1, original_filename="a.jpg"),
            types.SimpleNamespace(id=2, original_filename="b.jpg"),
            types.SimpleNamespace(id=3, original_filename="c.jpg"),
        ]
        by_id = listing_wizard._wizard_order_media_rows_for_primary(rows, "media:2")
        self.assertEqual([row.id for row in by_id], [2, 1, 3])

        by_upload = listing_wizard._wizard_order_media_rows_for_primary(rows, "upload:c.jpg")
        self.assertEqual([row.id for row in by_upload], [3, 1, 2])

        unchanged = listing_wizard._wizard_order_media_rows_for_primary(rows, "media:not-int")
        self.assertEqual([row.id for row in unchanged], [1, 2, 3])

    def test_primary_image_metadata_uses_first_ordered_wizard_media(self):
        rows = [
            types.SimpleNamespace(id=3, original_filename="c.jpg"),
            types.SimpleNamespace(id=1, original_filename="a.jpg"),
        ]
        payload = listing_wizard._wizard_primary_image_metadata(rows, "upload:c.jpg")
        self.assertEqual(payload["primary_image_ref"], "upload:c.jpg")
        self.assertEqual(payload["primary_image_media_id"], 3)
        self.assertEqual(payload["primary_image_filename"], "c.jpg")

        empty = listing_wizard._wizard_primary_image_metadata([], "")
        self.assertEqual(empty["primary_image_ref"], "")
        self.assertEqual(empty["primary_image_media_id"], 0)
        self.assertEqual(empty["primary_image_filename"], "")

    def test_create_eps_image_with_retry_retries_transient_file_upload(self):
        media = types.SimpleNamespace(id=3, original_filename="front.jpg", content_type="image/jpeg", s3_url="")
        ebay = types.SimpleNamespace(
            create_image_from_file=Mock(
                side_effect=[
                    RuntimeError("503 Server Error: Service Unavailable"),
                    {"image": {"imageUrl": "https://i.ebayimg.com/front.jpg"}},
                ]
            ),
            create_image_from_url=Mock(),
        )

        with patch.object(listing_wizard, "load_media_bytes", return_value=(b"img", "image/jpeg", "")), patch.object(
            listing_wizard.time, "sleep", return_value=None
        ):
            url, meta = listing_wizard._wizard_create_eps_image_with_retry(
                ebay=ebay,
                access_token="tok",
                media=media,
                storage=None,
            )

        self.assertEqual(url, "https://i.ebayimg.com/front.jpg")
        self.assertEqual(meta["mode"], "file_upload")
        self.assertEqual(meta["attempts"], 2)
        self.assertEqual(ebay.create_image_from_file.call_count, 2)
        self.assertEqual(ebay.create_image_from_url.call_count, 0)

    def test_create_eps_image_with_retry_uses_url_import_as_eps_only_fallback(self):
        media = types.SimpleNamespace(
            id=4,
            original_filename="back.jpg",
            content_type="image/jpeg",
            s3_url="https://cdn.example/back.jpg",
        )
        ebay = types.SimpleNamespace(
            create_image_from_file=Mock(side_effect=RuntimeError("400 Bad Request")),
            create_image_from_url=Mock(return_value={"imageUrl": "https://i.ebayimg.com/back.jpg"}),
        )

        with patch.object(listing_wizard, "load_media_bytes", return_value=(b"img", "image/jpeg", "")):
            url, meta = listing_wizard._wizard_create_eps_image_with_retry(
                ebay=ebay,
                access_token="tok",
                media=media,
                storage=None,
            )

        self.assertEqual(url, "https://i.ebayimg.com/back.jpg")
        self.assertEqual(meta["mode"], "url_import")
        self.assertEqual(ebay.create_image_from_url.call_count, 1)

    def test_select_ebay_video_media_prefers_first_supported_video(self):
        rows = [
            types.SimpleNamespace(id=1, media_type="image", original_filename="front.jpg", content_type="image/jpeg"),
            types.SimpleNamespace(id=2, media_type="video", original_filename="clip.mov", content_type="video/quicktime"),
            types.SimpleNamespace(id=3, media_type="video", original_filename="clip.mp4", content_type="application/octet-stream"),
            types.SimpleNamespace(id=4, media_type="video", original_filename="second.mp4", content_type="video/mp4"),
        ]

        selected = listing_wizard._wizard_select_ebay_video_media(rows)

        self.assertIsNotNone(selected)
        self.assertEqual(selected.id, 2)

    def test_video_upload_warning_notes_missing_linked_supported_video_when_enabled(self):
        self.assertEqual(listing_wizard._wizard_video_upload_warning(False, []), "")
        self.assertIn(
            "no video media was linked",
            listing_wizard._wizard_video_upload_warning(True, []),
        )
        self.assertIn(
            "no supported MP4/MOV video",
            listing_wizard._wizard_video_upload_warning(
                True,
                [types.SimpleNamespace(id=1, media_type="video", original_filename="clip.webm", content_type="video/webm")],
            ),
        )
        self.assertEqual(
            listing_wizard._wizard_video_upload_warning(
                True,
                [types.SimpleNamespace(id=2, media_type="video", original_filename="clip.mov", content_type="video/quicktime")],
            ),
            "",
        )

    def test_upload_ebay_video_with_retry_uploads_and_waits_for_live(self):
        media = types.SimpleNamespace(
            id=9,
            media_type="video",
            original_filename="demo.mp4",
            content_type="video/mp4",
        )
        ebay = types.SimpleNamespace(
            create_video=Mock(return_value="VID-123"),
            upload_video=Mock(),
            get_video=Mock(side_effect=[{"status": "PROCESSING"}, {"status": "LIVE"}]),
        )

        with patch.object(
            listing_wizard,
            "load_media_bytes",
            return_value=(b"video-bytes", "video/mp4", ""),
        ), patch.object(listing_wizard.time, "sleep", return_value=None):
            video_id, meta = listing_wizard._wizard_upload_ebay_video_with_retry(
                ebay=ebay,
                access_token="tok",
                media=media,
                storage=None,
                listing_title="Demo listing",
                status_sleep_seconds=0,
            )

        self.assertEqual(video_id, "VID-123")
        self.assertEqual(meta["media_asset_id"], 9)
        self.assertEqual(meta["status"], "LIVE")
        ebay.create_video.assert_called_once()
        ebay.upload_video.assert_called_once()
        self.assertEqual(ebay.get_video.call_count, 2)

    def test_upload_ebay_video_with_retry_retries_transient_create_error(self):
        media = types.SimpleNamespace(
            id=10,
            media_type="video",
            original_filename="retry.mp4",
            content_type="video/mp4",
        )
        ebay = types.SimpleNamespace(
            create_video=Mock(side_effect=[RuntimeError("503 Service Unavailable"), "VID-456"]),
            upload_video=Mock(),
            get_video=Mock(return_value={"status": "LIVE"}),
        )

        with patch.object(
            listing_wizard,
            "load_media_bytes",
            return_value=(b"video-bytes", "video/mp4", ""),
        ), patch.object(listing_wizard.time, "sleep", return_value=None):
            video_id, meta = listing_wizard._wizard_upload_ebay_video_with_retry(
                ebay=ebay,
                access_token="tok",
                media=media,
                storage=None,
                listing_title="Retry listing",
                status_sleep_seconds=0,
            )

        self.assertEqual(video_id, "VID-456")
        self.assertEqual(meta["attempts"], 2)
        self.assertEqual(ebay.create_video.call_count, 2)

    def test_upload_ebay_video_with_retry_converts_mov_before_upload(self):
        media = types.SimpleNamespace(
            id=11,
            media_type="video",
            original_filename="walkaround.mov",
            content_type="video/quicktime",
        )
        ebay = types.SimpleNamespace(
            create_video=Mock(return_value="VID-MOV"),
            upload_video=Mock(),
            get_video=Mock(return_value={"status": "LIVE"}),
        )

        with patch.object(
            listing_wizard,
            "load_media_bytes",
            return_value=(b"mov-bytes", "video/quicktime", ""),
        ), patch.object(listing_wizard, "transcode_mov_to_mp4", return_value=b"mp4-bytes"), patch.object(
            listing_wizard.time, "sleep", return_value=None
        ):
            video_id, meta = listing_wizard._wizard_upload_ebay_video_with_retry(
                ebay=ebay,
                access_token="tok",
                media=media,
                storage=None,
                listing_title="MOV listing",
                status_sleep_seconds=0,
            )

        self.assertEqual(video_id, "VID-MOV")
        self.assertEqual(meta["filename"], "walkaround.mp4")
        self.assertEqual(meta["original_filename"], "walkaround.mov")
        self.assertEqual(meta["converted_from"], "mov")
        ebay.create_video.assert_called_once()
        self.assertEqual(ebay.create_video.call_args.kwargs["size_bytes"], len(b"mp4-bytes"))
        self.assertNotIn("content_type", ebay.upload_video.call_args.kwargs)

    def test_verify_inventory_video_ids_confirms_retained_video_id(self):
        ebay = types.SimpleNamespace(
            get_inventory_item=Mock(
                side_effect=[
                    {"product": {"videoIds": []}},
                    {"product": {"videoIds": ["VID-MOV"]}},
                ]
            )
        )

        with patch.object(listing_wizard.time, "sleep", return_value=None):
            result = listing_wizard._wizard_verify_inventory_video_ids(
                ebay=ebay,
                access_token="tok",
                sku="SKU1",
                expected_video_ids=["VID-MOV"],
                sleep_seconds=0,
            )

        self.assertTrue(result["verified"])
        self.assertEqual(result["actual_video_ids"], ["VID-MOV"])
        self.assertEqual(ebay.get_inventory_item.call_count, 2)

    def test_verify_inventory_video_ids_raises_when_ebay_drops_video_id(self):
        ebay = types.SimpleNamespace(
            get_inventory_item=Mock(return_value={"product": {"videoIds": []}})
        )

        with patch.object(listing_wizard.time, "sleep", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "did not retain listing videoIds"):
                listing_wizard._wizard_verify_inventory_video_ids(
                    ebay=ebay,
                    access_token="tok",
                    sku="SKU1",
                    expected_video_ids=["VID-MOV"],
                    max_attempts=2,
                    sleep_seconds=0,
                )

    def test_wizard_inventory_fallback_preserves_video_ids_when_requested(self):
        payload = {
            "product": {
                "title": "Listing",
                "imageUrls": ["https://i.ebayimg.com/front.jpg"],
                "videoIds": ["VID-MOV"],
            },
            "packageWeightAndSize": {"weight": {"value": 1, "unit": "OUNCE"}},
        }
        ebay = types.SimpleNamespace(
            create_or_replace_inventory_item=Mock(
                side_effect=[RuntimeError('{"errorId":25001,"message":"Core Inventory Service internal error"}'), None]
            )
        )

        with patch.object(listing_wizard.time, "sleep", return_value=None):
            fell_back, error = listing_wizard._wizard_create_or_replace_inventory_item_with_fallback(
                ebay=ebay,
                access_token="tok",
                sku="SKU1",
                payload=payload,
                content_language="en-US",
                preserve_video_ids=True,
            )

        self.assertTrue(fell_back)
        self.assertIn("25001", error)
        fallback_payload = ebay.create_or_replace_inventory_item.call_args_list[1].kwargs["payload"]
        self.assertEqual(fallback_payload["product"]["videoIds"], ["VID-MOV"])
        self.assertNotIn("packageWeightAndSize", fallback_payload)

    def test_wizard_inventory_fallback_can_drop_video_ids_when_not_requested(self):
        payload = {
            "product": {
                "title": "Listing",
                "imageUrls": ["https://i.ebayimg.com/front.jpg"],
                "videoIds": ["VID-OLD"],
            },
            "packageWeightAndSize": {"weight": {"value": 1, "unit": "OUNCE"}},
        }
        ebay = types.SimpleNamespace(
            create_or_replace_inventory_item=Mock(
                side_effect=[RuntimeError('{"errorId":25001,"message":"Core Inventory Service internal error"}'), None]
            )
        )

        with patch.object(listing_wizard.time, "sleep", return_value=None):
            fell_back, _error = listing_wizard._wizard_create_or_replace_inventory_item_with_fallback(
                ebay=ebay,
                access_token="tok",
                sku="SKU1",
                payload=payload,
                content_language="en-US",
                preserve_video_ids=False,
            )

        self.assertTrue(fell_back)
        fallback_payload = ebay.create_or_replace_inventory_item.call_args_list[1].kwargs["payload"]
        self.assertNotIn("videoIds", fallback_payload["product"])
        self.assertNotIn("packageWeightAndSize", fallback_payload)

    def test_verify_trading_listing_video_ids_confirms_live_listing_video_id(self):
        ebay = types.SimpleNamespace(
            get_trading_item_video_ids=Mock(
                return_value={"video_ids": ["VID-MOV"], "ack": "Success", "item_id": "123"}
            )
        )

        result = listing_wizard._wizard_verify_trading_listing_video_ids(
            ebay=ebay,
            access_token="tok",
            listing_id="123",
            expected_video_ids=["VID-MOV"],
            marketplace_id="EBAY_US",
        )

        self.assertTrue(result["verified"])
        self.assertEqual(result["actual_video_ids"], ["VID-MOV"])

    def test_verify_trading_listing_video_ids_raises_when_live_listing_missing_video_id(self):
        ebay = types.SimpleNamespace(
            get_trading_item_video_ids=Mock(return_value={"video_ids": [], "ack": "Success", "item_id": "123"})
        )

        with self.assertRaisesRegex(RuntimeError, "Trading GetItem did not return"):
            listing_wizard._wizard_verify_trading_listing_video_ids(
                ebay=ebay,
                access_token="tok",
                listing_id="123",
                expected_video_ids=["VID-MOV"],
                marketplace_id="EBAY_US",
            )

    def test_expected_net_score_computes_variance_bands(self):
        score = listing_wizard._expected_net_score(
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
        self.assertEqual(float(score.get("breakeven_listing_price") or 0.0), 56.82)
        self.assertEqual(float(score.get("breakeven_unit_price") or 0.0), 28.41)
        self.assertEqual(float(score.get("price_cushion") or 0.0), 43.18)
        self.assertEqual(str(score.get("score") or ""), "strong")

    def test_suggested_price_band_derives_missing_values(self):
        price, low, high = listing_wizard._suggested_price_band({"suggested_price": "100"})
        self.assertEqual(price, 100.0)
        self.assertEqual(low, 90.0)
        self.assertEqual(high, 110.0)

        price2, low2, high2 = listing_wizard._suggested_price_band(
            {"suggested_price_low": "80", "suggested_price_high": "100"}
        )
        self.assertEqual(price2, 90.0)
        self.assertEqual(low2, 80.0)
        self.assertEqual(high2, 100.0)

        price3, low3, high3 = listing_wizard._suggested_price_band(
            {"suggested_price_low": "110", "suggested_price_high": "90"}
        )
        self.assertEqual(price3, 100.0)
        self.assertEqual(low3, 90.0)
        self.assertEqual(high3, 110.0)

    def test_sanitize_preview_html_removes_scripts_styles_and_handlers(self):
        raw = (
            "<div onclick='alert(1)'>Hi</div>"
            "<script>alert('x')</script>"
            "<style>body{display:none;}</style>"
            "<a href='javascript:alert(1)'>x</a>"
        )
        sanitized = listing_wizard._sanitize_preview_html(raw)
        self.assertIn("<div>Hi</div>", sanitized)
        self.assertNotIn("<script", sanitized.lower())
        self.assertNotIn("<style", sanitized.lower())
        self.assertNotIn("onclick", sanitized.lower())
        self.assertNotIn("javascript:", sanitized.lower())

    def test_format_listing_description_for_ebay_preserves_plain_text_structure(self):
        raw = (
            "In Case of FIAT Emergency\n\n"
            "Limited Edition Silver Shot Display\n\n"
            "Offered here is a handcrafted display piece.\n\n"
            "What's Included\n"
            "• Handcrafted stained wood display\n"
            "• 1/2 Troy Ounce of .999 Fine Silver Shot\n\n"
            "Thank you for supporting Golden Stackers."
        )
        formatted = listing_wizard._format_listing_description_for_ebay(raw)
        self.assertIn("<h3>In Case of FIAT Emergency</h3>", formatted)
        self.assertIn("<p>Offered here is a handcrafted display piece.</p>", formatted)
        self.assertIn("<ul>", formatted)
        self.assertIn("<li>Handcrafted stained wood display</li>", formatted)
        self.assertIn("<li>1/2 Troy Ounce of .999 Fine Silver Shot</li>", formatted)

    def test_format_listing_description_for_ebay_sanitizes_existing_html(self):
        raw = "<div onclick='bad()'>Nice</div><script>x()</script>"
        formatted = listing_wizard._format_listing_description_for_ebay(raw)
        self.assertIn("<div>Nice</div>", formatted)
        self.assertNotIn("onclick", formatted.lower())
        self.assertNotIn("<script", formatted.lower())

    def test_build_ebay_offer_payload_fixed_price_includes_quantity_and_best_offer(self):
        payload = listing_wizard._wizard_build_ebay_offer_payload(
            sku="SKU-1",
            marketplace_id="EBAY_US",
            format_type="FIXED_PRICE",
            listing_qty=5,
            category_id="16679",
            merchant_location_key="goldenstackers-main",
            listing_description="desc",
            listing_duration="GTC",
            payment_policy_id="pay-1",
            fulfillment_policy_id="ful-1",
            return_policy_id="ret-1",
            currency="USD",
            fixed_price=19.99,
            best_offer_enabled=True,
            best_offer_auto_accept=18.5,
            best_offer_minimum=17.0,
            auction_start_price=0.0,
            auction_reserve_price=0.0,
            auction_buy_now_price=0.0,
            store_category_names=["/Coins/Bullion", "/Copper/Rounds", ""],
        )
        self.assertEqual(payload["format"], "FIXED_PRICE")
        self.assertEqual(payload["availableQuantity"], 5)
        self.assertEqual(payload["storeCategoryNames"], ["/Coins/Bullion", "/Copper/Rounds"])
        self.assertEqual(payload["pricingSummary"]["price"]["value"], "19.99")
        self.assertEqual(
            payload["listingPolicies"]["bestOfferTerms"]["autoAcceptPrice"]["value"],
            "18.5",
        )
        self.assertEqual(
            payload["listingPolicies"]["bestOfferTerms"]["autoDeclinePrice"]["value"],
            "17.0",
        )

    def test_build_ebay_offer_payload_auction_omits_quantity(self):
        payload = listing_wizard._wizard_build_ebay_offer_payload(
            sku="SKU-2",
            marketplace_id="EBAY_US",
            format_type="AUCTION",
            listing_qty=7,
            category_id="16679",
            merchant_location_key="goldenstackers-main",
            listing_description="desc",
            listing_duration="DAYS_7",
            payment_policy_id="pay-1",
            fulfillment_policy_id="ful-1",
            return_policy_id="ret-1",
            currency="USD",
            fixed_price=0.0,
            best_offer_enabled=False,
            best_offer_auto_accept=0.0,
            best_offer_minimum=0.0,
            auction_start_price=9.99,
            auction_reserve_price=19.99,
            auction_buy_now_price=24.99,
        )
        self.assertEqual(payload["format"], "AUCTION")
        self.assertNotIn("availableQuantity", payload)
        self.assertEqual(payload["pricingSummary"]["auctionStartPrice"]["value"], "9.99")
        self.assertEqual(payload["pricingSummary"]["auctionReservePrice"]["value"], "19.99")
        self.assertEqual(payload["pricingSummary"]["price"]["value"], "24.99")

    def test_merge_pending_field_updates_merges_existing_and_new(self):
        merged = listing_wizard._wizard_merge_pending_field_updates(
            {"listing_wizard_title": "A"},
            {"listing_wizard_category_id": "16679"},
        )
        self.assertEqual(merged.get("listing_wizard_title"), "A")
        self.assertEqual(merged.get("listing_wizard_category_id"), "16679")

    def test_merge_pending_field_updates_ignores_blank_keys(self):
        merged = listing_wizard._wizard_merge_pending_field_updates(
            {"listing_wizard_title": "A"},
            {"": "bad", "   ": "bad2", "listing_wizard_price": 10.0},
        )
        self.assertNotIn("", merged)
        self.assertEqual(merged.get("listing_wizard_title"), "A")
        self.assertEqual(merged.get("listing_wizard_price"), 10.0)

    def test_apply_pending_field_updates_applies_allowed_keys_only(self):
        class _FakeSt:
            def __init__(self):
                self.session_state = {
                    "listing_wizard_pending_field_updates": {
                        "listing_wizard_title": "Updated Title",
                        "listing_wizard_category_id": "16679",
                        "bad_key": "ignored",
                    }
                }

        original_st = listing_wizard.st
        try:
            listing_wizard.st = _FakeSt()
            listing_wizard._wizard_apply_pending_field_updates()
            state = listing_wizard.st.session_state
            self.assertEqual(state.get("listing_wizard_title"), "Updated Title")
            self.assertEqual(state.get("listing_wizard_category_id"), "16679")
            self.assertNotIn("bad_key", state)
            self.assertNotIn("listing_wizard_pending_field_updates", state)
        finally:
            listing_wizard.st = original_st

    def test_apply_pending_field_updates_defers_locked_widget_keys(self):
        class _LockedSessionState(dict):
            def __setitem__(self, key, value):
                if key == "listing_wizard_category_id":
                    raise listing_wizard.StreamlitAPIException("locked widget key")
                return super().__setitem__(key, value)

        class _FakeSt:
            def __init__(self):
                self.session_state = _LockedSessionState(
                    {
                        "listing_wizard_pending_field_updates": {
                            "listing_wizard_title": "Updated Title",
                            "listing_wizard_category_id": "16679",
                        }
                    }
                )

        original_st = listing_wizard.st
        try:
            listing_wizard.st = _FakeSt()
            listing_wizard._wizard_apply_pending_field_updates()
            state = listing_wizard.st.session_state
            self.assertEqual(state.get("listing_wizard_title"), "Updated Title")
            self.assertEqual(
                state.get("listing_wizard_pending_field_updates"),
                {"listing_wizard_category_id": "16679"},
            )
            self.assertIn("deferred", str(state.get("listing_wizard_apply_flash") or "").lower())
        finally:
            listing_wizard.st = original_st

    def test_apply_draft_payload_to_session_defers_locked_widget_keys(self):
        class _LockedSessionState(dict):
            def __setitem__(self, key, value):
                if key == "listing_wizard_category_id":
                    raise listing_wizard.StreamlitAPIException("locked widget key")
                return super().__setitem__(key, value)

        class _FakeSt:
            def __init__(self):
                self.session_state = _LockedSessionState()

        payload = {
            "contract": {"type": "listing_draft", "version": 1},
            "state": {
                "listing_wizard_title": "Draft Title",
                "listing_wizard_category_id": "16679",
            },
            "context": {"selected_product_id": 1},
        }

        original_st = listing_wizard.st
        try:
            listing_wizard.st = _FakeSt()
            listing_wizard._wizard_apply_draft_payload_to_session(payload)
            state = listing_wizard.st.session_state
            self.assertEqual(state.get("listing_wizard_title"), "Draft Title")
            self.assertEqual(
                state.get("listing_wizard_pending_field_updates"),
                {"listing_wizard_category_id": "16679"},
            )
            self.assertIn("deferred", str(state.get("listing_wizard_apply_flash") or "").lower())
        finally:
            listing_wizard.st = original_st


if __name__ == "__main__":
    unittest.main()
