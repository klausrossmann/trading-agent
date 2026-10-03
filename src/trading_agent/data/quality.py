"""Data quality checks on daily bars. Pure functions; the caller supplies the expected sessions."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from trading_agent.domain.market import Bar
from trading_agent.settings import QualityConfig

Check = Literal[
    "no_data", "stale", "missing_session", "unexpected_date", "ohlc_invalid", "zero_volume", "jump"
]


@dataclass(frozen=True)
class QualityIssue:
    yahoo_symbol: str
    check: Check
    date: date | None
    detail: str
    blocking: bool  # recent enough to exclude the symbol from today's scan


def check_bars(
    yahoo_symbol: str, bars: Sequence[Bar], expected: Sequence[date], cfg: QualityConfig
) -> list[QualityIssue]:
    """`expected`: exchange sessions from the first bar up to the last closed session."""
    if not bars:
        return [QualityIssue(yahoo_symbol, "no_data", None, "no bars stored", blocking=True)]

    recent = expected[-cfg.block_window_sessions :]
    window_start = recent[0] if recent else bars[-1].date
    issues: list[QualityIssue] = []

    def add(check: Check, day: date, detail: str, *, blocking: bool | None = None) -> None:
        if blocking is None:
            blocking = day >= window_start
        issues.append(QualityIssue(yahoo_symbol, check, day, detail, blocking))

    have = {b.date for b in bars}
    expected_set = set(expected)
    first, last = bars[0].date, bars[-1].date

    behind = sum(1 for d in expected if d > last)
    if behind > cfg.stale_sessions:
        issues.append(
            QualityIssue(
                yahoo_symbol, "stale", last, f"last bar is {behind} sessions old", blocking=True
            )
        )
    for day in sorted(d for d in expected_set - have if first <= d <= last):
        add("missing_session", day, "no bar for an exchange session")
    for day in sorted(have - expected_set):
        add("unexpected_date", day, "bar on a non-session date")

    jump_limit = Decimal(str(cfg.max_jump_pct))
    previous: Bar | None = None
    for bar in bars:
        if (
            bar.low <= 0
            or bar.low > min(bar.open, bar.close)
            or bar.high < max(bar.open, bar.close)
        ):
            add("ohlc_invalid", bar.date, f"O={bar.open} H={bar.high} L={bar.low} C={bar.close}")
        if bar.volume == 0:
            # Yahoo's Xetra data has sporadic zero-volume days with valid prices; the scan's
            # average-volume filter absorbs those, so only a zero on the latest bar blocks.
            add("zero_volume", bar.date, "volume is 0", blocking=bar.date == last)
        if previous is not None and previous.close > 0:
            move = abs(bar.close / previous.close - 1) * 100
            if move > jump_limit:
                add("jump", bar.date, f"close moved {move:.1f}% ({previous.close} -> {bar.close})")
        previous = bar
    return issues
