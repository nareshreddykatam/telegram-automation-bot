"""Small timezone-aware time helpers."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_now_iso() -> str:
    return utc_now().isoformat(timespec="seconds")


def iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def local_now(tz: ZoneInfo) -> datetime:
    return datetime.now(tz)


def scheduled_target_date(
    tz: ZoneInfo, publish_hour: int, publish_minute: int, now: datetime | None = None
) -> date:
    """Return the most recent scheduled occurrence date.

    If today's publishing time has passed, today's date is returned. Otherwise
    yesterday's date is returned. This is used for startup catch-up and pause
    resume handling.
    """

    current = (now or datetime.now(tz)).astimezone(tz)
    scheduled_today = datetime.combine(
        current.date(), time(publish_hour, publish_minute), tzinfo=tz
    )
    if current >= scheduled_today:
        return current.date()
    return current.date() - timedelta(days=1)


def format_local(value: datetime | str | None, tz: ZoneInfo) -> str:
    if value is None:
        return "-"
    if isinstance(value, str):
        parsed = parse_iso(value)
        if parsed is None:
            return value
        value = parsed
    return value.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z")
