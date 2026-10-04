"""IBKR market data through IB Gateway (ib_async): contracts and daily bars (M6, read-only).

Pacing: IBKR allows at most 60 historical data requests in any 10 minutes.
"""

import asyncio
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

import structlog
from ib_async import IB, Contract, Stock

from trading_agent.domain.market import Bar, BarSeries, Instrument

log = structlog.get_logger(__name__)

SOURCE = "ibkr"


class Pacer:
    """At most `limit` requests in any `window_s` seconds."""

    def __init__(
        self,
        limit: int = 60,
        window_s: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.limit, self.window_s = limit, window_s
        self._clock, self._sleep = clock, sleep
        self._sent: deque[float] = deque()

    async def wait(self) -> None:
        now = self._clock()
        while self._sent and self._sent[0] <= now - self.window_s:
            self._sent.popleft()
        if len(self._sent) >= self.limit:
            await self._sleep(self._sent[0] + self.window_s - now)
            self._sent.popleft()
            now = self._clock()
        self._sent.append(now)


def contract_for(inst: Instrument) -> Contract:
    """SMART-routed stock; the stored conid wins once resolved."""
    if inst.conid is not None:
        return Contract(conId=inst.conid, exchange="SMART")
    symbol = inst.symbol.replace(".", " ")  # BRK.B -> "BRK B"
    if inst.market == "EU":
        return Stock(symbol, "SMART", inst.currency, primaryExchange=inst.exchange)
    return Stock(symbol, "SMART", inst.currency)


async def resolve_conids(ib: IB, instruments: Sequence[Instrument]) -> dict[str, int | None]:
    """yahoo_symbol -> conid, None where IBKR finds no unique contract."""
    unresolved = [i.model_copy(update={"conid": None}) for i in instruments]
    found = await ib.qualifyContractsAsync(*(contract_for(i) for i in unresolved))
    out: dict[str, int | None] = {}
    for inst, contract in zip(unresolved, found, strict=True):
        conid = contract.conId if isinstance(contract, Contract) and contract.conId else None
        out[inst.yahoo_symbol] = conid
    return out


def _dec(value: float) -> Decimal:
    return Decimal(str(round(float(value), 4)))


class IbkrPriceProvider:
    """Daily TRADES bars, regular trading hours, split-adjusted like the stored Yahoo bars."""

    source = SOURCE

    def __init__(self, ib: IB, pacer: Pacer | None = None) -> None:
        self.ib = ib
        self.pacer = pacer or Pacer()

    async def daily_bars_async(self, instrument: Instrument, start: date, end: date) -> BarSeries:
        days = (end - start).days + 7  # the request ends now; bars after `end` are dropped
        duration = f"{math.ceil(days / 365)} Y" if days > 365 else f"{days} D"
        await self.pacer.wait()
        bars = await self.ib.reqHistoricalDataAsync(
            contract_for(instrument),
            endDateTime="",
            durationStr=duration,
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )
        if not bars:
            raise LookupError(f"no IBKR bars for {instrument.yahoo_symbol}")
        out = tuple(
            Bar(
                date=b.date if isinstance(b.date, date) else b.date.date(),
                open=_dec(b.open),
                high=_dec(b.high),
                low=_dec(b.low),
                close=_dec(b.close),
                volume=int(b.volume),
            )
            for b in bars
        )
        return BarSeries(
            yahoo_symbol=instrument.yahoo_symbol,
            source=SOURCE,
            fetched_at=datetime.now(UTC),
            bars=tuple(b for b in out if start <= b.date <= end),
        )
