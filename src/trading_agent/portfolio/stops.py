"""Stop management for open positions on the day's bar. Stops never loosen."""


def breakeven_stop(high: float, stop: float, entry: float, trigger: float) -> float:
    """Move the stop to the entry price once the day's high reaches `trigger`."""
    return max(stop, entry) if high >= trigger else stop


def chandelier_stop(stop: float, entry: float, highest_close: float, atr: float, k: float) -> float:
    """Once the stop is at breakeven, trail it `k` ATR under the highest close since the entry."""
    if stop < entry or not atr > 0:  # also rejects a NaN ATR
        return stop
    return max(stop, highest_close - k * atr)
