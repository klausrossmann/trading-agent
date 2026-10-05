"""Kill-switch transitions (IMPLEMENTATION.md 9.3). Pure; the caller stores and announces them.

Every function returns the state unchanged when the transition doesn't apply, so callers can
compare old and new to decide whether anything happened.
"""

from datetime import date, datetime, time, timedelta, tzinfo

from trading_agent.domain.risk import KillSwitch, Trip


def current(ks: KillSwitch, now: datetime) -> KillSwitch:
    """A pause whose end has passed is over."""
    if ks.state == "paused" and ks.until is not None and now >= ks.until:
        return KillSwitch(state="active", reason="pause ended", since=ks.until)
    return ks


def pause(ks: KillSwitch, reason: str, now: datetime, until: datetime | None = None) -> KillSwitch:
    """Halted stays halted; an existing pause is only ever extended, never shortened."""
    ks = current(ks, now)
    if ks.state == "halted":
        return ks
    if ks.state == "paused" and (ks.until is None or (until is not None and until <= ks.until)):
        return ks
    return KillSwitch(state="paused", reason=reason, since=now, until=until)


def resume(ks: KillSwitch, now: datetime) -> KillSwitch:
    ks = current(ks, now)
    if ks.state != "paused":
        return ks
    return KillSwitch(state="active", reason="resumed", since=now)


def halt(ks: KillSwitch, reason: str, now: datetime) -> KillSwitch:
    if ks.state == "halted":
        return ks
    return KillSwitch(state="halted", reason=reason, since=now)


def reset(ks: KillSwitch, now: datetime) -> KillSwitch:
    """The only way out of halted (CLI code on the host, confirmed in Telegram)."""
    if ks.state != "halted":
        return ks
    return KillSwitch(state="active", reason="reset", since=now)


def apply_trip(ks: KillSwitch, trip: Trip, now: datetime, tz: tzinfo) -> KillSwitch:
    """Loss limits: pause until the next local midnight or Monday, or halt on the drawdown."""
    if trip == "halt":
        return halt(ks, "drawdown limit", now)
    local = now.astimezone(tz)
    days = 1 if trip == "pause_day" else 7 - local.weekday()
    until = datetime.combine(local.date() + timedelta(days=days), time(0), tz)
    reason = "daily loss limit" if trip == "pause_day" else "weekly loss limit"
    return pause(ks, reason, now, until)


def apply_close_trip(
    ks: KillSwitch, trip: Trip, now: datetime, next_session: date, tz: tzinfo
) -> KillSwitch:
    """A loss limit measured at a close: the daily pause lasts through `next_session`."""
    if trip != "pause_day":
        return apply_trip(ks, trip, now, tz)
    until = datetime.combine(next_session + timedelta(days=1), time(0), tz)
    return pause(ks, "daily loss limit", now, until)
