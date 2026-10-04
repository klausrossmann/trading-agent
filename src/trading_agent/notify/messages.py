"""Telegram message texts. Pure functions over plain data, no I/O.

Privacy (IMPLEMENTATION.md 12.4): no account numbers or credentials; amounts in EUR and in percent.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from trading_agent.domain.proposals import Proposal
from trading_agent.domain.risk import KillSwitch

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
BERLIN = ZoneInfo("Europe/Berlin")


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
    gateway: str = "disabled (IB_ENABLED=false)"
    kill_switch: str = "unknown"
    interlock: str | None = None  # live mode only


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
        f"IB Gateway: {s.gateway}",
        f"Kill switch: {s.kill_switch}",
        *([f"Live interlock: {s.interlock}"] if s.interlock else []),
        "LLM spend: /budget",
    ]
    return "\n".join(lines)


# --- LLM budget ---


@dataclass(frozen=True)
class BudgetSnapshot:
    month: date
    spent_usd: float
    monthly_usd: float
    mode: str  # normal / lean / stopped
    by_model: Sequence[tuple[str, int, float]]  # model, calls, USD


def render_budget(b: BudgetSnapshot) -> str:
    used = b.spent_usd / b.monthly_usd * 100 if b.monthly_usd else 0.0
    lines = [
        f"💸 LLM budget {b.month:%b %Y}: ${b.spent_usd:.2f} of ${b.monthly_usd:.2f} "
        f"({used:.0f} %), mode {b.mode}",
    ]
    lines += [f"  {model}: {calls} calls, ${usd:.4f}" for model, calls, usd in b.by_model]
    if not b.by_model:
        lines.append("  no calls this month")
    lines.append("Costs are estimated from list prices (config/models.yaml).")
    return "\n".join(lines)


# --- proposals (M7) ---


def _plan(p: Proposal) -> str:
    if p.entry is None or p.stop is None or p.target is None:
        return ""
    rr = f", R:R {p.risk_reward:.1f}" if p.risk_reward else ""
    return f"{p.entry:.2f}, stop {p.stop:.2f}, target {p.target:.2f}{rr}"


def _label_mark(p: Proposal, labels: Mapping[UUID, str]) -> str:
    label = labels.get(p.id)
    return {"agree": " 👍", "disagree": " 👎"}.get(label or "", "")


def render_proposals(
    title: str,
    proposals: Sequence[Proposal],
    labels: Mapping[UUID, str] | None = None,
    skipped: int = 0,
    cost_usd: float | None = None,
) -> str:
    labels = labels or {}
    cost = f" (LLM ${cost_usd:.4f})" if cost_usd is not None else ""
    lines = [f"🧠 {title}{cost}"]
    proposed = sorted((p for p in proposals if p.status == "proposed"), key=lambda p: p.rank or 99)
    for p in proposed:
        critic = f", critic {p.critic_severity}" if p.critic_severity else ", no critique"
        conf = f", conf {p.confidence:.2f}" if p.confidence is not None else ""
        lines.append(
            f"{p.rank or '-'}. {p.yahoo_symbol} {_plan(p)}{conf}{critic}{_label_mark(p, labels)}"
        )
    if not proposed:
        lines.append("No trade proposed.")
    blocked = [p for p in proposals if p.status == "blocked"]
    if blocked:
        items = [f"{p.yahoo_symbol}{_label_mark(p, labels)}" for p in blocked]
        lines.append(f"Blocked by the critic: {', '.join(items)}")
    passed = [p for p in proposals if p.status == "no_trade"]
    if passed:
        items = [f"{p.yahoo_symbol}{_label_mark(p, labels)}" for p in passed]
        lines.append(f"Passed: {', '.join(items)}")
    if skipped:
        lines.append(f"Not sent to the proposer: {skipped} (technical rating, held, failed)")
    if proposals:
        lines.append("/why SYMBOL for details, /review to label them.")
    return "\n".join(lines)


def render_why(p: Proposal, label: tuple[str, str | None] | None = None) -> str:
    status = {"proposed": f"proposed, rank {p.rank}", "blocked": "blocked by the critic"}
    lines = [
        f"🧠 {p.yahoo_symbol}, as of {day_label(p.as_of)}: {status.get(p.status, 'passed')}",
    ]
    if plan := _plan(p):
        lines.append(f"Plan: buy limit {plan} ({p.entry_ref} / {p.stop_ref} / {p.target_ref})")
    if p.confidence is not None:
        lines.append(f"Confidence: {p.confidence:.2f}")
    lines += [f"Thesis: {p.thesis}", f"Invalidation: {p.invalidation}"]
    proposer = p.payload.get("proposer", {})
    if reason := proposer.get("no_trade_reason"):
        lines.append(f"Why not: {reason}")
    critic = p.payload.get("critic", {})
    if p.critic_summary:
        lines.append(f"Critic ({p.critic_severity}): {p.critic_summary}")
        lines += [f"  - {o['severity']}: {o['point']}" for o in critic.get("objections", [])]
    elif "unavailable" in critic:
        lines.append(f"Critic: unavailable ({critic['unavailable']})")
    if pm := p.payload.get("portfolio_manager"):
        lines.append(f"Ranking: {pm['note']}")
    if label:
        reason = f" ({label[1]})" if label[1] else ""
        lines.append(f"Your label: {label[0]}{reason}")
    return "\n".join(lines)


@dataclass(frozen=True)
class ClosedLine:
    symbol: str
    reason: str
    pnl_eur: float
    budget_eur: float


@dataclass(frozen=True)
class BookDay:
    book: str
    entered: list[str]
    closed: list[ClosedLine]
    open: int


@dataclass(frozen=True)
class Digest:
    day: date
    proposals_as_of: date | None
    statuses: Mapping[str, int]
    to_label: int
    books: list[BookDay]
    llm_usd_today: float


def render_digest(d: Digest) -> str:
    lines = [f"🌙 Evening digest {day_label(d.day)}"]
    if d.proposals_as_of is None:
        lines.append("Proposals: none yet")
    else:
        counts = ", ".join(f"{n} {s.replace('_', ' ')}" for s, n in sorted(d.statuses.items()))
        lines.append(f"Proposals ({day_label(d.proposals_as_of)}): {counts or 'none'}")
        if d.to_label:
            lines.append(f"  {d.to_label} to label: /review")
    for b in d.books:
        parts = [f"{b.open} open"]
        if b.entered:
            parts.append(f"entered {', '.join(b.entered)}")
        parts += [
            f"closed {c.symbol} {money(c.pnl_eur, c.budget_eur)} ({c.reason})" for c in b.closed
        ]
        lines.append(f"{b.book}: {'; '.join(parts)}")
    lines.append(f"LLM today: ${d.llm_usd_today:.4f}")
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


def gateway_down_alert(minutes: int) -> str:
    return (
        f"🔌 IB Gateway unreachable for {minutes} min. If it lasts, check "
        "`docker compose logs ib-gateway`; a re-login may be needed (VNC via SSH tunnel)."
    )


def gateway_up_alert(minutes: int) -> str:
    return f"🔌 IB Gateway connected again after {minutes} min"


def reconcile_alert(unknown: Sequence[str], missing: Sequence[str], qty: Sequence[str]) -> str:
    lines = ["⚠️ Reconciliation: broker and book differ"]
    if unknown:
        lines.append(f"At the broker only: {', '.join(unknown)}")
    if missing:
        lines.append(f"In the book only: {', '.join(missing)}")
    if qty:
        lines.append(f"Different size: {', '.join(qty)}")
    return "\n".join(lines)


# --- kill switch and live interlock (M8) ---

RESET_HINT = "Reset on the host with `trading-agent reset`, then send /reset CODE here."
STOP_CONFIRM = (
    "Stop trading? The agent halts: no new entries, protective stops stay. "
    "Only a reset on the host undoes it."
)


def when(t: datetime) -> str:
    local = t.astimezone(BERLIN)
    return f"{WEEKDAYS[local.weekday()]} {local:%d %b %H:%M}"


def kill_switch_line(ks: KillSwitch) -> str:
    if ks.state == "active":
        return "active"
    if ks.state == "paused":
        return f"paused ({ks.reason}) until {when(ks.until) if ks.until else '/resume'}"
    return f"halted ({ks.reason})" + (f" since {when(ks.since)}" if ks.since else "")


def kill_switch_alert(ks: KillSwitch) -> str:
    if ks.state == "active":
        return f"▶️ New entries allowed again ({ks.reason})"
    if ks.state == "paused":
        return f"⏸ New entries {kill_switch_line(ks)}"
    return f"⛔ Halted: {ks.reason}. No new entries; protective stops stay.\n{RESET_HINT}"


def live_confirm_alert() -> str:
    return (
        "🔐 Live mode: no orders until you send /confirm_live CODE. The code is in the host "
        "logs: `docker compose logs agent | grep live.confirm_code`."
    )
