from dataclasses import dataclass


@dataclass(frozen=True)
class ReadinessResult:
    status: str
    blockers: list[str]
    warnings: list[str]
    score: int


def evaluate_ebay_readiness(
    *,
    listing_title: str,
    listing_price: float,
    auction_start_price: float,
    auction_reserve_price: float,
    auction_buy_now_price: float,
    quantity_listed: int,
    listing_status: str,
    format_type: str,
    listing_duration: str,
    media_count: int,
    category_id: str,
    merchant_location_key: str,
    payment_policy_id: str,
    fulfillment_policy_id: str,
    return_policy_id: str,
) -> ReadinessResult:
    blockers: list[str] = []
    warnings: list[str] = []

    title = (listing_title or "").strip()
    status = (listing_status or "").strip().lower()
    publish_format = (format_type or "FIXED_PRICE").strip().upper()
    duration = (listing_duration or "").strip().upper()

    if not title:
        blockers.append("Missing listing title")
    if publish_format == "FIXED_PRICE":
        if float(listing_price or 0) <= 0:
            blockers.append("Buy It Now price must be > 0")
    elif publish_format == "AUCTION":
        if float(auction_start_price or 0) <= 0:
            blockers.append("Auction start price must be > 0")
        if float(auction_reserve_price or 0) > 0 and float(auction_reserve_price or 0) < float(auction_start_price or 0):
            blockers.append("Auction reserve price cannot be lower than start price")
        if duration not in {"DAYS_1", "DAYS_3", "DAYS_5", "DAYS_7", "DAYS_10"}:
            blockers.append("Auction duration must be one of DAYS_1/3/5/7/10")
        if float(auction_buy_now_price or 0) > 0 and float(auction_buy_now_price or 0) < float(auction_start_price or 0):
            blockers.append("Auction Buy It Now price cannot be lower than start price")
        if float(auction_buy_now_price or 0) > 0 and float(auction_reserve_price or 0) > 0:
            if float(auction_buy_now_price or 0) < float(auction_reserve_price or 0):
                warnings.append("Auction Buy It Now is below reserve price; verify intended strategy")
    else:
        blockers.append("Unknown eBay listing format")
    if int(quantity_listed or 0) <= 0:
        blockers.append("Quantity listed must be > 0")
    if int(media_count or 0) <= 0:
        blockers.append("At least 1 image/video required")
    if status == "ended":
        blockers.append("Listing is ended")

    if not (category_id or "").strip():
        blockers.append("Missing eBay category ID")
    if not (merchant_location_key or "").strip():
        blockers.append("Missing merchant location key")
    if not (payment_policy_id or "").strip():
        blockers.append("Missing payment policy ID")
    if not (fulfillment_policy_id or "").strip():
        blockers.append("Missing fulfillment policy ID")
    if not (return_policy_id or "").strip():
        blockers.append("Missing return policy ID")

    if publish_format == "AUCTION" and int(quantity_listed or 0) > 1:
        warnings.append("Auction quantity > 1; verify intended multi-quantity auction behavior")
    if status != "draft":
        warnings.append("Status is not draft; verify before publish")

    score = max(0, 100 - (len(blockers) * 12) - (len(warnings) * 3))
    resolved_status = "ready" if not blockers else "blocked"
    return ReadinessResult(
        status=resolved_status,
        blockers=blockers,
        warnings=warnings,
        score=score,
    )
