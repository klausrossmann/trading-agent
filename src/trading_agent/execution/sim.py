"""Daily-bar fill model for simulated books and tests (IMPLEMENTATION.md 10.5).

Conservative where a daily bar is ambiguous: if stop and target are both inside a bar, the
stop wins; on the entry bar only the stop is checked.
"""

from dataclasses import dataclass
from typing import Literal

ExitReason = Literal["stop", "target", "time"]


@dataclass(frozen=True)
class Bar:
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Fill:
    price: float
    reason: ExitReason


def _slip(price: float, slippage_pct: float, side: Literal["buy", "sell"]) -> float:
    factor = slippage_pct / 100
    return price * (1 + factor) if side == "buy" else price * (1 - factor)


def fill_entry(bar: Bar, limit: float, slippage_pct: float) -> float | None:
    """Buy limit: fills at the limit if the low reaches it, at the open if it gaps below."""
    if bar.low > limit:
        return None
    return _slip(min(bar.open, limit), slippage_pct, "buy")


def check_exit(bar: Bar, stop: float, target: float | None, slippage_pct: float) -> Fill | None:
    """Stop (sell stop) and target (sell limit) of a long position on one bar."""
    if bar.open <= stop:
        return Fill(_slip(bar.open, slippage_pct, "sell"), "stop")
    if target is not None and bar.open >= target:
        return Fill(_slip(bar.open, slippage_pct, "sell"), "target")
    if bar.low <= stop:
        return Fill(_slip(stop, slippage_pct, "sell"), "stop")
    if target is not None and bar.high >= target:
        return Fill(_slip(target, slippage_pct, "sell"), "target")
    return None


def time_exit(bar: Bar, slippage_pct: float) -> Fill:
    return Fill(_slip(bar.close, slippage_pct, "sell"), "time")


def open_exit(bar: Bar, slippage_pct: float) -> Fill:
    """A market sell placed before this bar fills at its open."""
    return Fill(_slip(bar.open, slippage_pct, "sell"), "time")


def trailed_stop(bar: Bar, stop: float, entry: float, trigger: float) -> float:
    """Move the stop to breakeven once the high reaches `trigger`. Stops never loosen."""
    return max(stop, entry) if bar.high >= trigger else stop


def chandelier_stop(stop: float, entry: float, highest_close: float, atr: float, k: float) -> float:
    """Once the stop is at breakeven, trail it `k` ATR under the highest close since the entry.
    Stops never loosen."""
    if stop < entry or not atr > 0:  # also rejects a NaN ATR
        return stop
    return max(stop, highest_close - k * atr)
