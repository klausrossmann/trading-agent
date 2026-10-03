"""Exchange session helpers (exchange_calendars): holidays, half-days and DST handled there."""

from datetime import date, datetime
from functools import cache
from typing import Literal

import exchange_calendars as xcals
import pandas as pd

from trading_agent.domain.market import Market

CALENDAR_BY_MARKET: dict[Market, str] = {"US": "XNYS", "EU": "XETR"}


@cache
def calendar(name: str) -> xcals.ExchangeCalendar:
    return xcals.get_calendar(name)


def last_closed_session(name: str, now: datetime) -> date:
    """The most recent session whose close is at or before `now` (tz-aware)."""
    cal = calendar(name)
    ts = pd.Timestamp(now)
    local_day = pd.Timestamp(ts.tz_convert(cal.tz).date())
    session = cal.date_to_session(local_day, direction="previous")
    if cal.session_close(session) > ts:
        session = cal.previous_session(session)
    return session.date()


def sessions(name: str, start: date, end: date) -> list[date]:
    if start > end:
        return []
    return [s.date() for s in calendar(name).sessions_in_range(start, end)]


def session_offset(name: str, day: date, count: int) -> date:
    """The session `count` sessions away from `day` (or from the session before it)."""
    cal = calendar(name)
    session = cal.date_to_session(pd.Timestamp(day), direction="previous")
    return cal.session_offset(session, count).date()


def session_time(name: str, day: date, anchor: Literal["open", "close"]) -> datetime | None:
    """Open or close of the session on `day` (UTC), or None if the exchange is closed."""
    cal = calendar(name)
    label = pd.Timestamp(day)
    if not cal.is_session(label):
        return None
    ts = cal.session_open(label) if anchor == "open" else cal.session_close(label)
    return ts.to_pydatetime()
