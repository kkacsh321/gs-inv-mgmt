from datetime import date, datetime, timezone


def utcnow_naive() -> datetime:
    """Return UTC now as naive datetime for timestamp-without-time-zone columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_today() -> date:
    """Return current UTC calendar date."""
    return datetime.now(timezone.utc).date()
