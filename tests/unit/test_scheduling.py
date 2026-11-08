from datetime import datetime, timezone

import pytest

from src.scheduler.windows import (
    compute_relative_reminder,
    compute_send_window,
)


def test_scheduled_time_before_window_moves_to_eight_local_time() -> None:
    result = compute_send_window(
        datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc),
        "America/Sao_Paulo",
    )

    assert result == datetime.fromisoformat("2026-11-10T08:00:00-03:00")


def test_scheduled_time_after_window_moves_to_next_day() -> None:
    result = compute_send_window(
        datetime.fromisoformat("2026-11-10T23:30:00-03:00"),
        "America/Sao_Paulo",
    )

    assert result == datetime.fromisoformat("2026-11-11T08:00:00-03:00")


def test_scheduled_time_inside_window_is_preserved_in_recipient_timezone() -> None:
    result = compute_send_window(
        datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc),
        "America/Sao_Paulo",
    )

    assert result == datetime.fromisoformat("2026-11-10T12:00:00-03:00")


def test_relative_reminder_subtracts_calendar_days_in_target_timezone() -> None:
    result = compute_relative_reminder(
        datetime.fromisoformat("2026-11-13T14:30:00-03:00"),
        days_before=3,
        timezone_name="America/Sao_Paulo",
    )

    assert result == datetime.fromisoformat("2026-11-10T14:30:00-03:00")


@pytest.mark.parametrize(
    ("due_at", "days_before"),
    [
        (datetime(2026, 11, 13, 14, 30), 3),
        (datetime(2026, 11, 13, 14, 30, tzinfo=timezone.utc), 0),
        (datetime(2026, 11, 13, 14, 30, tzinfo=timezone.utc), -1),
    ],
)
def test_relative_reminder_rejects_naive_due_date_or_invalid_offset(
    due_at: datetime, days_before: int
) -> None:
    with pytest.raises(ValueError):
        compute_relative_reminder(due_at, days_before, "UTC")


def test_send_window_rejects_unknown_timezone() -> None:
    with pytest.raises(ValueError, match="timezone"):
        compute_send_window(
            datetime.now(timezone.utc),
            "Mars/Olympus",
        )
