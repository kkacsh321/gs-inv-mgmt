from dataclasses import dataclass


@dataclass(frozen=True)
class ChannelCapability:
    channel: str
    publish_api: str
    orders_sync: str
    tracking_push: str
    policy_management: str
    status: str


class BaseChannelAdapter:
    channel_key: str = ""

    def capability(self) -> ChannelCapability:
        raise NotImplementedError

    def orchestration_status(self, *, listing_status: str, readiness_status: str, external_listing_id: str) -> str:
        status = (listing_status or "").strip().lower()
        readiness = (readiness_status or "").strip().lower()
        has_external_id = bool((external_listing_id or "").strip())
        if status == "ended":
            return "error"
        if status == "active" and has_external_id:
            return "published"
        if readiness == "ready":
            return "ready"
        return "blocked"


class EbayChannelAdapter(BaseChannelAdapter):
    channel_key = "ebay"

    def capability(self) -> ChannelCapability:
        return ChannelCapability(
            channel="eBay",
            publish_api="yes",
            orders_sync="yes",
            tracking_push="yes",
            policy_management="yes",
            status="implemented",
        )


class PlannedChannelAdapter(BaseChannelAdapter):
    def __init__(
        self,
        *,
        channel_key: str,
        channel_label: str,
        publish_api: str,
        orders_sync: str,
        tracking_push: str,
        policy_management: str,
    ) -> None:
        self.channel_key = channel_key
        self._capability = ChannelCapability(
            channel=channel_label,
            publish_api=publish_api,
            orders_sync=orders_sync,
            tracking_push=tracking_push,
            policy_management=policy_management,
            status="roadmap",
        )

    def capability(self) -> ChannelCapability:
        return self._capability


def build_channel_adapters() -> list[BaseChannelAdapter]:
    return [
        EbayChannelAdapter(),
        PlannedChannelAdapter(
            channel_key="shopify",
            channel_label="Shopify",
            publish_api="planned",
            orders_sync="planned",
            tracking_push="planned",
            policy_management="n/a",
        ),
        PlannedChannelAdapter(
            channel_key="whatnot",
            channel_label="Whatnot",
            publish_api="planned",
            orders_sync="planned",
            tracking_push="planned",
            policy_management="n/a",
        ),
        PlannedChannelAdapter(
            channel_key="facebook",
            channel_label="Facebook Marketplace",
            publish_api="planned",
            orders_sync="manual+planned",
            tracking_push="planned",
            policy_management="n/a",
        ),
        PlannedChannelAdapter(
            channel_key="craigslist",
            channel_label="Craigslist",
            publish_api="manual+planned",
            orders_sync="manual",
            tracking_push="manual",
            policy_management="n/a",
        ),
    ]


def adapter_by_channel_key(adapters: list[BaseChannelAdapter]) -> dict[str, BaseChannelAdapter]:
    return {adapter.channel_key.strip().lower(): adapter for adapter in adapters if (adapter.channel_key or "").strip()}


def orchestration_status_for_listing(
    *,
    adapters: list[BaseChannelAdapter],
    channel_key: str,
    listing_status: str,
    readiness_status: str,
    external_listing_id: str,
) -> str:
    adapter_map = adapter_by_channel_key(adapters)
    adapter = adapter_map.get((channel_key or "").strip().lower())
    if adapter is None:
        # Unknown channels are treated as planned/manual until explicit adapter support is added.
        if (readiness_status or "").strip().lower() == "ready":
            return "ready"
        return "blocked"
    return adapter.orchestration_status(
        listing_status=listing_status,
        readiness_status=readiness_status,
        external_listing_id=external_listing_id,
    )


def capability_matrix_rows(adapters: list[BaseChannelAdapter]) -> list[dict]:
    return [
        {
            "channel": capability.channel,
            "publish_api": capability.publish_api,
            "orders_sync": capability.orders_sync,
            "tracking_push": capability.tracking_push,
            "policy_management": capability.policy_management,
            "status": capability.status,
        }
        for capability in [adapter.capability() for adapter in adapters]
    ]
