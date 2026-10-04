"""Market facts the risk engine needs for one instrument, from stored daily bars. Pure."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

import pandas as pd

from trading_agent.calc.indicators import atr, bars_to_frame
from trading_agent.domain.market import Bar, Instrument
from trading_agent.domain.risk import MarketSnapshot

VALUE_SESSIONS = 20
CORRELATION_SESSIONS = 60
MIN_OVERLAP = 20  # fewer common sessions: no estimate, the holding counts as correlated
TICK = Decimal("0.01")  # Xetra price bands come with the IBKR market rules (step 5)


def to_tick(price: Decimal, up: bool) -> Decimal:
    """Entry limits round down, stops and targets up: never more risk than approved."""
    return price.quantize(TICK, rounding=ROUND_CEILING if up else ROUND_FLOOR)


def _returns(bars: Sequence[Bar]) -> pd.Series:
    closes = pd.Series(
        [float(b.close) for b in bars], index=pd.DatetimeIndex([pd.Timestamp(b.date) for b in bars])
    )
    return closes.pct_change().iloc[1:].tail(CORRELATION_SESSIONS)


def correlations(bars: Sequence[Bar], held: Mapping[int, Sequence[Bar]]) -> dict[int, float]:
    mine = _returns(bars)
    out: dict[int, float] = {}
    for inst_id, other in held.items():
        joined = pd.concat([mine, _returns(other)], axis=1, join="inner").dropna()
        if len(joined) >= MIN_OVERLAP and (joined.std() > 0).all():  # flat: no estimate
            out[inst_id] = float(joined.iloc[:, 0].corr(joined.iloc[:, 1]))
    return out


def snapshot(
    inst: Instrument,
    bars: Sequence[Bar],
    held: Mapping[int, Sequence[Bar]],
    *,
    session_open: datetime | None,
    session_close: datetime | None,
    sessions_to_earnings: int | None,
    eur_rate: Decimal,
    mid: Decimal | None = None,
) -> MarketSnapshot:
    """`mid` defaults to the last close (no live quote in simulator mode)."""
    if inst.id is None or not bars:
        raise ValueError(f"{inst.yahoo_symbol}: no id or no bars")
    frame = bars_to_frame(list(bars))
    last_atr = float(atr(frame).iloc[-1])
    value = (frame["close"] * frame["volume"]).tail(VALUE_SESSIONS).mean()
    return MarketSnapshot(
        instrument_id=inst.id,
        in_universe=bool(inst.indices),
        session_open=session_open,
        session_close=session_close,
        mid=mid if mid is not None else bars[-1].close,
        atr=Decimal(str(round(last_atr, 4))) if last_atr == last_atr else Decimal(0),
        avg_daily_value=Decimal(str(round(float(value), 0))),
        sessions_to_earnings=sessions_to_earnings,
        eur_rate=eur_rate,
        correlations=correlations(bars, {k: v for k, v in held.items() if k != inst.id}),
    )
