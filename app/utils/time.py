from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app.config import settings


def app_timezone_name() -> str:
    return str(settings.app_default_timezone or "America/Denver").strip() or "America/Denver"


def app_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(app_timezone_name())
    except Exception:
        return ZoneInfo("America/Denver")


def utcnow_naive() -> datetime:
    """Return UTC now as naive datetime for timestamp-without-time-zone columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_today() -> date:
    """Return current UTC calendar date."""
    return datetime.now(timezone.utc).date()


def app_now() -> datetime:
    """Return aware datetime in configured app timezone."""
    return datetime.now(app_timezone())
