"""Weekly report (CONCEPT.md 13, IMPLEMENTATION.md 14): KPIs against the baselines, costs,
calibration, your label accuracy and the go-live gate. Pure; amounts in EUR.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from trading_agent.domain.proposals import Label
from trading_agent.domain.trading import Trade
from trading_agent.evaluation.kpi import TradeStats, trade_stats

GATE_DAYS = 91  # three months
GATE_TRADES = 50
BUCKETS = ((0.0, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 1.01))


@dataclass(frozen=True)
class Outcome:
    """A closed agent trade with what the proposal said and what you said."""

    confidence: float | None
    label: Label | None
    pnl_eur: float


@dataclass(frozen=True)
class WeeklyInput:
    week_end: date
    start: date  # first day of the paper phase
    books: Mapping[str, Sequence[Trade]]  # agent_paper, agent_shadow, baseline_sim
    outcomes: Sequence[Outcome]  # closed trades of the agent sample
    llm_eur_week: float
    llm_eur_total: float
    benchmarks: Mapping[str, float]  # name -> return in % since start
    drawdown_pct: Mapping[str, float]  # agent_paper sleeve -> max drawdown in %


@dataclass(frozen=True)
class GateCheck:
    name: str
    passed: bool
    detail: str


def agent_sample(paper: Sequence[Trade], shadow: Sequence[Trade]) -> list[Trade]:
    """Executed trades, plus shadow trades of proposals that weren't executed."""
    executed = {(t.instrument_id, t.signal_date) for t in paper}
    return [*paper, *(t for t in shadow if (t.instrument_id, t.signal_date) not in executed)]


def _closed(trades: Sequence[Trade]) -> list[Trade]:
    return [t for t in trades if t.pnl_net_eur is not None]


def net_expectancy(trades: Sequence[Trade], llm_eur: float) -> float:
    """Average net P&L per closed trade after spreading the LLM costs over them."""
    closed = _closed(trades)
    if not closed:
        return math.nan
    return (sum(t.pnl_net_eur or 0.0 for t in closed) - llm_eur) / len(closed)


def gate(w: WeeklyInput) -> list[GateCheck]:
    sample = agent_sample(w.books.get("agent_paper", ()), w.books.get("agent_shadow", ()))
    closed = len(_closed(sample))
    agent = net_expectancy(sample, w.llm_eur_total)
    baseline = net_expectancy(w.books.get("baseline_sim", ()), 0.0)
    days = (w.week_end - w.start).days
    return [
        GateCheck("3 months of paper trading", days >= GATE_DAYS, f"{days} of {GATE_DAYS} days"),
        GateCheck(
            "50 closed trades (paper + shadow)", closed >= GATE_TRADES, f"{closed} of {GATE_TRADES}"
        ),
        GateCheck(
            "Positive net expectancy after LLM costs",
            agent > 0,
            f"EUR {_f(agent)} per trade",
        ),
        GateCheck(
            "Agent beats baseline_sim",
            agent > baseline,
            f"EUR {_f(agent)} vs {_f(baseline)} per trade",
        ),
    ]


def calibration(outcomes: Sequence[Outcome]) -> list[tuple[str, int, float]]:
    """(confidence bucket, trades, hit rate in %) for buckets with trades."""
    rows: list[tuple[str, int, float]] = []
    for lo, hi in BUCKETS:
        hits = [
            o.pnl_eur > 0 for o in outcomes if o.confidence is not None and lo <= o.confidence < hi
        ]
        if hits:
            label = f"{lo:.1f}-{min(hi, 1.0):.1f}"
            rows.append((label, len(hits), sum(hits) / len(hits) * 100))
    return rows


def label_accuracy(outcomes: Sequence[Outcome]) -> tuple[int, float]:
    """(labelled trades, % where agree won or disagree lost)."""
    labelled = [o for o in outcomes if o.label is not None]
    if not labelled:
        return 0, math.nan
    right = sum((o.label == "agree") == (o.pnl_eur > 0) for o in labelled)
    return len(labelled), right / len(labelled) * 100


def _f(value: float, fmt: str = ".2f") -> str:
    if math.isnan(value):
        return "-"
    return "inf" if math.isinf(value) else format(value, fmt)


def _row(name: str, s: TradeStats) -> str:
    return (
        f"| {name} | {s.trades} | {_f(s.win_rate_pct, '.0f')} % | {_f(s.avg_r)} | "
        f"{_f(s.expectancy_eur)} | {_f(s.profit_factor)} | {_f(s.total_pnl_eur)} | "
        f"{_f(s.fees_eur)} |"
    )


def _in_week(t: Trade, w: WeeklyInput) -> bool:
    return t.exit_date is not None and w.week_end - timedelta(days=7) < t.exit_date <= w.week_end


def render(w: WeeklyInput) -> str:
    header = "| Book | Trades | Win rate | Avg R | Expectancy | PF | Net P&L | Fees |"
    sep = "|---|---|---|---|---|---|---|---|"
    lines = [f"# Weekly report, week ending {w.week_end}", "", "## This week", "", header, sep]
    for name, trades in w.books.items():
        lines.append(_row(name, trade_stats([t for t in trades if _in_week(t, w)])))
    lines += ["", f"## Since {w.start}", "", header, sep]
    for name, trades in w.books.items():
        lines.append(_row(name, trade_stats(trades)))
    open_ = ", ".join(f"{n} {sum(t.exit_date is None for t in ts)}" for n, ts in w.books.items())
    lines += ["", f"Open positions: {open_}"]
    if w.drawdown_pct:
        dd = ", ".join(f"{k} {_f(v, '.1f')} %" for k, v in w.drawdown_pct.items())
        lines.append(f"agent_paper max drawdown: {dd}")
    if w.benchmarks:
        bm = ", ".join(f"{k} {_f(v, '+.1f')} %" for k, v in w.benchmarks.items())
        lines.append(f"Buy and hold since {w.start}: {bm}")
    lines += [
        "",
        "## Costs",
        "",
        f"- LLM: EUR {_f(w.llm_eur_week)} this week, EUR {_f(w.llm_eur_total)} since start",
        "- Fees (closed trades): "
        + ", ".join(f"{n} EUR {_f(trade_stats(ts).fees_eur)}" for n, ts in w.books.items()),
        "",
        "## Calibration (agent sample)",
        "",
        "| Confidence | Trades | Hit rate |",
        "|---|---|---|",
        *[f"| {b} | {n} | {_f(h, '.0f')} % |" for b, n, h in calibration(w.outcomes)],
    ]
    n, accuracy = label_accuracy(w.outcomes)
    lines += [
        "",
        f"Your labels: {n} closed trades labelled, {_f(accuracy, '.0f')} % right "
        "(agree on a winner or disagree on a loser).",
        "",
        "## Go-live gate",
        "",
        *[f"- [{'x' if c.passed else ' '}] {c.name}: {c.detail}" for c in gate(w)],
    ]
    return "\n".join(lines) + "\n"


def summary(w: WeeklyInput) -> str:
    """The Telegram version."""
    lines = [f"📈 Weekly report, week ending {w.week_end}"]
    for name, trades in w.books.items():
        week = trade_stats([t for t in trades if _in_week(t, w)])
        total = trade_stats(trades)
        lines.append(
            f"{name}: week {week.trades} closed, EUR {_f(week.total_pnl_eur, '+.2f')}; "
            f"total {total.trades} closed, avg R {_f(total.avg_r)}"
        )
    lines.append(f"LLM: EUR {_f(w.llm_eur_week)} this week, EUR {_f(w.llm_eur_total)} total")
    checks = gate(w)
    lines.append(f"Go-live gate: {sum(c.passed for c in checks)} of {len(checks)} met")
    lines += [f"  {'✅' if c.passed else '⬜'} {c.name}: {c.detail}" for c in checks]
    lines.append("Full report: dashboard, page Reports")
    return "\n".join(lines)
