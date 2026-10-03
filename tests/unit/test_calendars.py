from datetime import UTC, date, datetime

import pytest

from trading_agent.data.calendars import (
    last_closed_session,
    session_offset,
    session_time,
    sessions,
)


@pytest.mark.parametrize(
    ("cal", "now", "expected"),
    [
        ("XNYS", datetime(2026, 10, 2, 21, 0, tzinfo=UTC), date(2026, 10, 2)),  # after 16:00 ET
        ("XNYS", datetime(2026, 10, 2, 19, 0, tzinfo=UTC), date(2026, 10, 1)),  # still open
        ("XNYS", datetime(2026, 10, 3, 10, 0, tzinfo=UTC), date(2026, 10, 2)),  # Saturday
        ("XETR", datetime(2026, 10, 2, 16, 0, tzinfo=UTC), date(2026, 10, 2)),  # after 17:30 CEST
        ("XETR", datetime(2026, 10, 2, 15, 0, tzinfo=UTC), date(2026, 10, 1)),
        # Christmas 2026: NYSE half-day on the 24th, Xetra closed on the 24th and 25th
        ("XNYS", datetime(2026, 12, 25, 22, 0, tzinfo=UTC), date(2026, 12, 24)),
        ("XETR", datetime(2026, 12, 25, 22, 0, tzinfo=UTC), date(2026, 12, 23)),
    ],
)
def test_last_closed_session(cal: str, now: datetime, expected: date) -> None:
    assert last_closed_session(cal, now) == expected


def test_session_time_and_offset() -> None:
    assert session_time("XNYS", date(2026, 10, 2), "close") == datetime(
        2026, 10, 2, 20, 0, tzinfo=UTC
    )
    assert session_time("XETR", date(2026, 10, 2), "open") == datetime(
        2026, 10, 2, 7, 0, tzinfo=UTC
    )
    assert session_time("XNYS", date(2026, 10, 3), "close") is None
    assert session_offset("XNYS", date(2026, 10, 5), -1) == date(2026, 10, 2)
    assert session_offset("XNYS", date(2026, 10, 4), -1) == date(2026, 10, 1)  # Sunday -> Friday


def test_sessions_range() -> None:
    assert sessions("XNYS", date(2026, 10, 1), date(2026, 10, 6)) == [
        date(2026, 10, 1),
        date(2026, 10, 2),
        date(2026, 10, 5),
        date(2026, 10, 6),
    ]
    assert sessions("XNYS", date(2026, 10, 6), date(2026, 10, 1)) == []
