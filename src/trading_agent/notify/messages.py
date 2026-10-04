"""Telegram message texts. Pure functions over plain data, no I/O.

Privacy (IMPLEMENTATION.md 12.4): no account numbers or credentials; amounts in EUR and in percent.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def money(eur: float, budget_eur: float) -> str:
    """'+€12.30 (+1.2 %)': amount and share of the budget."""
    sign = "+" if eur > 0 else "−" if eur < 0 else ""
    pct = abs(eur) / budget_eur * 100 if budget_eur else 0.0
    return f"{sign}€{abs(eur):,.2f} ({sign}{pct:.1f} %)"


def pct(value: float) -> str:
    return f"{value:+.1f} %".replace("-", "−")


def day_label(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d.day} {d:%b}"


def ago(then: datetime | None, now: datetime) -> str:
    if then is None:
        return "never"
    minutes = int((now - then).total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} min ago"
    return f"{minutes // 60} h {minutes % 60} min ago"


# --- briefing ---


@dataclass(frozen=True)
class MarketLine:
    name: str
    last_date: date
    close: float
    change_pct: float
    trend: Mapping[str, str]  # timeframe -> up/down/sideways/unknown


@dataclass(frozen=True)
class EarningsLine:
    symbol: str
    date: date
    timing: str


@dataclass(frozen=True)
class PositionLine:
    symbol: str
    entry_date: date
    r_now: float
    pnl_eur: float


@dataclass(frozen=True)
class SleeveLine:
    label: str  # e.g. "US"
    budget_eur: float
    closed_trades: int
    closed_pnl_eur: float
    open: Sequence[PositionLine] = ()


@dataclass(frozen=True)
class Briefing:
    day: date
    markets: Sequence[MarketLine]
    eur_usd: float | None
    earnings: Sequence[EarningsLine]
    book: Sequence[SleeveLine] | None  # None: baseline book not started yet
    book_start: date
    setups: Sequence[str]
    last_bars: Mapping[str, date | None]
    blocked: Sequence[str] | None = None  # None: no quality check since start
    notes: Sequence[str] = field(default_factory=list[str])


def render_briefing(b: Briefing) -> str:
    lines = [f"📰 Briefing {day_label(b.day)}", "", "Markets (last close):"]
    for m in b.markets:
        trend = ", ".join(f"{k} {v}" for k, v in m.trend.items())
        lines.append(f"  {m.name} {m.close:,.2f} {pct(m.change_pct)} ({trend})")
    if b.eur_usd is not None:
        lines.append(f"  EUR/USD {b.eur_usd:.4f}")

    lines += ["", "Earnings, next 3 sessions:"]
    if b.earnings:
        for e in b.earnings:
            lines.append(f"  {e.symbol} {day_label(e.date)} {e.timing}")
    else:
        lines.append("  none in the universe")

    lines += ["", "Baseline book:"]
    if b.book is None:
        lines.append(f"  starts {b.book_start}")
    else:
        for s in b.book:
            lines.append(
                f"  {s.label}: {len(s.open)} open, {s.closed_trades} closed, "
                f"P&L {money(s.closed_pnl_eur, s.budget_eur)}"
            )
            for p in s.open:
                lines.append(
                    f"    {p.symbol} since {day_label(p.entry_date)}: {p.r_now:+.2f} R, "
                    f"{money(p.pnl_eur, s.budget_eur)}"
                )

    lines += ["", "Baseline setups at the last close:"]
    lines.append(f"  {', '.join(b.setups)}" if b.setups else "  none")

    bars = ", ".join(f"{m} {d:%d %b}" if d else f"{m} -" for m, d in b.last_bars.items())
    quality = (
        "not checked yet"
        if b.blocked is None
        else f"{len(b.blocked)} blocked" + (f" ({', '.join(b.blocked)})" if b.blocked else "")
    )
    lines += ["", f"Data: last bars {bars}; quality {quality}"]
    lines += [f"ℹ️ {n}" for n in b.notes]
    return "\n".join(lines)


# --- status ---


@dataclass(frozen=True)
class StatusSnapshot:
    mode: str
    started_at: datetime
    now: datetime
    heartbeat_at: datetime | None
    heartbeat_ok: bool | None
    last_bars: Mapping[str, date | None]
    blocked: Mapping[str, Sequence[str]]  # market -> symbols, from the last quality check
    next_jobs: Sequence[tuple[str, datetime]]


def render_status(s: StatusSnapshot) -> str:
    uptime = ago(s.started_at, s.now).removesuffix(" ago")
    heartbeat = (
        "not configured"
        if s.heartbeat_ok is None and s.heartbeat_at is None
        else f"{'ok' if s.heartbeat_ok else 'FAILING'}, {ago(s.heartbeat_at, s.now)}"
    )
    bars = ", ".join(f"{m} {d:%d %b}" if d else f"{m} -" for m, d in s.last_bars.items())
    blocked = "; ".join(f"{m}: {', '.join(v) or 'none'}" for m, v in s.blocked.items()) or (
        "not checked since start"
    )
    lines = [
        f"🟢 Agent running ({s.mode}), up {uptime}",
        f"Heartbeat: {heartbeat}",
        f"Data: last bars {bars}",
        f"Blocked symbols: {blocked}",
        "Next jobs:",
        *[f"  {name} {when:%a %d %b %H:%M}" for name, when in s.next_jobs],
        "Broker: not connected (IBKR from M6) · LLM: not in use (M5) · Kill switch: M8",
    ]
    return "\n".join(lines)


# --- alerts ---


def data_alert(
    job: str,
    failed: Mapping[str, str],
    restated: Sequence[str] = (),
    max_items: int = 10,
) -> str | None:
    if not failed and not restated:
        return None
    lines = [f"⚠️ Data: {job}"]
    if failed:
        items = [f"{k} ({v})" for k, v in list(failed.items())[:max_items]]
        more = f" and {len(failed) - max_items} more" if len(failed) > max_items else ""
        lines.append(f"failed for {len(failed)}: {', '.join(items)}{more}")
    if restated:
        lines.append(f"history restated (split?): {', '.join(restated)}")
    return "\n".join(lines)


def quality_alert(market: str, blocked: Mapping[str, Sequence[str]]) -> str | None:
    """`blocked`: symbol -> blocking checks."""
    if not blocked:
        return None
    items = [f"{sym} ({', '.join(sorted(set(checks)))})" for sym, checks in sorted(blocked.items())]
    return f"⚠️ Data quality {market}: excluded from today's scan: {', '.join(items)}"


def job_alert(job_id: str, error: str) -> str:
    return f"⚠️ Job {job_id} failed: {error}"


def missed_alert(job_id: str) -> str:
    return f"⚠️ Job {job_id} missed its run time and was skipped"
