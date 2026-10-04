"""Swing points, support/resistance zones, Fibonacci retracements and the level menu."""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

import numpy as np
import pandas as pd

from trading_agent.calc.indicators import atr, bollinger, ema, sma
from trading_agent.domain.levels import LevelKind, LevelRef

FIB_RATIOS = {"fib_382": 0.382, "fib_500": 0.5, "fib_618": 0.618}
ATR_STOPS = {"atr_stop_1_5x": 1.5, "atr_stop_2x": 2.0, "atr_stop_3x": 3.0}
# Targets above the close: with stops 1.5-3 x ATR below, these allow a 2R plan near highs.
ATR_TARGETS = {"atr_target_3x": 3.0, "atr_target_4x": 4.0, "atr_target_6x": 6.0}


@dataclass(frozen=True)
class SwingPoint:
    date: date
    price: float
    kind: Literal["high", "low"]


@dataclass(frozen=True)
class Zone:
    price: float  # mean of the swing points in the zone
    touches: int
    last_touch: date


def swing_points(high: pd.Series, low: pd.Series, lookback: int = 5) -> list[SwingPoint]:
    """Confirmed pivots: the extreme of `lookback` bars on each side (first of equal extremes)."""
    highs = high.to_numpy(dtype=np.float64)
    lows = low.to_numpy(dtype=np.float64)
    points: list[SwingPoint] = []
    for i in range(lookback, len(highs) - lookback):
        day = pd.Timestamp(high.index[i]).date()
        left, right = slice(i - lookback, i), slice(i + 1, i + lookback + 1)
        if highs[i] > highs[left].max() and highs[i] >= highs[right].max():
            points.append(SwingPoint(day, float(highs[i]), "high"))
        if lows[i] < lows[left].min() and lows[i] <= lows[right].min():
            points.append(SwingPoint(day, float(lows[i]), "low"))
    return points


def zones(points: Sequence[SwingPoint], tolerance: float) -> list[Zone]:
    """Cluster swing points (highs and lows alike) whose prices lie within `tolerance`."""
    clusters: list[list[SwingPoint]] = []
    for p in sorted(points, key=lambda p: p.price):
        if clusters and p.price - clusters[-1][0].price <= tolerance:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    return [
        Zone(
            price=sum(p.price for p in c) / len(c),
            touches=len(c),
            last_touch=max(p.date for p in c),
        )
        for c in clusters
    ]


def fib_levels(swing_low: float, swing_high: float, *, up_leg: bool) -> dict[str, float]:
    """Retracements: down from the high after an up leg, else up from the low."""
    span = swing_high - swing_low
    if up_leg:
        return {name: swing_high - r * span for name, r in FIB_RATIOS.items()}
    return {name: swing_low + r * span for name, r in FIB_RATIOS.items()}


def _price(value: float) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def level_menu(
    frame: pd.DataFrame,
    *,
    swing_lookback: int = 5,
    zone_atr_fraction: float = 0.5,
    fib_window: int = 126,
    range_window: int = 252,
    max_zones: int = 3,
) -> list[LevelRef]:
    """Named levels at the last bar of `frame` (open/high/low/close, oldest first).

    Levels that can't be computed yet (not enough history) are left out.
    """
    menu: dict[str, LevelRef] = {}

    def add(name: str, value: float, kind: LevelKind, touches: int | None = None) -> None:
        if math.isfinite(value) and value > 0 and name not in menu:
            menu[name] = LevelRef(name=name, price=_price(value), kind=kind, touches=touches)

    if frame.empty:
        return []
    last = frame.iloc[-1]
    close = float(last["close"])
    add("close", close, "price")
    add("last_high", float(last["high"]), "price")
    add("last_low", float(last["low"]), "price")

    add("ema20", float(ema(frame["close"], 20).iloc[-1]), "ma")
    add("sma50", float(sma(frame["close"], 50).iloc[-1]), "ma")
    add("sma200", float(sma(frame["close"], 200).iloc[-1]), "ma")
    bands = bollinger(frame["close"]).iloc[-1]
    add("bb_lower", float(bands["lower"]), "band")
    add("bb_upper", float(bands["upper"]), "band")

    recent = frame.iloc[-range_window:]
    add("high_52w", float(recent["high"].max()), "range")
    add("low_52w", float(recent["low"].min()), "range")

    atr_now = float(atr(frame).iloc[-1])
    if math.isfinite(atr_now):
        for name, k in ATR_STOPS.items():
            add(name, close - k * atr_now, "atr_stop")
        for name, k in ATR_TARGETS.items():
            add(name, close + k * atr_now, "atr_target")

        points = swing_points(recent["high"], recent["low"], swing_lookback)
        found = zones(points, zone_atr_fraction * atr_now)
        below = sorted((z for z in found if z.price < close), key=lambda z: -z.price)
        above = sorted((z for z in found if z.price > close), key=lambda z: z.price)
        for i, z in enumerate(below[:max_zones], start=1):
            add(f"support_{i}", z.price, "support", z.touches)
        for i, z in enumerate(above[:max_zones], start=1):
            add(f"resistance_{i}", z.price, "resistance", z.touches)
        last_low = next((p for p in reversed(points) if p.kind == "low" and p.price < close), None)
        last_high = next(
            (p for p in reversed(points) if p.kind == "high" and p.price > close), None
        )
        if last_low:
            add("swing_low_last", last_low.price, "swing")
        if last_high:
            add("swing_high_last", last_high.price, "swing")

    leg = frame.iloc[-fib_window:]
    if len(leg) >= 2 * swing_lookback:
        lo_i, hi_i = int(np.argmin(leg["low"].to_numpy())), int(np.argmax(leg["high"].to_numpy()))
        lo, hi = float(leg["low"].iloc[lo_i]), float(leg["high"].iloc[hi_i])
        if hi > lo:
            for name, value in fib_levels(lo, hi, up_leg=lo_i < hi_i).items():
                add(name, value, "fib")

    return list(menu.values())
