"""Market facts the risk engine needs for one instrument, from stored daily bars. Pure."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

import pandas as pd

from trading_agent.calc.indicators import atr, bars_to_frame
from trading_agent.domain.market import Bar, Increments, Instrument
from trading_agent.domain.proposals import Proposal
from trading_agent.domain.risk import MarketSnapshot

VALUE_SESSIONS = 20
CORRELATION_SESSIONS = 60
MIN_OVERLAP = 20  # fewer common sessions: no estimate, the holding counts as correlated

CENT_TICKS: Increments = ((Decimal(0), Decimal("0.01")),)  # until IBKR's market rules are known


def tick_for(price: Decimal, increments: Increments) -> Decimal:
    return next((tick for low, tick in reversed(increments) if price >= low), increments[0][1])


def to_tick(price: Decimal, up: bool, increments: Increments = CENT_TICKS) -> Decimal:
    """Entry limits round down, stops and targets up: never more risk than approved."""
    tick = tick_for(price, increments)
    steps = (price / tick).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)
    return (steps * tick).quantize(tick)


def proposal_levels(p: Proposal, increments: Increments = CENT_TICKS) -> dict[str, Decimal]:
    """The proposal's resolved prices on the tick grid; missing ones stay unresolved."""
    out: dict[str, Decimal] = {}
    for ref, price, up in (
        (p.entry_ref, p.entry, False),
        (p.stop_ref, p.stop, True),
        (p.target_ref, p.target, True),
    ):
        if ref is not None and price is not None:
            out[ref] = to_tick(Decimal(str(price)), up, increments)
    return out


def _returns(bars: Sequence[Bar]) -> pd.Series:
    closes = pd.Series(
        [float(b.close) for b in bars], index=pd.DatetimeIndex([pd.Timestamp(b.date) for b in bars])
    )
    return closes.pct_change().iloc[1:].tail(CORRELATION_SESSIONS)


def correlation(a: pd.Series, b: pd.Series) -> float | None:
    """rho of two return series on their common dates; None if too short or one is flat."""
    joined = pd.concat([a, b], axis=1, join="inner").dropna()
    if len(joined) >= MIN_OVERLAP and (joined.std() > 0).all():
        return float(joined.iloc[:, 0].corr(joined.iloc[:, 1]))
    return None


def correlations(bars: Sequence[Bar], held: Mapping[int, Sequence[Bar]]) -> dict[int, float]:
    mine = _returns(bars)
    out: dict[int, float] = {}
    for inst_id, other in held.items():
        if (rho := correlation(mine, _returns(other))) is not None:
            out[inst_id] = rho
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
