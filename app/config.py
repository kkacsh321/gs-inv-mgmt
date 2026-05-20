import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def _default_ebay_callback_base(app_env: str) -> str:
    normalized = (app_env or "").strip().lower()
    if normalized in {"prod", "production"}:
        return "https://inventory.goldenstackers.com"
    if normalized in {"dev", "development", "staging"}:
        return "https://dev-inventory.goldenstackers.com"
    return "http://localhost:8501"


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "GoldenStackers Inventory")
    app_env: str = os.getenv("APP_ENV", "local")
    app_build_version: str = os.getenv("APP_BUILD_VERSION", "unknown")
    app_build_sha: str = os.getenv("APP_BUILD_SHA", "unknown")
    app_user_name: str = os.getenv("APP_USER_NAME", "employee")
    app_user_role: str = os.getenv("APP_USER_ROLE", "admin")
    app_allow_role_override: bool = os.getenv("APP_ALLOW_ROLE_OVERRIDE", "true").lower() == "true"
    app_require_password_auth: bool = os.getenv("APP_REQUIRE_PASSWORD_AUTH", "false").lower() == "true"
    app_auth_signing_key: str = os.getenv("APP_AUTH_SIGNING_KEY", os.getenv("POSTGRES_PASSWORD", "change-me-signing-key"))
    app_auth_remember_days: int = int(os.getenv("APP_AUTH_REMEMBER_DAYS", "14"))
    app_auth_cookie_enabled: bool = os.getenv("APP_AUTH_COOKIE_ENABLED", "false").lower() == "true"
    app_auth_query_token_fallback_enabled: bool = (
        os.getenv("APP_AUTH_QUERY_TOKEN_FALLBACK_ENABLED", "true").lower() == "true"
    )
    app_default_timezone: str = os.getenv("APP_DEFAULT_TIMEZONE", "America/Denver")
    ux_workspace_ebay_enabled: bool = os.getenv("UX_WORKSPACE_EBAY_ENABLED", "false").lower() == "true"
    db_host: str = os.getenv("POSTGRES_HOST", "db")
    db_port: int = int(os.getenv("POSTGRES_PORT", "5432"))
    db_name: str = os.getenv("POSTGRES_DB", "goldenstackers")
    db_user: str = os.getenv("POSTGRES_USER", "goldenstackers")
    db_password: str = os.getenv("POSTGRES_PASSWORD", "goldenstackers")

    ebay_environment: str = os.getenv("EBAY_ENVIRONMENT", "sandbox")
    ebay_allow_sandbox_seller_ops: bool = os.getenv("EBAY_ALLOW_SANDBOX_SELLER_OPS", "false").lower() == "true"
    ebay_client_id: str = os.getenv("EBAY_CLIENT_ID", "")
    ebay_client_secret: str = os.getenv("EBAY_CLIENT_SECRET", "")
    ebay_ru_name: str = os.getenv("EBAY_RU_NAME", "")
    ebay_redirect_uri: str = os.getenv("EBAY_REDIRECT_URI", "")
    ebay_auth_accepted_url: str = os.getenv("EBAY_AUTH_ACCEPTED_URL", "")
    ebay_auth_declined_url: str = os.getenv("EBAY_AUTH_DECLINED_URL", "")
    ebay_user_access_token: str = os.getenv("EBAY_USER_ACCESS_TOKEN", "")
    ebay_user_refresh_token: str = os.getenv("EBAY_USER_REFRESH_TOKEN", "")
    ebay_marketplace_id: str = os.getenv("EBAY_MARKETPLACE_ID", "EBAY_US")
    ebay_currency: str = os.getenv("EBAY_CURRENCY", "USD")
    ebay_content_language: str = os.getenv("EBAY_CONTENT_LANGUAGE", "en-US")
    ebay_merchant_location_key: str = os.getenv("EBAY_MERCHANT_LOCATION_KEY", "goldenstackers-main")
    ebay_payment_policy_id: str = os.getenv("EBAY_PAYMENT_POLICY_ID", "")
    ebay_fulfillment_policy_id: str = os.getenv("EBAY_FULFILLMENT_POLICY_ID", "")
    ebay_return_policy_id: str = os.getenv("EBAY_RETURN_POLICY_ID", "")
    ebay_finding_rate_limit_cooldown_seconds: int = int(
        os.getenv("EBAY_FINDING_RATE_LIMIT_COOLDOWN_SECONDS", "600")
    )
    ebay_finding_rate_limit_severe_cooldown_seconds: int = int(
        os.getenv("EBAY_FINDING_RATE_LIMIT_SEVERE_COOLDOWN_SECONDS", "3600")
    )
    ebay_finding_rate_limit_probe_interval_seconds: int = int(
        os.getenv("EBAY_FINDING_RATE_LIMIT_PROBE_INTERVAL_SECONDS", "120")
    )

    storage_provider: str = os.getenv("STORAGE_PROVIDER", "s3")
    aws_region: str = os.getenv("AWS_REGION", "us-east-1")
    aws_access_key_id: str = os.getenv("AWS_ACCESS_KEY_ID", "")
    aws_secret_access_key: str = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    s3_bucket: str = os.getenv("S3_BUCKET", "")
    s3_endpoint_url: str = os.getenv("S3_ENDPOINT_URL", "")
    s3_public_base_url: str = os.getenv("S3_PUBLIC_BASE_URL", "")

    spot_price_provider: str = os.getenv("SPOT_PRICE_PROVIDER", "yahoo_finance")
    metals_api_key: str = os.getenv("METALS_API_KEY", "")
    metals_api_base_url: str = os.getenv("METALS_API_BASE_URL", "https://metals-api.com/api")

    yahoo_finance_base_url: str = os.getenv(
        "YAHOO_FINANCE_BASE_URL", "https://query1.finance.yahoo.com/v8/finance/chart"
    )
    yahoo_symbol_gold: str = os.getenv("YAHOO_SYMBOL_GOLD", "GC=F")
    yahoo_symbol_silver: str = os.getenv("YAHOO_SYMBOL_SILVER", "SI=F")
    yahoo_symbol_platinum: str = os.getenv("YAHOO_SYMBOL_PLATINUM", "PL=F")

    sync_runner_enabled: bool = os.getenv("SYNC_RUNNER_ENABLED", "true").lower() == "true"
    sync_runner_interval_seconds: int = int(os.getenv("SYNC_RUNNER_INTERVAL_SECONDS", "900"))
    sync_runner_actor: str = os.getenv("SYNC_RUNNER_ACTOR", "sync-worker")
    sync_runner_run_once: bool = os.getenv("SYNC_RUNNER_RUN_ONCE", "false").lower() == "true"

    sync_job_ebay_orders_pull_import_enabled: bool = (
        os.getenv("SYNC_JOB_EBAY_ORDERS_PULL_IMPORT_ENABLED", "true").lower() == "true"
    )
    sync_job_ebay_orders_pull_import_limit: int = int(os.getenv("SYNC_JOB_EBAY_ORDERS_PULL_IMPORT_LIMIT", "50"))
    sync_job_ebay_orders_pull_import_offset: int = int(os.getenv("SYNC_JOB_EBAY_ORDERS_PULL_IMPORT_OFFSET", "0"))
    sync_job_ebay_shipping_tracking_push_enabled: bool = (
        os.getenv("SYNC_JOB_EBAY_SHIPPING_TRACKING_PUSH_ENABLED", "false").lower() == "true"
    )
    sync_job_ebay_connection_health_check_enabled: bool = (
        os.getenv("SYNC_JOB_EBAY_CONNECTION_HEALTH_CHECK_ENABLED", "true").lower() == "true"
    )
    sync_job_ebay_connection_health_check_interval_minutes: int = int(
        os.getenv("SYNC_JOB_EBAY_CONNECTION_HEALTH_CHECK_INTERVAL_MINUTES", "30")
    )
    sync_job_quickbooks_export_enabled: bool = (
        os.getenv("SYNC_JOB_QUICKBOOKS_EXPORT_ENABLED", "false").lower() == "true"
    )
    sync_job_shopify_orders_pull_enabled: bool = (
        os.getenv("SYNC_JOB_SHOPIFY_ORDERS_PULL_ENABLED", "false").lower() == "true"
    )
    sync_job_shopify_orders_pull_shop_domain: str = os.getenv("SYNC_JOB_SHOPIFY_ORDERS_PULL_SHOP_DOMAIN", "")
    sync_job_shopify_orders_pull_access_token: str = os.getenv("SYNC_JOB_SHOPIFY_ORDERS_PULL_ACCESS_TOKEN", "")
    sync_job_shopify_orders_pull_limit: int = int(os.getenv("SYNC_JOB_SHOPIFY_ORDERS_PULL_LIMIT", "50"))
    sync_job_shopify_orders_pull_offset: int = int(os.getenv("SYNC_JOB_SHOPIFY_ORDERS_PULL_OFFSET", "0"))
    comp_web_fallback_enabled: bool = os.getenv("COMP_WEB_FALLBACK_ENABLED", "true").lower() == "true"
    comp_llm_enabled: bool = os.getenv("COMP_LLM_ENABLED", "false").lower() == "true"
    comp_llm_provider: str = os.getenv("COMP_LLM_PROVIDER", "openai")
    comp_llm_base_url: str = os.getenv("COMP_LLM_BASE_URL", "https://api.openai.com/v1")
    comp_llm_endpoint_type: str = os.getenv("COMP_LLM_ENDPOINT_TYPE", "responses")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    comp_llm_model: str = os.getenv("COMP_LLM_MODEL", "gpt-4o-mini")
    comp_llm_temperature: float = float(os.getenv("COMP_LLM_TEMPERATURE", "0.2"))
    comp_llm_max_output_tokens: int = int(os.getenv("COMP_LLM_MAX_OUTPUT_TOKENS", "16000"))
    comp_llm_timeout_seconds: int = int(os.getenv("COMP_LLM_TIMEOUT_SECONDS", "60"))

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.db_user}:{self.db_password}@"
            f"{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def ebay_auth_accepted_url_effective(self) -> str:
        explicit = (self.ebay_auth_accepted_url or "").strip()
        if explicit:
            return explicit
        return f"{_default_ebay_callback_base(self.app_env).rstrip('/')}/eBay_Workspace"

    @property
    def ebay_auth_declined_url_effective(self) -> str:
        explicit = (self.ebay_auth_declined_url or "").strip()
        if explicit:
            return explicit
        return f"{_default_ebay_callback_base(self.app_env).rstrip('/')}/eBay_Workspace"


settings = Settings()
