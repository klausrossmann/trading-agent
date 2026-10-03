"""Plain-text (Markdown) report for a simulated book or backtest."""

import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import date

import pandas as pd

from trading_agent.domain.trading import Trade
from trading_agent.evaluation.kpi import equity_stats, trade_stats


def _f(value: float, fmt: str = ".2f") -> str:
    if math.isnan(value):
        return "-"
    return "inf" if math.isinf(value) else format(value, fmt)


def _group_rows(trades: Sequence[Trade], key: Callable[[Trade], str]) -> list[str]:
    groups: dict[str, list[Trade]] = {}
    for t in trades:
        groups.setdefault(key(t), []).append(t)
    rows: list[str] = []
    for name in sorted(groups):
        s = trade_stats(groups[name])
        rows.append(
            f"| {name} | {s.trades} | {_f(s.win_rate_pct, '.0f')} % | {_f(s.avg_r)} | "
            f"{_f(s.total_pnl_eur)} | {_f(s.fees_eur)} |"
        )
    return rows


def render(
    *,
    title: str,
    start: date,
    end: date,
    capital: float,
    trades: Sequence[Trade],
    equity: pd.Series,
    invested: pd.Series,
    benchmarks: Mapping[str, pd.Series],
    signals: int,
    rejections: Counter[str],
    notes: Sequence[str] = (),
) -> str:
    closed = [t for t in trades if t.exit_date is not None]
    still_open = [t for t in trades if t.exit_date is None]
    ts = trade_stats(closed)
    lines = [
        f"# {title}",
        "",
        f"Period {start} to {end}, starting capital EUR {capital:,.0f}. Amounts in EUR, net of "
        "modelled fees and slippage.",
        "",
        "## Returns",
        "",
        "| | Total return | CAGR | Max drawdown | Sharpe | Sortino |",
        "|---|---|---|---|---|---|",
    ]
    curves = {"Strategy": equity, **benchmarks}
    for name, curve in curves.items():
        if curve.empty:
            continue
        es = equity_stats(curve)
        lines.append(
            f"| {name} | {_f(es.total_return_pct, '.1f')} % | {_f(es.cagr_pct, '.1f')} % | "
            f"{_f(es.max_drawdown_pct, '.1f')} % | {_f(es.sharpe)} | {_f(es.sortino)} |"
        )
    exposure = float((invested / equity).mean() * 100) if not equity.empty else math.nan
    lines += [
        "",
        "## Trades",
        "",
        f"- Closed trades: {ts.trades} (open at the end: {len(still_open)})",
        f"- Win rate: {_f(ts.win_rate_pct, '.1f')} %",
        f"- Average R: {_f(ts.avg_r)}; expectancy EUR {_f(ts.expectancy_eur)} per trade",
        f"- Profit factor: {_f(ts.profit_factor)}",
        f"- Net P&L: EUR {_f(ts.total_pnl_eur)}; fees EUR {_f(ts.fees_eur)}",
        f"- Average holding period: {_f(ts.avg_holding_sessions, '.1f')} sessions",
        f"- Average exposure: {_f(exposure, '.0f')} % of equity invested",
        "",
        "## By exit reason",
        "",
        "| Exit | Trades | Win rate | Avg R | P&L | Fees |",
        "|---|---|---|---|---|---|",
        *_group_rows(closed, lambda t: str(t.exit_reason)),
        "",
        "## By market",
        "",
        "| Market | Trades | Win rate | Avg R | P&L | Fees |",
        "|---|---|---|---|---|---|",
        *_group_rows(closed, lambda t: t.market),
        "",
        "## By year (exit date)",
        "",
        "| Year | Trades | Win rate | Avg R | P&L | Fees |",
        "|---|---|---|---|---|---|",
        *_group_rows(closed, lambda t: str(t.exit_date.year if t.exit_date else "")),
        "",
        "## Signal funnel",
        "",
        f"- Setups found: {signals}",
        *[f"- Not taken, {reason}: {n}" for reason, n in rejections.most_common()],
        f"- Entries filled: {len(trades)}",
    ]
    if notes:
        lines += ["", "## Notes", "", *[f"- {n}" for n in notes]]
    return "\n".join(lines) + "\n"
