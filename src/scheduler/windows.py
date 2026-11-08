from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SEND_WINDOW_START = time(8, 0)
SEND_WINDOW_END = time(20, 0)


def _recipient_zone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(f"Unknown IANA timezone: {timezone_name}.") from error


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone.")


def compute_send_window(
    scheduled_for: datetime,
    timezone_name: str,
    *,
    window_start: time = SEND_WINDOW_START,
    window_end: time = SEND_WINDOW_END,
) -> datetime:
    _require_aware(scheduled_for, "scheduled_for")
    if window_start >= window_end:
        raise ValueError("Send window start must be earlier than its end.")

    zone = _recipient_zone(timezone_name)
    local_time = scheduled_for.astimezone(zone)
    if local_time.timetz().replace(tzinfo=None) < window_start:
        return datetime.combine(local_time.date(), window_start, tzinfo=zone)
    if local_time.timetz().replace(tzinfo=None) >= window_end:
        return datetime.combine(
            local_time.date() + timedelta(days=1),
            window_start,
            tzinfo=zone,
        )
    return local_time


def compute_relative_reminder(
    due_at: datetime,
    days_before: int,
    timezone_name: str,
) -> datetime:
    _require_aware(due_at, "due_at")
    if days_before < 1:
        raise ValueError("days_before must be at least one.")
    zone = _recipient_zone(timezone_name)
    local_due_at = due_at.astimezone(zone)
    reminder_local = local_due_at - timedelta(days=days_before)
    return compute_send_window(reminder_local, timezone_name)


def normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
