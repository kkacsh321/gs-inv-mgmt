from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.utils.time import utcnow_naive


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow_naive, onupdate=utcnow_naive
    )


class Product(Base, TimestampMixin):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(String(64), default="bullion")
    inventory_class: Mapped[str] = mapped_column(String(32), default="sellable", index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    metal_type: Mapped[str] = mapped_column(String(64), default="")
    weight_oz: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    package_weight_oz: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    package_length_in: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    package_width_in: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    package_height_in: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    acquisition_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    acquisition_tax_paid: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    acquisition_shipping_paid: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    acquisition_handling_paid: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    product_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    ebay_purchase: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    ebay_purchase_item_id: Mapped[str] = mapped_column(String(128), default="")
    ebay_purchase_url: Mapped[str] = mapped_column(Text, default="")
    current_quantity: Mapped[int] = mapped_column(default=0)
    acquired_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    status: Mapped[str] = mapped_column(
        Enum("active", "archived", name="product_status_enum"), default="active"
    )
    coin_reference_id: Mapped[int | None] = mapped_column(
        ForeignKey("coin_reference_catalog.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    ai_graded: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    ai_grading_description: Mapped[str] = mapped_column(Text, default="")
    ai_description: Mapped[str] = mapped_column(Text, default="")
    ai_comp: Mapped[str] = mapped_column(Text, default="")

    listings: Mapped[list["MarketplaceListing"]] = relationship(back_populates="product")
    media_assets: Mapped[list["MediaAsset"]] = relationship(back_populates="product")
    lot_assignments: Mapped[list["ProductLotAssignment"]] = relationship(back_populates="product")
    inventory_movements: Mapped[list["InventoryMovement"]] = relationship(back_populates="product")
    returns: Mapped[list["ReturnRecord"]] = relationship(back_populates="product")
    coin_reference: Mapped["CoinReferenceCatalog | None"] = relationship()


class InventorySource(Base, TimestampMixin):
    __tablename__ = "inventory_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    source_type: Mapped[str] = mapped_column(String(64), default="vendor", index=True)
    contact_name: Mapped[str] = mapped_column(String(128), default="")
    contact_email: Mapped[str] = mapped_column(String(255), default="")
    contact_phone: Mapped[str] = mapped_column(String(64), default="")
    source_url: Mapped[str] = mapped_column(String(512), default="")
    ebay_store_url: Mapped[str] = mapped_column(String(512), default="")
    account_id: Mapped[str] = mapped_column(String(128), default="")
    payment_method: Mapped[str] = mapped_column(String(64), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    purchase_lots: Mapped[list["PurchaseLot"]] = relationship(back_populates="source")


class MarketplaceListing(Base, TimestampMixin):
    __tablename__ = "marketplace_listings"
    __table_args__ = (
        # Enforce uniqueness only for non-blank external IDs so draft listings without
        # a marketplace-assigned ID can coexist.
        Index(
            "uq_marketplace_listing_nonblank",
            "marketplace",
            "external_listing_id",
            unique=True,
            postgresql_where=text("external_listing_id <> ''"),
            sqlite_where=text("external_listing_id <> ''"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    marketplace: Mapped[str] = mapped_column(String(64), index=True)
    external_listing_id: Mapped[str] = mapped_column(String(128), default="")
    marketplace_url: Mapped[str] = mapped_column(Text, default="")
    marketplace_details: Mapped[str] = mapped_column(Text, default="")
    listing_title: Mapped[str] = mapped_column(String(255))
    listing_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    listing_status: Mapped[str] = mapped_column(
        Enum("draft", "active", "ended", name="listing_status_enum"), default="draft"
    )
    review_status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reviewed_by: Mapped[str] = mapped_column(String(128), default="")
    quantity_listed: Mapped[int] = mapped_column(default=1)
    listed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)

    product: Mapped[Product] = relationship(back_populates="listings")
    sales: Mapped[list["Sale"]] = relationship(back_populates="listing")
    order_items: Mapped[list["OrderItem"]] = relationship(back_populates="listing")
    media_assets: Mapped[list["MediaAsset"]] = relationship(back_populates="listing")


class Order(Base, TimestampMixin):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("marketplace", "external_order_id", name="uq_marketplace_order"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    marketplace: Mapped[str] = mapped_column(String(64), index=True)
    external_order_id: Mapped[str] = mapped_column(String(128), default="")
    order_status: Mapped[str] = mapped_column(String(32), default="paid")
    subtotal_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    fees: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    shipping_cost: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    sold_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    notes: Mapped[str] = mapped_column(Text, default="")

    items: Mapped[list["OrderItem"]] = relationship(back_populates="order")
    sales: Mapped[list["Sale"]] = relationship(back_populates="order")
    returns: Mapped[list["ReturnRecord"]] = relationship(back_populates="order")


class OrderItem(Base, TimestampMixin):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), nullable=True)
    listing_id: Mapped[int | None] = mapped_column(
        ForeignKey("marketplace_listings.id", ondelete="SET NULL"), nullable=True
    )
    quantity: Mapped[int] = mapped_column(default=1)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    line_fees: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    line_shipping: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    notes: Mapped[str] = mapped_column(Text, default="")

    order: Mapped[Order] = relationship(back_populates="items")
    product: Mapped[Product | None] = relationship()
    listing: Mapped[MarketplaceListing | None] = relationship(back_populates="order_items")


class Sale(Base, TimestampMixin):
    __tablename__ = "sales"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), nullable=True, index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), nullable=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("marketplace_listings.id", ondelete="SET NULL"), nullable=True)

    marketplace: Mapped[str] = mapped_column(String(64), index=True)
    external_order_id: Mapped[str] = mapped_column(String(128), default="")
    sold_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    fees: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    shipping_cost: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    shipping_provider: Mapped[str] = mapped_column(String(64), default="")
    shipping_service: Mapped[str] = mapped_column(String(128), default="")
    shipping_package_type: Mapped[str] = mapped_column(String(64), default="")
    tracking_number: Mapped[str] = mapped_column(String(128), default="")
    tracking_status: Mapped[str] = mapped_column(String(64), default="")
    shipping_exception_code: Mapped[str] = mapped_column(String(64), default="")
    shipping_exception_notes: Mapped[str] = mapped_column(Text, default="")
    shipping_exception_action: Mapped[str] = mapped_column(String(64), default="")
    shipping_exception_resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    shipping_exception_resolved_by: Mapped[str] = mapped_column(String(128), default="")
    shipping_label_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    shipping_label_url: Mapped[str] = mapped_column(String(512), default="")
    shipping_label_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    shipping_label_currency: Mapped[str] = mapped_column(String(8), default="USD")
    shipping_label_purchased_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    shipment_exported_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    quantity_sold: Mapped[int] = mapped_column(default=1)
    sold_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)

    product: Mapped[Product | None] = relationship()
    listing: Mapped[MarketplaceListing | None] = relationship(back_populates="sales")
    order: Mapped[Order | None] = relationship(back_populates="sales")
    returns: Mapped[list["ReturnRecord"]] = relationship(back_populates="sale")


class ReturnRecord(Base, TimestampMixin):
    __tablename__ = "returns"

    id: Mapped[int] = mapped_column(primary_key=True)
    sale_id: Mapped[int | None] = mapped_column(ForeignKey("sales.id", ondelete="SET NULL"), nullable=True, index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), nullable=True, index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True)
    marketplace: Mapped[str] = mapped_column(String(64), index=True)
    external_return_id: Mapped[str] = mapped_column(String(128), default="")
    return_status: Mapped[str] = mapped_column(String(32), default="requested")
    reason: Mapped[str] = mapped_column(String(255), default="")
    disposition: Mapped[str] = mapped_column(String(64), default="pending")
    quantity: Mapped[int] = mapped_column(default=1)
    refund_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    refund_fees: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    refund_shipping: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    restocked: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    returned_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")

    sale: Mapped[Sale | None] = relationship(back_populates="returns")
    order: Mapped[Order | None] = relationship(back_populates="returns")
    product: Mapped[Product | None] = relationship(back_populates="returns")


class ShippingPreset(Base, TimestampMixin):
    __tablename__ = "shipping_presets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    shipping_provider: Mapped[str] = mapped_column(String(64), default="")
    shipping_service: Mapped[str] = mapped_column(String(128), default="")
    shipping_package_type: Mapped[str] = mapped_column(String(64), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class DocumentTemplateProfile(Base, TimestampMixin):
    __tablename__ = "document_template_profiles"
    __table_args__ = (
        UniqueConstraint(
            "environment",
            "doc_type",
            "name",
            name="uq_document_template_profile_env_doc_type_name",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    environment: Mapped[str] = mapped_column(String(32), default="local", index=True)
    doc_type: Mapped[str] = mapped_column(String(32), default="all", index=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    template_name: Mapped[str] = mapped_column(String(64), default="Classic")
    accent_color: Mapped[str] = mapped_column(String(16), default="#b45309")
    company_name: Mapped[str] = mapped_column(String(255), default="")
    company_email: Mapped[str] = mapped_column(String(255), default="")
    company_phone: Mapped[str] = mapped_column(String(64), default="")
    company_website: Mapped[str] = mapped_column(String(255), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class DocumentArtifact(Base):
    __tablename__ = "document_artifacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    environment: Mapped[str] = mapped_column(String(32), default="local", index=True)
    source_type: Mapped[str] = mapped_column(String(32), index=True)
    source_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    doc_type: Mapped[str] = mapped_column(String(32), index=True)
    document_number: Mapped[str] = mapped_column(String(128), index=True)
    artifact_kind: Mapped[str] = mapped_column(String(64), default="printable_html", index=True)
    file_name: Mapped[str] = mapped_column(String(255), default="")
    mime_type: Mapped[str] = mapped_column(String(128), default="text/html")
    content_sha256: Mapped[str] = mapped_column(String(128), index=True)
    size_bytes: Mapped[int] = mapped_column(default=0)
    storage_backend: Mapped[str] = mapped_column(String(32), default="db_inline")
    storage_ref: Mapped[str] = mapped_column(String(255), default="")
    content_base64: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(128), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, index=True)


class EbayPublishPreset(Base, TimestampMixin):
    __tablename__ = "ebay_publish_presets"
    __table_args__ = (
        UniqueConstraint(
            "environment",
            "username",
            "name",
            name="uq_ebay_publish_preset_env_user_name",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    environment: Mapped[str] = mapped_column(String(32), default="local", index=True)
    username: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    marketplace_id: Mapped[str] = mapped_column(String(32), default="EBAY_US")
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    content_language: Mapped[str] = mapped_column(String(16), default="en-US")
    merchant_location_key: Mapped[str] = mapped_column(String(64), default="")
    payment_policy_id: Mapped[str] = mapped_column(String(64), default="")
    fulfillment_policy_id: Mapped[str] = mapped_column(String(64), default="")
    return_policy_id: Mapped[str] = mapped_column(String(64), default="")
    category_id: Mapped[str] = mapped_column(String(32), default="")
    format_type: Mapped[str] = mapped_column(String(16), default="FIXED_PRICE")
    listing_duration: Mapped[str] = mapped_column(String(16), default="GTC")
    condition_value: Mapped[str] = mapped_column(String(32), default="NEW")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class EbayListingTemplateProfile(Base, TimestampMixin):
    __tablename__ = "ebay_listing_template_profiles"
    __table_args__ = (
        UniqueConstraint(
            "environment",
            "username",
            "name",
            name="uq_ebay_listing_template_profile_env_user_name",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    environment: Mapped[str] = mapped_column(String(32), default="local", index=True)
    username: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    marketplace: Mapped[str] = mapped_column(String(32), default="ebay")
    listing_title_template: Mapped[str] = mapped_column(String(255), default="")
    marketplace_details_template: Mapped[str] = mapped_column(Text, default="")
    listing_price_default: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    quantity_default: Mapped[int] = mapped_column(default=1)
    listing_status_default: Mapped[str] = mapped_column(String(16), default="draft")
    is_shared: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class SavedFilterProfile(Base, TimestampMixin):
    __tablename__ = "saved_filter_profiles"
    __table_args__ = (
        UniqueConstraint(
            "environment",
            "username",
            "scope",
            "name",
            name="uq_saved_filter_profile_env_user_scope_name",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    environment: Mapped[str] = mapped_column(String(32), default="local", index=True)
    username: Mapped[str] = mapped_column(String(128), index=True)
    scope: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    filter_json: Mapped[str] = mapped_column(Text, default="{}")
    is_shared: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class AIProviderConfig(Base, TimestampMixin):
    __tablename__ = "ai_provider_configs"
    __table_args__ = (
        UniqueConstraint(
            "environment",
            "name",
            name="uq_ai_provider_config_env_name",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    environment: Mapped[str] = mapped_column(String(32), default="local", index=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    provider: Mapped[str] = mapped_column(String(32), default="openai", index=True)
    model: Mapped[str] = mapped_column(String(128), default="gpt-4o-mini")
    multimodal_model: Mapped[str] = mapped_column(String(128), default="")
    base_url: Mapped[str] = mapped_column(String(255), default="https://api.openai.com/v1")
    endpoint_type: Mapped[str] = mapped_column(String(32), default="responses")
    api_key: Mapped[str] = mapped_column(String(512), default="")
    temperature: Mapped[Decimal] = mapped_column(Numeric(4, 2), default=Decimal("0.20"))
    max_output_tokens: Mapped[int] = mapped_column(default=600)
    timeout_seconds: Mapped[int] = mapped_column(default=60)
    notes: Mapped[str] = mapped_column(Text, default="")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class RuntimeSetting(Base, TimestampMixin):
    __tablename__ = "runtime_settings"
    __table_args__ = (
        UniqueConstraint(
            "environment",
            "key",
            name="uq_runtime_setting_env_key",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    environment: Mapped[str] = mapped_column(String(32), default="local", index=True)
    key: Mapped[str] = mapped_column(String(128), index=True)
    value: Mapped[str] = mapped_column(Text, default="")
    value_type: Mapped[str] = mapped_column(String(16), default="str")
    description: Mapped[str] = mapped_column(Text, default="")
    updated_by: Mapped[str] = mapped_column(String(128), default="system")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class IntegrationQueueJob(Base, TimestampMixin):
    __tablename__ = "integration_queue_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    environment: Mapped[str] = mapped_column(String(32), default="local", index=True)
    integration: Mapped[str] = mapped_column(String(64), default="google", index=True)
    action: Mapped[str] = mapped_column(String(128), default="", index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    retry_count: Mapped[int] = mapped_column(default=0)
    max_retries: Mapped[int] = mapped_column(default=5)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, index=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    requested_by: Mapped[str] = mapped_column(String(128), default="system", index=True)
    updated_by: Mapped[str] = mapped_column(String(128), default="system")


class IntegrationAutomationRule(Base, TimestampMixin):
    __tablename__ = "integration_automation_rules"
    __table_args__ = (
        UniqueConstraint(
            "environment",
            "integration",
            "action",
            "name",
            name="uq_integration_automation_rule_env_integration_action_name",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    environment: Mapped[str] = mapped_column(String(32), default="local", index=True)
    integration: Mapped[str] = mapped_column(String(64), default="shipping", index=True)
    action: Mapped[str] = mapped_column(String(128), default="", index=True)
    name: Mapped[str] = mapped_column(String(128), default="", index=True)
    trigger_status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    conditions_json: Mapped[str] = mapped_column(Text, default="{}")
    effect_json: Mapped[str] = mapped_column(Text, default="{}")
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_by: Mapped[str] = mapped_column(String(128), default="system")
    updated_by: Mapped[str] = mapped_column(String(128), default="system")


class IntegrationAutomationApproval(Base, TimestampMixin):
    __tablename__ = "integration_automation_approvals"

    id: Mapped[int] = mapped_column(primary_key=True)
    environment: Mapped[str] = mapped_column(String(32), default="local", index=True)
    rule_id: Mapped[int] = mapped_column(
        ForeignKey("integration_automation_rules.id", ondelete="CASCADE"),
        index=True,
    )
    queue_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("integration_queue_jobs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), default="approved", index=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    approved_by: Mapped[str] = mapped_column(String(128), default="system", index=True)
    approved_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class CoinAIRun(Base):
    __tablename__ = "coin_ai_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    environment: Mapped[str] = mapped_column(String(32), default="local", index=True)
    tool_name: Mapped[str] = mapped_column(String(32), index=True)
    username: Mapped[str] = mapped_column(String(128), index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True)
    listing_id: Mapped[int | None] = mapped_column(
        ForeignKey("marketplace_listings.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    input_hint: Mapped[str] = mapped_column(Text, default="")
    image_filename: Mapped[str] = mapped_column(String(255), default="")
    image_content_type: Mapped[str] = mapped_column(String(128), default="")
    result_markdown: Mapped[str] = mapped_column(Text, default="")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    web_rows_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, index=True)


class CoinReferenceCatalog(Base, TimestampMixin):
    __tablename__ = "coin_reference_catalog"

    id: Mapped[int] = mapped_column(primary_key=True)
    coin_name: Mapped[str] = mapped_column(String(255), index=True)
    country: Mapped[str] = mapped_column(String(64), default="", index=True)
    issuer: Mapped[str] = mapped_column(String(128), default="", index=True)
    denomination: Mapped[str] = mapped_column(String(64), default="", index=True)
    series: Mapped[str] = mapped_column(String(128), default="", index=True)
    year_start: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    year_end: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    mint_mark: Mapped[str] = mapped_column(String(32), default="", index=True)
    composition: Mapped[str] = mapped_column(String(128), default="")
    metal_type: Mapped[str] = mapped_column(String(64), default="", index=True)
    weight_grams: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    asw_oz: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    diameter_mm: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    thickness_mm: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    km_number: Mapped[str] = mapped_column(String(64), default="", index=True)
    pcgs_no: Mapped[str] = mapped_column(String(64), default="", index=True)
    ngc_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    mintage: Mapped[str] = mapped_column(String(64), default="")
    estimated_value_low: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    estimated_value_high: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    price_source: Mapped[str] = mapped_column(String(128), default="")
    source_url: Mapped[str] = mapped_column(String(512), default="")
    tags: Mapped[str] = mapped_column(Text, default="")
    obverse_description: Mapped[str] = mapped_column(Text, default="")
    reverse_description: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class AppUser(Base, TimestampMixin):
    __tablename__ = "app_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(32), default="viewer", index=True)
    display_name: Mapped[str] = mapped_column(String(255), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    password_hash: Mapped[str] = mapped_column(String(512), default="")
    password_salt: Mapped[str] = mapped_column(String(128), default="")
    password_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class RolePermission(Base, TimestampMixin):
    __tablename__ = "role_permissions"
    __table_args__ = (UniqueConstraint("role", "permission", name="uq_role_permission"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    role: Mapped[str] = mapped_column(String(32), index=True)
    permission: Mapped[str] = mapped_column(String(64), index=True)


class SyncRun(Base, TimestampMixin):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    retry_of_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("sync_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    retry_count: Mapped[int] = mapped_column(default=0, index=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    job_name: Mapped[str] = mapped_column(String(128), default="", index=True)
    direction: Mapped[str] = mapped_column(String(32), default="pull", index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    records_processed: Mapped[int] = mapped_column(default=0)
    records_created: Mapped[int] = mapped_column(default=0)
    records_updated: Mapped[int] = mapped_column(default=0)
    records_failed: Mapped[int] = mapped_column(default=0)
    line_items_with_listing_link: Mapped[int] = mapped_column(default=0)
    line_items_unmapped_sku: Mapped[int] = mapped_column(default=0)
    auto_listings_created: Mapped[int] = mapped_column(default=0)
    notes: Mapped[str] = mapped_column(Text, default="")

    events: Mapped[list["SyncEvent"]] = relationship(back_populates="sync_run")
    errors: Mapped[list["SyncError"]] = relationship(back_populates="sync_run")


class SyncEvent(Base):
    __tablename__ = "sync_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    sync_run_id: Mapped[int] = mapped_column(ForeignKey("sync_runs.id", ondelete="CASCADE"), index=True)
    entity_type: Mapped[str] = mapped_column(String(64), default="", index=True)
    entity_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    action: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), default="ok", index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, index=True)

    sync_run: Mapped[SyncRun] = relationship(back_populates="events")


class SyncError(Base):
    __tablename__ = "sync_errors"

    id: Mapped[int] = mapped_column(primary_key=True)
    sync_run_id: Mapped[int] = mapped_column(ForeignKey("sync_runs.id", ondelete="CASCADE"), index=True)
    severity: Mapped[str] = mapped_column(String(32), default="error", index=True)
    code: Mapped[str] = mapped_column(String(64), default="", index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    context_json: Mapped[str] = mapped_column(Text, default="{}")
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    sync_run: Mapped[SyncRun] = relationship(back_populates="errors")


class MediaAsset(Base, TimestampMixin):
    __tablename__ = "media_assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    listing_id: Mapped[int | None] = mapped_column(
        ForeignKey("marketplace_listings.id", ondelete="SET NULL"), nullable=True
    )

    media_type: Mapped[str] = mapped_column(
        Enum("image", "video", "other", name="media_type_enum"), default="image"
    )
    original_filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(128), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(default=0)
    s3_bucket: Mapped[str] = mapped_column(String(255))
    s3_key: Mapped[str] = mapped_column(String(512), unique=True)
    s3_url: Mapped[str] = mapped_column(Text)
    uploaded_by: Mapped[str] = mapped_column(String(128), default="system")

    product: Mapped[Product | None] = relationship(back_populates="media_assets")
    listing: Mapped[MarketplaceListing | None] = relationship(back_populates="media_assets")


class PurchaseLot(Base, TimestampMixin):
    __tablename__ = "purchase_lots"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_sources.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    lot_code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    vendor: Mapped[str] = mapped_column(String(255), default="")
    purchase_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    total_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    total_tax_paid: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    total_shipping_paid: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    total_handling_paid: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    ebay_purchase: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    ebay_purchase_item_id: Mapped[str] = mapped_column(String(128), default="")
    ebay_purchase_url: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")

    product_assignments: Mapped[list["ProductLotAssignment"]] = relationship(back_populates="lot")
    purchase_documents: Mapped[list["PurchaseDocument"]] = relationship(back_populates="lot")
    source: Mapped[InventorySource | None] = relationship(back_populates="purchase_lots")


class ProductLotAssignment(Base, TimestampMixin):
    __tablename__ = "product_lot_assignments"
    __table_args__ = (UniqueConstraint("product_id", "lot_id", name="uq_product_lot_assignment"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    lot_id: Mapped[int] = mapped_column(ForeignKey("purchase_lots.id", ondelete="CASCADE"))
    quantity_acquired: Mapped[int] = mapped_column(default=1)
    unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    unit_tax_paid: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    unit_shipping_paid: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    unit_handling_paid: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    allocated_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    allocated_tax_paid: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    allocated_shipping_paid: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    allocated_handling_paid: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    acquired_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)

    product: Mapped[Product] = relationship(back_populates="lot_assignments")
    lot: Mapped[PurchaseLot] = relationship(back_populates="product_assignments")


class PurchaseDocument(Base, TimestampMixin):
    __tablename__ = "purchase_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    lot_id: Mapped[int | None] = mapped_column(
        ForeignKey("purchase_lots.id", ondelete="SET NULL"), nullable=True, index=True
    )
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_sources.id", ondelete="SET NULL"), nullable=True, index=True
    )
    document_kind: Mapped[str] = mapped_column(String(64), default="incoming_invoice", index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    original_filename: Mapped[str] = mapped_column(String(255), default="")
    content_type: Mapped[str] = mapped_column(String(128), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(default=0)
    content_sha256: Mapped[str] = mapped_column(String(128), default="", index=True)
    s3_bucket: Mapped[str] = mapped_column(String(255), default="")
    s3_key: Mapped[str] = mapped_column(String(512), unique=True)
    s3_url: Mapped[str] = mapped_column(Text, default="")
    ai_extracted_json: Mapped[str] = mapped_column(Text, default="{}")
    ai_summary: Mapped[str] = mapped_column(Text, default="")
    uploaded_by: Mapped[str] = mapped_column(String(128), default="system")

    lot: Mapped[PurchaseLot | None] = relationship(back_populates="purchase_documents")
    product: Mapped[Product | None] = relationship()
    source: Mapped[InventorySource | None] = relationship()


class InventoryMovement(Base):
    __tablename__ = "inventory_movements"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True
    )
    movement_type: Mapped[str] = mapped_column(String(64), index=True)
    quantity_delta: Mapped[int] = mapped_column()
    quantity_before: Mapped[int] = mapped_column()
    quantity_after: Mapped[int] = mapped_column()
    unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    reference_type: Mapped[str] = mapped_column(String(64), default="")
    reference_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, index=True)

    product: Mapped[Product | None] = relationship(back_populates="inventory_movements")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(64), index=True)
    entity_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(32), index=True)
    actor: Mapped[str] = mapped_column(String(128), default="system")
    changes_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, index=True)
