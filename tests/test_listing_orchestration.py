import unittest

from app.services.listing_orchestration import (
    BaseChannelAdapter,
    EbayChannelAdapter,
    PlannedChannelAdapter,
    adapter_by_channel_key,
    build_channel_adapters,
    capability_matrix_rows,
    orchestration_status_for_listing,
)


class ListingOrchestrationTests(unittest.TestCase):
    def test_base_adapter_status_logic(self) -> None:
        adapter = PlannedChannelAdapter(
            channel_key="x",
            channel_label="X",
            publish_api="planned",
            orders_sync="planned",
            tracking_push="planned",
            policy_management="n/a",
        )
        self.assertEqual(
            adapter.orchestration_status(listing_status="ended", readiness_status="ready", external_listing_id="123"),
            "error",
        )
        self.assertEqual(
            adapter.orchestration_status(listing_status="sold", readiness_status="ready", external_listing_id="123"),
            "completed",
        )
        self.assertEqual(
            adapter.orchestration_status(listing_status="active", readiness_status="blocked", external_listing_id="123"),
            "published",
        )
        self.assertEqual(
            adapter.orchestration_status(listing_status="draft", readiness_status="ready", external_listing_id=""),
            "ready",
        )
        self.assertEqual(
            adapter.orchestration_status(listing_status="draft", readiness_status="blocked", external_listing_id=""),
            "blocked",
        )

    def test_build_adapters_and_capability_rows(self) -> None:
        adapters = build_channel_adapters()
        self.assertGreaterEqual(len(adapters), 5)
        rows = capability_matrix_rows(adapters)
        channels = {row["channel"] for row in rows}
        self.assertIn("eBay", channels)
        self.assertIn("Shopify", channels)

    def test_adapter_map_skips_blank_keys(self) -> None:
        class BlankAdapter(BaseChannelAdapter):
            channel_key = " "

            def capability(self):  # type: ignore[override]
                return EbayChannelAdapter().capability()

        adapters = [EbayChannelAdapter(), BlankAdapter()]
        mapped = adapter_by_channel_key(adapters)
        self.assertIn("ebay", mapped)
        self.assertNotIn("", mapped)

    def test_orchestration_status_for_listing_unknown_channel(self) -> None:
        adapters = build_channel_adapters()
        ready = orchestration_status_for_listing(
            adapters=adapters,
            channel_key="unknown",
            listing_status="draft",
            readiness_status="ready",
            external_listing_id="",
        )
        blocked = orchestration_status_for_listing(
            adapters=adapters,
            channel_key="unknown",
            listing_status="draft",
            readiness_status="blocked",
            external_listing_id="",
        )
        self.assertEqual(ready, "ready")
        self.assertEqual(blocked, "blocked")

    def test_orchestration_status_for_listing_known_channel(self) -> None:
        adapters = build_channel_adapters()
        status = orchestration_status_for_listing(
            adapters=adapters,
            channel_key="EBAY",
            listing_status="active",
            readiness_status="blocked",
            external_listing_id="abc",
        )
        self.assertEqual(status, "published")


if __name__ == "__main__":
    unittest.main()
