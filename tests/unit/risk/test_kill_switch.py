"""Kill-switch transitions (9.3): every path of the state diagram."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trading_agent.domain.risk import KillSwitch, Trip
from trading_agent.risk import kill_switch as k

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 10, 7, 14, 0, tzinfo=UTC)  # Wednesday, 16:00 in Berlin
ACTIVE = KillSwitch()
HALTED = KillSwitch(state="halted", reason="/stop", since=NOW)


def paused(until: datetime | None) -> KillSwitch:
    return KillSwitch(state="paused", reason="x", since=NOW, until=until)


def test_pause_and_resume() -> None:
    p = k.pause(ACTIVE, "/pause", NOW)
    assert p == KillSwitch(state="paused", reason="/pause", since=NOW, until=None)
    assert k.current(p, NOW + timedelta(days=30)) == p  # open-ended until /resume
    assert k.resume(p, NOW) == KillSwitch(state="active", reason="resumed", since=NOW)
    assert k.resume(ACTIVE, NOW) == ACTIVE


def test_a_timed_pause_ends_by_itself() -> None:
    until = NOW + timedelta(hours=1)
    p = paused(until)
    assert k.current(p, until - timedelta(seconds=1)) == p
    assert k.current(p, until) == KillSwitch(state="active", reason="pause ended", since=until)


def test_pauses_are_extended_never_shortened() -> None:
    soon, later = NOW + timedelta(hours=1), NOW + timedelta(days=1)
    assert k.pause(paused(later), "y", NOW, soon) == paused(later)
    assert k.pause(paused(later), "y", NOW, later) == paused(later)
    assert k.pause(paused(None), "y", NOW, later) == paused(None)
    assert k.pause(paused(soon), "y", NOW, later).until == later
    assert k.pause(paused(soon), "y", NOW).until is None


def test_halted_only_leaves_by_reset() -> None:
    assert k.pause(HALTED, "y", NOW) == HALTED
    assert k.resume(HALTED, NOW) == HALTED
    assert k.halt(HALTED, "other", NOW) == HALTED
    assert k.current(HALTED, NOW) == HALTED
    assert k.reset(HALTED, NOW) == KillSwitch(state="active", reason="reset", since=NOW)
    assert k.reset(paused(None), NOW) == paused(None)


def test_halt_from_active_or_paused() -> None:
    assert k.halt(ACTIVE, "drawdown limit", NOW).state == "halted"
    assert k.halt(paused(None), "/stop", NOW) == KillSwitch(
        state="halted", reason="/stop", since=NOW
    )


@pytest.mark.parametrize(
    ("trip", "now", "until", "reason"),
    [
        ("pause_day", NOW, datetime(2026, 10, 8, tzinfo=BERLIN), "daily loss limit"),
        ("pause_week", NOW, datetime(2026, 10, 12, tzinfo=BERLIN), "weekly loss limit"),
        # 23:30 UTC on Sunday is already Monday in Berlin: the next Monday is a week away
        (
            "pause_week",
            datetime(2026, 10, 11, 23, 30, tzinfo=UTC),
            datetime(2026, 10, 19, tzinfo=BERLIN),
            "weekly loss limit",
        ),
    ],
)
def test_loss_limit_trips_pause_until_the_next_day_or_week(
    trip: Trip, now: datetime, until: datetime, reason: str
) -> None:
    ks = k.apply_trip(ACTIVE, trip, now, BERLIN)
    assert (ks.state, ks.reason, ks.until) == ("paused", reason, until)
    # the same trip again changes nothing, so it is announced once
    assert k.apply_trip(ks, trip, now + timedelta(minutes=5), BERLIN) == ks


def test_drawdown_trip_halts() -> None:
    ks = k.apply_trip(paused(None), "halt", NOW, BERLIN)
    assert (ks.state, ks.reason) == ("halted", "drawdown limit")


def test_close_trips_pause_through_the_next_session() -> None:
    friday_close = datetime(2026, 10, 9, 20, 30, tzinfo=UTC)
    monday = date(2026, 10, 12)
    ks = k.apply_close_trip(ACTIVE, "pause_day", friday_close, monday, BERLIN)
    assert (ks.state, ks.reason, ks.until) == (
        "paused",
        "daily loss limit",
        datetime(2026, 10, 13, tzinfo=BERLIN),
    )
    # the weekly pause ends earlier (Monday 00:00), so it doesn't shorten the daily one
    assert k.apply_close_trip(ks, "pause_week", friday_close, monday, BERLIN) == ks
    assert k.apply_close_trip(ks, "halt", friday_close, monday, BERLIN).state == "halted"
