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

from trading_agent.domain.market import Bar, BarSeries, Increments, Instrument

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


def _valid(value: float) -> bool:
    return math.isfinite(value) and value > 0


class MarketInfo:
    """Price increments (IBKR market rules) and quotes. Falls back (None) without a connection
    or on any IBKR error, so placement continues with 0.01 ticks and the last close."""

    def __init__(self, ib: IB) -> None:
        self.ib = ib
        self._increments: dict[str, Increments] = {}

    async def increments(self, inst: Instrument) -> Increments | None:
        if inst.yahoo_symbol in self._increments:
            return self._increments[inst.yahoo_symbol]
        if not self.ib.isConnected():
            return None
        try:
            details = await self.ib.reqContractDetailsAsync(contract_for(inst))
            if not details:
                return None
            exchanges = details[0].validExchanges.split(",")
            rules = details[0].marketRuleIds.split(",")
            venue = "SMART" if inst.market == "US" else inst.exchange
            rule = rules[exchanges.index(venue)] if venue in exchanges else rules[0]
            table = await self.ib.reqMarketRuleAsync(int(rule)) or []
        except (OSError, TimeoutError, ConnectionError, ValueError, IndexError) as exc:
            log.warning("ibkr.market_rule_failed", symbol=inst.yahoo_symbol, error=repr(exc))
            return None
        out = tuple(sorted((_dec(p.lowEdge), Decimal(str(p.increment))) for p in table))
        if not out:
            return None
        self._increments[inst.yahoo_symbol] = out
        return out

    async def mid(self, inst: Instrument) -> Decimal | None:
        """(bid + ask) / 2, else the last trade; real-time or delayed, whatever the account gets."""
        if not self.ib.isConnected():
            return None
        try:
            tickers = await self.ib.reqTickersAsync(contract_for(inst))
        except (OSError, TimeoutError, ConnectionError) as exc:
            log.warning("ibkr.quote_failed", symbol=inst.yahoo_symbol, error=repr(exc))
            return None
        if not tickers:
            return None
        t = tickers[0]
        if _valid(t.bid) and _valid(t.ask) and t.ask >= t.bid:
            return _dec((t.bid + t.ask) / 2)
        return _dec(t.last) if _valid(t.last) else None
