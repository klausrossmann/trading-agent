"""IB Gateway link: connection, contract ids, reconciliation, the check.

Read-only unless IB_ORDERS_ENABLED (M8 step 5)."""

import asyncio
import contextlib
import math
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import structlog
from ib_async import IB, Ticker

from trading_agent.data import ibkr as ibkr_data
from trading_agent.data.ingest import Sessions
from trading_agent.db import market as market_repo
from trading_agent.domain.market import Instrument
from trading_agent.domain.orders import OrderSpec, order_ref
from trading_agent.domain.risk import Mode
from trading_agent.execution.ibkr import IbkrBroker
from trading_agent.execution.reconcile import ReconcileReport, reconcile
from trading_agent.notify import messages
from trading_agent.notify.telegram import Notifier
from trading_agent.portfolio.snapshot import CENT_TICKS, to_tick
from trading_agent.settings import Settings

log = structlog.get_logger(__name__)

ALERT_AFTER_S = 600  # IMPLEMENTATION.md 12.3: disconnected for more than 10 minutes
CHECK_EVERY_S = 30
MAX_BACKOFF_S = 300
DELAYED = 3  # IBKR market data type: real-time where subscribed, else 15-20 min delayed


class BrokerLink:
    """Keeps one read-only ib_async connection; alerts on long outages, runs `on_connect`."""

    def __init__(
        self,
        settings: Settings,
        notifier: Notifier,
        ib: IB | None = None,
        on_connect: Callable[["BrokerLink"], Awaitable[None]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.notifier = notifier
        self.ib = ib or IB()
        self.broker = IbkrBroker(self.ib)
        self.prices = ibkr_data.IbkrPriceProvider(self.ib)
        self.market_info = ibkr_data.MarketInfo(self.ib)
        self.on_connect = on_connect
        self._clock = clock
        self.down_since: float | None = None
        self.alerted = False

    @property
    def connected(self) -> bool:
        return self.ib.isConnected()

    def status(self) -> str:
        if self.connected:
            return f"connected ({self.settings.ib_host}:{self.settings.ib_port})"
        if self.down_since is None:
            return "connecting"
        return f"unreachable for {int((self._clock() - self.down_since) // 60)} min"

    def mode(self) -> Mode | None:
        """Paper or live from the account type (paper ids start with D); None if unknown."""
        accounts = self.ib.managedAccounts() if self.connected else []
        if not accounts:
            return None
        return "paper" if all(a.startswith("D") for a in accounts) else "live"

    async def connect(self) -> bool:
        s = self.settings
        try:
            await self.ib.connectAsync(
                s.ib_host,
                s.ib_port,
                clientId=s.ib_client_id,
                readonly=not s.ib_orders_enabled,
                timeout=15,
            )
        except (OSError, TimeoutError, ConnectionError) as exc:
            log.warning("ibkr.connect_failed", error=type(exc).__name__)
            return False
        self.ib.reqMarketDataType(DELAYED)
        return True

    async def step(self) -> float:
        """One supervision step; returns the seconds to wait before the next."""
        if self.connected:
            return CHECK_EVERY_S
        now = self._clock()
        if self.down_since is None:
            self.down_since = now
        if await self.connect():
            down_min = int((now - self.down_since) // 60)
            log.info("ibkr.connected", after_min=down_min)
            if self.alerted:
                await self.notifier.send(messages.gateway_up_alert(down_min))
            self.down_since, self.alerted = None, False
            if self.on_connect is not None:
                try:
                    await self.on_connect(self)
                except Exception:
                    log.exception("ibkr.on_connect_failed")
            return CHECK_EVERY_S
        down_s = now - self.down_since
        if not self.alerted and down_s >= ALERT_AFTER_S:
            await self.notifier.send(messages.gateway_down_alert(int(down_s // 60)))
            self.alerted = True
        return min(CHECK_EVERY_S * 2 ** min(int(down_s // CHECK_EVERY_S), 4), MAX_BACKOFF_S)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            wait = await self.step()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), wait)
        self.ib.disconnect()


async def sync_conids(link: BrokerLink, sessions: Sessions) -> list[str]:
    """Resolve and store missing contract ids; returns the symbols IBKR couldn't resolve."""
    async with sessions() as s:
        todo = {k: i for k, i in (await market_repo.active_instruments(s)).items() if not i.conid}
    if not todo:
        return []
    found = await ibkr_data.resolve_conids(link.ib, list(todo.values()))
    by_symbol = {i.yahoo_symbol: k for k, i in todo.items()}
    resolved = {by_symbol[sym]: c for sym, c in found.items() if c is not None}
    async with sessions.begin() as s:
        await market_repo.set_conids(s, resolved)
    missing = sorted(sym for sym, c in found.items() if c is None)
    log.info("ibkr.conids", resolved=len(resolved), unresolved=missing)
    return missing


Expected = Callable[[], Awaitable[dict[int, tuple[str, float]]]]


async def reconcile_positions(
    link: BrokerLink | None,
    notifier: Notifier,
    expected: Expected | None = None,
    halt: Callable[[str], Awaitable[object]] | None = None,
) -> ReconcileReport | None:
    """Broker positions vs. the agent book (conid -> symbol, quantity); any difference halts
    (10.3). Without `expected` (orders still simulated) the account must hold nothing."""
    if link is None or not link.connected:
        log.info("reconcile.skipped", reason="gateway not connected")
        return None
    report = reconcile(await link.broker.positions(), await expected() if expected else {})
    log.info(
        "reconcile.done",
        clean=report.clean,
        unknown=len(report.unknown),
        missing=len(report.missing),
        quantity=len(report.quantity),
    )
    if not report.clean:
        await notifier.send(
            messages.reconcile_alert(
                [f"{p.symbol} {p.quantity:g}" for p in report.unknown],
                [f"{m.symbol} {m.expected:g}" for m in report.missing],
                [f"{m.symbol} {m.expected:g} vs {m.actual:g}" for m in report.quantity],
            )
        )
        if halt is not None:
            await halt("reconciliation mismatch")
    return report


async def on_connect(
    link: BrokerLink,
    sessions: Sessions,
    notifier: Notifier,
    expected: Expected | None = None,
    halt: Callable[[str], Awaitable[object]] | None = None,
) -> None:
    await sync_conids(link, sessions)
    await reconcile_positions(link, notifier, expected, halt)


# --- one-off check for the runbook (IMPLEMENTATION.md 15.5) ---

DATA_TYPES = {1: "real-time", 2: "frozen", 3: "delayed", 4: "delayed frozen"}


async def _instrument(sessions: Sessions, symbol: str) -> tuple[int, Instrument] | None:
    async with sessions() as s:
        inst = await market_repo.instrument_by_symbol(s, symbol)
    return (inst.id, inst) if inst is not None and inst.id is not None else None


async def _quote_type(ib: IB, inst: Instrument) -> str:
    contract = ibkr_data.contract_for(inst)
    ticker: Ticker = ib.reqMktData(contract)
    await asyncio.sleep(5)
    ib.cancelMktData(contract)
    prices = (ticker.last, ticker.bid, ticker.ask)
    if not any(math.isfinite(p) and p > 0 for p in prices):
        return f"{inst.yahoo_symbol}: no data (the last close is used)"
    kind = DATA_TYPES.get(ticker.marketDataType, str(ticker.marketDataType))
    return f"{inst.yahoo_symbol}: {kind}, last {ticker.last}, bid {ticker.bid}, ask {ticker.ask}"


async def _compare_bars(
    link: BrokerLink, sessions: Sessions, inst_id: int, inst: Instrument
) -> str:
    today = datetime.now(UTC).date()
    async with sessions() as s:
        stored = {b.date: b for b in await market_repo.bars(s, inst_id, today - timedelta(days=20))}
    end = max(stored) if stored else today
    series = await link.prices.daily_bars_async(inst, end - timedelta(days=14), end)
    lines = [f"{inst.yahoo_symbol}, IBKR vs stored (Yahoo):"]
    for bar in series.bars[-5:]:
        y = stored.get(bar.date)
        if y is None:
            lines.append(f"  {bar.date} close {bar.close}, no stored bar")
            continue
        diff = float(bar.close / y.close - 1) * 100
        volume = f"{bar.volume / y.volume:.2f}" if y.volume else "-"
        lines.append(
            f"  {bar.date} close {bar.close} vs {y.close} ({diff:+.2f} %), volume ratio {volume}"
        )
    return "\n".join(lines)


class _LogOnly:
    async def send(self, text: str) -> None:
        log.info("ibkr_check.message", text=text)


async def check(
    settings: Settings, sessions: Sessions, symbols: list[str], order_test: bool = False
) -> list[str]:
    """What M6 needs to know from the real gateway. Writes nothing except contract ids
    (and, with `order_test`, one far-from-market order that is cancelled right away)."""
    link = BrokerLink(settings, _LogOnly())
    if not await link.connect():
        return [f"Gateway not reachable at {settings.ib_host}:{settings.ib_port}"]
    ib = link.ib
    out: list[str] = []
    try:
        server_time = await ib.reqCurrentTimeAsync()
        out.append(f"Connected: server version {ib.client.serverVersion()}, time {server_time}")
        acc = await link.broker.account()
        out.append(
            f"Account ({acc.currency}): net liquidation {acc.net_liquidation}, "
            f"cash {acc.total_cash}, settled {acc.settled_cash}, available {acc.available_funds}"
        )
        out.append(f"Positions: {len(await link.broker.positions())}")
        missing = await sync_conids(link, sessions)
        out.append(f"Contracts without conid: {', '.join(missing) or 'none'}")
        quotes = ["Quotes via the API (5 s after subscribing):"]
        known: list[tuple[str, Instrument]] = []
        for symbol in symbols:
            found = await _instrument(sessions, symbol)
            if found is None:
                out.append(f"{symbol}: not in the universe")
                continue
            known.append((symbol, found[1]))
            out.append(await _compare_bars(link, sessions, *found))
            quotes.append("  " + await _quote_type(ib, found[1]))
        out += quotes
        for symbol, inst in known:
            ticks = await link.market_info.increments(inst)
            out.append(f"Price increments {symbol}: {ticks or 'unknown'}")
        if order_test and symbols:
            out += await _order_test(link, sessions, symbols[0])
    finally:
        ib.disconnect()
    return out


async def _order_test(link: BrokerLink, sessions: Sessions, symbol: str) -> list[str]:
    """Buy 1 share with a limit at half the price, read its status, cancel, read it again."""
    if not link.settings.ib_orders_enabled:
        return ["Order test skipped: IB_ORDERS_ENABLED=false"]
    found = await _instrument(sessions, symbol)
    if found is None:
        return [f"Order test skipped: {symbol} not in the universe"]
    inst_id, inst = found
    mid = await link.market_info.mid(inst)
    if mid is None:
        async with sessions() as s:
            mid = (await market_repo.last_closes(s, [inst_id])).get(inst_id)
    if mid is None:
        return ["Order test skipped: no price"]
    ticks = await link.market_info.increments(inst) or CENT_TICKS
    bracket_id = uuid4()
    spec = OrderSpec(
        order_ref=order_ref(bracket_id, "entry"),
        bracket_id=bracket_id,
        instrument=inst,
        kind="entry",
        action="BUY",
        order_type="LMT",
        quantity=Decimal(1),
        limit_price=to_tick(mid / 2, False, ticks),
        tif="DAY",
    )
    lines = [f"Order test: BUY 1 {symbol} LMT {spec.limit_price} ({spec.order_ref})"]
    placed = await link.broker.place([spec])
    lines.append(f"  placed: {placed[0].status}, broker id {placed[0].broker_order_id}")
    await asyncio.sleep(3)
    await link.broker.cancel(spec.order_ref)
    await asyncio.sleep(3)
    after = {o.order_ref: o for o in await link.broker.orders()}.get(spec.order_ref)
    lines.append(f"  after cancel: {after.status if after else 'not found'}")
    return lines
