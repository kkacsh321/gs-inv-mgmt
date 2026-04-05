import re
from decimal import Decimal

from sqlalchemy import select

from app.db.models import MarketplaceListing, Order, Sale


class ValidationError(ValueError):
    pass


class ValidationService:
    TRACKING_RE = re.compile(r"^[A-Za-z0-9\-]{6,64}$")
    TRACKING_REQUIRED_STATUSES = {"out_for_delivery", "delivered"}

    @staticmethod
    def require_non_empty(field_name: str, value: str) -> None:
        if not (value or "").strip():
            raise ValidationError(f"{field_name} is required.")

    @staticmethod
    def require_positive_int(field_name: str, value: int, min_value: int = 1) -> None:
        if int(value) < int(min_value):
            raise ValidationError(f"{field_name} must be at least {min_value}.")

    @staticmethod
    def require_non_negative_decimal(field_name: str, value: Decimal | None) -> None:
        if value is None:
            return
        if Decimal(str(value)) < Decimal("0"):
            raise ValidationError(f"{field_name} cannot be negative.")

    @classmethod
    def validate_tracking_number(cls, tracking_number: str) -> None:
        if not tracking_number:
            return
        if not cls.TRACKING_RE.match(tracking_number.strip()):
            raise ValidationError(
                "Tracking number format is invalid. Use 6-64 alphanumeric characters and dashes."
            )

    @classmethod
    def validate_sale_tracking_requirements(cls, tracking_status: str, tracking_number: str) -> None:
        status = (tracking_status or "").strip()
        tracking = (tracking_number or "").strip()
        if status in cls.TRACKING_REQUIRED_STATUSES and not tracking:
            raise ValidationError(f"Tracking number is required when tracking status is `{status}`.")
        cls.validate_tracking_number(tracking)

    @staticmethod
    def validate_shipping_dates(
        tracking_status: str,
        shipped_at,
        delivered_at,
    ) -> None:
        status = (tracking_status or "").strip()
        if status == "delivered" and delivered_at is None:
            raise ValidationError("Delivered date is required when tracking status is `delivered`.")
        if delivered_at is not None and shipped_at is not None and delivered_at < shipped_at:
            raise ValidationError("Delivered date cannot be earlier than shipped date.")

    @staticmethod
    def ensure_unique_marketplace_listing(
        db_session,
        marketplace: str,
        external_listing_id: str,
        exclude_listing_id: int | None = None,
    ) -> None:
        external = (external_listing_id or "").strip()
        if not external:
            return
        query = select(MarketplaceListing).where(
            MarketplaceListing.marketplace == marketplace.strip(),
            MarketplaceListing.external_listing_id == external,
        )
        if exclude_listing_id is not None:
            query = query.where(MarketplaceListing.id != exclude_listing_id)
        if db_session.scalar(query) is not None:
            raise ValidationError(
                f"Duplicate listing detected for marketplace `{marketplace}` and external ID `{external}`."
            )

    @staticmethod
    def ensure_unique_marketplace_order(
        db_session,
        marketplace: str,
        external_order_id: str,
        exclude_order_id: int | None = None,
    ) -> None:
        external = (external_order_id or "").strip()
        if not external:
            return
        query = select(Order).where(
            Order.marketplace == marketplace.strip(),
            Order.external_order_id == external,
        )
        if exclude_order_id is not None:
            query = query.where(Order.id != exclude_order_id)
        if db_session.scalar(query) is not None:
            raise ValidationError(
                f"Duplicate order detected for marketplace `{marketplace}` and external order ID `{external}`."
            )

    @staticmethod
    def ensure_tracking_number_not_reused(
        db_session,
        tracking_number: str,
        external_order_id: str,
        exclude_sale_id: int | None = None,
    ) -> None:
        tracking = (tracking_number or "").strip()
        if not tracking:
            return
        query = select(Sale).where(Sale.tracking_number == tracking)
        if exclude_sale_id is not None:
            query = query.where(Sale.id != exclude_sale_id)
        existing = db_session.scalar(query)
        if existing is None:
            return
        existing_order = (existing.external_order_id or "").strip()
        current_order = (external_order_id or "").strip()
        if existing_order and current_order and existing_order == current_order:
            return
        raise ValidationError(
            f"Tracking number `{tracking}` is already used on sale #{existing.id}."
        )

    @staticmethod
    def validate_listing_workflow(
        *,
        listing_title: str,
        listing_price,
        quantity_listed: int,
        listing_status: str,
        media_count: int = 0,
        external_listing_id: str = "",
        marketplace_url: str = "",
    ) -> None:
        ValidationService.require_non_empty("Listing title", listing_title)
        ValidationService.require_positive_int("Quantity listed", quantity_listed)

        price = Decimal(str(listing_price or 0))
        if price <= Decimal("0"):
            raise ValidationError("Listing price must be greater than 0.")

        status = (listing_status or "").strip().lower()
        if status == "active":
            if int(media_count) < 1:
                raise ValidationError(
                    "At least one image/video is required before setting a listing to `active`."
                )
            if not (external_listing_id or "").strip() and not (marketplace_url or "").strip():
                raise ValidationError(
                    "External Listing ID or Marketplace URL is required before setting a listing to `active`."
                )
