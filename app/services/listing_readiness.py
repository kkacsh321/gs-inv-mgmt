from dataclasses import dataclass

from app.services.ebay_aspects import (
    aspects_have_approved_grader_evidence,
    missing_required_ebay_aspects,
    title_has_numerical_coin_grade,
)
from app.services.ebay import EBAY_MAX_CONDITION_DESCRIPTION_CHARS, EBAY_MAX_INVENTORY_DESCRIPTION_CHARS


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
    aspects: dict[str, list[str]] | None = None,
    category_aspects: list[dict] | None = None,
    condition: str | None = None,
    condition_description: str | None = None,
    listing_description: str | None = None,
    category_conditions: list[dict] | None = None,
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
    if status == "sold":
        blockers.append("Listing is sold")

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
    missing_required_aspects = missing_required_ebay_aspects(category_aspects or [], aspects or {})
    for row in missing_required_aspects:
        name = str((row or {}).get("name") or "").strip()
        if name:
            blockers.append(f"Missing required eBay item specific: {name}")
    if category_conditions:
        selected_condition = str(condition or "").strip().upper()
        allowed_conditions = {
            str((row or {}).get("condition") or "").strip().upper()
            for row in category_conditions
            if str((row or {}).get("condition") or "").strip()
        }
        if selected_condition and selected_condition not in allowed_conditions:
            blockers.append(f"Selected eBay condition is not valid for this category: {selected_condition}")
        elif not selected_condition and allowed_conditions:
            blockers.append("Missing eBay condition for selected category")
    condition_description_text = str(condition_description or "")
    if len(condition_description_text) > EBAY_MAX_CONDITION_DESCRIPTION_CHARS:
        blockers.append(
            "eBay condition description must be "
            f"{EBAY_MAX_CONDITION_DESCRIPTION_CHARS} characters or fewer "
            f"(currently {len(condition_description_text)})"
        )
    listing_description_text = str(listing_description or "")
    if listing_description is not None and not listing_description_text.strip():
        blockers.append("eBay listing description must be between 1 and 4000 characters")
    if len(listing_description_text) > EBAY_MAX_INVENTORY_DESCRIPTION_CHARS:
        blockers.append(
            "eBay listing description must be "
            f"{EBAY_MAX_INVENTORY_DESCRIPTION_CHARS} characters or fewer "
            f"(currently {len(listing_description_text)})"
        )
    if title_has_numerical_coin_grade(title) and not aspects_have_approved_grader_evidence(aspects or {}):
        blockers.append(
            "Numerical coin grade requires an approved grading company item specific "
            "(Certification or Professional Grader)."
        )

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
