from dataclasses import dataclass
from typing import Any, Protocol

from app.config import settings
from app.services.runtime_settings import get_runtime_bool, get_runtime_str


@dataclass(frozen=True)
class PaidCoinSourceConfig:
    enabled: bool
    provider: str
    base_url: str
    api_key: str
    license_acknowledged: bool
    allow_prod: bool


class CoinReferenceSourceAdapter(Protocol):
    provider: str

    def validate(self) -> list[str]:
        ...

    def fetch_records(self, *, query: str, limit: int = 100) -> list[dict[str, Any]]:
        ...


class DisabledCoinSourceAdapter:
    provider = "none"

    def validate(self) -> list[str]:
        return ["Paid source adapter is disabled."]

    def fetch_records(self, *, query: str, limit: int = 100) -> list[dict[str, Any]]:
        return []


class GreysheetAdapter:
    provider = "greysheet"

    def __init__(self, config: PaidCoinSourceConfig):
        self._config = config

    def validate(self) -> list[str]:
        issues: list[str] = []
        if not self._config.license_acknowledged:
            issues.append("License acknowledgment is required for greysheet provider.")
        if settings.app_env == "prod" and not self._config.allow_prod:
            issues.append("Production usage is blocked until `coin_ref_paid_source_allow_prod=true`.")
        if not self._config.base_url:
            issues.append("Base URL is required for greysheet provider.")
        if not self._config.api_key:
            issues.append("API key is required for greysheet provider.")
        return issues

    def fetch_records(self, *, query: str, limit: int = 100) -> list[dict[str, Any]]:
        raise NotImplementedError(
            "Greysheet adapter contract is in place, but direct API fetch is intentionally not implemented. "
            "Use licensed export/manual import workflow until endpoint/legal contract is finalized."
        )


def resolve_paid_coin_source_config(repo: Any) -> PaidCoinSourceConfig:
    provider = get_runtime_str(repo, "coin_ref_paid_source_provider", "none").strip().lower() or "none"
    return PaidCoinSourceConfig(
        enabled=get_runtime_bool(repo, "coin_ref_paid_source_enabled", False),
        provider=provider,
        base_url=get_runtime_str(repo, "coin_ref_paid_source_base_url", "").strip(),
        api_key=get_runtime_str(repo, "coin_ref_paid_source_api_key", "").strip(),
        license_acknowledged=get_runtime_bool(repo, "coin_ref_paid_source_license_ack", False),
        allow_prod=get_runtime_bool(repo, "coin_ref_paid_source_allow_prod", False),
    )


def resolve_paid_coin_source_adapter(repo: Any) -> CoinReferenceSourceAdapter:
    config = resolve_paid_coin_source_config(repo)
    if not config.enabled:
        return DisabledCoinSourceAdapter()
    if config.provider == "greysheet":
        return GreysheetAdapter(config)
    return DisabledCoinSourceAdapter()

