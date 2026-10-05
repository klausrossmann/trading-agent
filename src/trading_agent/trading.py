"""Order placement and the daily execution cycle (IMPLEMENTATION.md 9.6; orchestration).

- `place`: today's ranked proposals of one market -> risk engine -> brackets at the broker.
- `end_of_day`: simulator fills from the day's bar, broker sync, breakeven stops, time stops,
  the agent book's trades and the sleeve's equity snapshot.
- `monitor`: broker sync during sessions when orders go to IBKR.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from trading_agent.backtest import SETTLEMENT_SESSIONS
from trading_agent.calc.fees import FeeSchedule, order_fees
from trading_agent.calc.indicators import atr, bars_to_frame
from trading_agent.controls import ControlCenter
from trading_agent.data import calendars
from trading_agent.data.ibkr import MarketInfo
from trading_agent.data.ingest import Sessions
from trading_agent.db import book as book_repo
from trading_agent.db import market as market_repo
from trading_agent.db import orders as orders_repo
from trading_agent.db import proposals as proposals_repo
from trading_agent.db import trades as trades_repo
from trading_agent.domain.market import (
    BERLIN,
    Bar,
    Increments,
    Instrument,
    Market,
    Observation,
)
from trading_agent.domain.orders import Bracket, BracketRequest
from trading_agent.domain.proposals import Proposal
from trading_agent.domain.risk import Mode, PortfolioState, RiskDecision
from trading_agent.domain.trading import Book, Trade, book_for_mode
from trading_agent.execution import sim
from trading_agent.execution.sim_broker import SimBroker
from trading_agent.executor import Executor
from trading_agent.notify import messages
from trading_agent.notify.telegram import Command, Notifier
from trading_agent.portfolio import book as accounting
from trading_agent.portfolio.snapshot import CENT_TICKS, proposal_levels, snapshot, to_tick
from trading_agent.risk import engine
from trading_agent.risk.config import RiskConfig
from trading_agent.strategies.pullback import PullbackParams

log = structlog.get_logger(__name__)

HISTORY_DAYS = 150  # calendar days of bars for ATR(14), 20-day value and 60-day correlations


@dataclass
class TradingContext:
    sessions: Sessions
    executor: Executor
    center: ControlCenter
    risk: RiskConfig
    fees: FeeSchedule
    rules: PullbackParams  # entry validity, breakeven and time stop, as in the backtest
    mode: Mode
    notifier: Notifier
    sim: SimBroker | None  # None: orders go to IBKR
    market_info: MarketInfo | None = None  # IBKR ticks and quotes, when the gateway is enabled
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    async def ticks(self, inst: Instrument) -> Increments:
        found = await self.market_info.increments(inst) if self.market_info else None
        return found or CENT_TICKS

    @property
    def book(self) -> Book:
        return book_for_mode(self.mode)

    def sleeve(self, market: Market) -> tuple[list[Market], RiskConfig]:
        markets = self.risk.markets.paper if self.mode == "paper" else self.risk.markets.live
        return next(
            (ms, cfg)
            for ms, cfg in self.risk.sleeves(list({*markets, market}), self.mode)
            if market in ms
        )


@dataclass
class _Book:
    """Everything the accounting needs, loaded once per step."""

    brackets: list[tuple[Bracket, datetime]]
    fills: list[accounting.BookFill]
    instruments: dict[int, Instrument]
    marks: dict[int, Decimal]
    rates: dict[str, Decimal]

    def sleeve(self, markets: Sequence[Market]) -> tuple[list[Bracket], list[accounting.BookFill]]:
        brackets = [
            b for b, _ in self.brackets if self.instruments[b.instrument_id].market in markets
        ]
        ids = {b.id for b in brackets}
        return brackets, [f for f in self.fills if f.bracket_id in ids]


def _rate_on(usd: Sequence[Observation], day: date) -> Decimal:
    known = [o.value for o in usd if o.date <= day]
    if known:
        return known[-1]
    if usd:
        return usd[0].value
    raise LookupError("no EUR/USD rate stored")


async def _read_book(
    s: AsyncSession, book: Book, fees: FeeSchedule
) -> tuple[
    list[tuple[Bracket, datetime]],
    list[accounting.BookFill],
    dict[int, Instrument],
    list[Observation],
]:
    brackets = await orders_repo.all_brackets(s, book)
    fills = await orders_repo.book_fills(s, book)
    instruments = await market_repo.instruments(s, {b.instrument_id for b, _ in brackets})
    usd = await market_repo.fx_rates(s, "USD")
    by_id = {b.id: b for b, _ in brackets}
    book_fills: list[accounting.BookFill] = []
    for bracket_id, kind, f in fills:
        inst = instruments[by_id[bracket_id].instrument_id]
        cal = calendars.CALENDAR_BY_MARKET[inst.market]
        day = f.ts.astimezone(calendars.calendar(cal).tz).date()
        side = "buy" if kind == "entry" else "sell"
        fee = f.commission or order_fees(fees, inst.market, side, f.quantity, f.price)
        book_fills.append(
            accounting.BookFill(
                bracket_id=bracket_id,
                kind=kind,
                day=day,
                quantity=f.quantity,
                price=f.price,
                fee=fee,
                eur_rate=Decimal(1) if inst.currency == "EUR" else _rate_on(usd, day),
                settles=calendars.session_offset(cal, day, SETTLEMENT_SESSIONS[inst.market]),
            )
        )
    return brackets, book_fills, instruments, usd


async def load_book(
    sessions: Sessions, book: Book, fees: FeeSchedule
) -> tuple[list[tuple[Bracket, datetime]], list[accounting.BookFill], dict[int, Instrument]]:
    """The book's brackets, its fills in EUR terms (ECB rate of the trade day, fees) and the
    instruments involved."""
    async with sessions() as s:
        brackets, fills, instruments, _ = await _read_book(s, book, fees)
    return brackets, fills, instruments


async def _load(ctx: TradingContext) -> _Book:
    async with ctx.sessions() as s:
        brackets, fills, instruments, usd = await _read_book(s, ctx.book, ctx.fees)
        marks = await market_repo.last_closes(s, instruments)
    today = ctx.clock().date()
    rates = {"EUR": Decimal(1)} | ({"USD": _rate_on(usd, today)} if usd else {})
    return _Book(brackets, fills, instruments, marks, rates)


async def _sleeve_state(
    ctx: TradingContext, loaded: _Book, market: Market, today: date
) -> tuple[accounting.Account, PortfolioState, RiskConfig, str]:
    markets, cfg = ctx.sleeve(market)
    key = ",".join(markets)
    brackets, fills = loaded.sleeve(markets)
    budget = cfg.capital.agent_budget_eur
    acct = accounting.account(
        budget, brackets, fills, loaded.instruments, loaded.marks, loaded.rates, today
    )
    async with ctx.sessions() as s:
        history = await book_repo.equity_history(s, ctx.book, key)
    orders_today = sum(
        1 for _, created in loaded.brackets if created.astimezone(BERLIN).date() == today
    )
    state = accounting.portfolio_state(acct, budget, history, today, orders_today)
    return acct, state, cfg, key


def _sessions_to_earnings(cal: str, today: date, dates: Sequence[date]) -> int | None:
    upcoming = [d for d in dates if d >= today]
    if not upcoming:
        return None
    return len(calendars.sessions(cal, today, min(upcoming))) - 1


async def _decide(
    ctx: TradingContext, loaded: _Book, p: Proposal, now: datetime, today: date
) -> tuple[RiskDecision, Instrument]:
    cal = calendars.CALENDAR_BY_MARKET[p.market]
    _, portfolio, cfg, _ = await _sleeve_state(ctx, loaded, p.market, today)
    held_ids = {h.instrument_id for h in portfolio.holdings}
    start = today - timedelta(days=HISTORY_DAYS)
    async with ctx.sessions() as s:
        inst = (await market_repo.instruments(s, [p.instrument_id]))[p.instrument_id]
        bars = await market_repo.bars(s, p.instrument_id, start)
        held = {i: await market_repo.bars(s, i, start) for i in held_ids}
        earnings = (await market_repo.earnings_dates(s, [p.instrument_id])).get(p.instrument_id, [])
    if inst.currency not in loaded.rates:
        raise LookupError(f"no EUR/{inst.currency} rate stored")
    market = snapshot(
        inst,
        bars,
        held,
        session_open=calendars.session_time(cal, today, "open"),
        session_close=calendars.session_time(cal, today, "close"),
        sessions_to_earnings=_sessions_to_earnings(cal, today, earnings),
        eur_rate=loaded.rates[inst.currency],
        mid=await ctx.market_info.mid(inst) if ctx.market_info else None,
    )
    controls = await ctx.center.controls()
    levels = proposal_levels(p, await ctx.ticks(inst))
    decision = engine.evaluate(p, levels, portfolio, market, controls, cfg, ctx.fees, now)
    return decision, inst


@dataclass
class Placement:
    placed: list[messages.PlacedLine] = field(default_factory=list[messages.PlacedLine])
    rejected: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])


async def place(ctx: TradingContext, market: Market) -> Placement:
    """Run after the open: only proposals from today's scan (as_of = previous session)."""
    now = ctx.clock()
    cal = calendars.CALENDAR_BY_MARKET[market]
    today = now.astimezone(calendars.calendar(cal).tz).date()
    result = Placement()
    if calendars.session_time(cal, today, "open") is None:
        log.info("place.skipped", market=market, reason="no session today")
        return result
    as_of = calendars.session_offset(cal, today, -1)
    async with ctx.sessions() as s:
        found = await proposals_repo.proposals(s, start=as_of, end=as_of)
    todo = sorted(
        (p for p in found if p.market == market and p.status == "proposed"),
        key=lambda p: (p.rank is None, p.rank or 0),
    )
    if not todo:
        log.info("place.nothing", market=market, as_of=as_of.isoformat())
        return result
    await ctx.executor.sync()
    expires_day = calendars.session_offset(cal, today, ctx.rules.entry_valid_sessions - 1)
    expires = calendars.session_time(cal, expires_day, "close")
    if expires is None:
        raise LookupError(f"no close for {expires_day}")
    for p in todo:
        decision, inst = await _decide(ctx, await _load(ctx), p, now, today)
        async with ctx.sessions.begin() as s:
            await book_repo.save_decision(s, p.id, decision, now)
        if decision.trip is not None:
            await ctx.center.apply_trip(decision.trip)
        entry, stop, target = decision.entry, decision.stop, decision.target
        if not decision.approved or entry is None or stop is None or target is None:
            first = decision.failures[0]
            result.rejected.append((p.yahoo_symbol, f"{first.name}: {first.detail}"))
            continue
        await ctx.executor.submit(
            BracketRequest(
                id=p.id,
                instrument=inst,
                quantity=decision.quantity,
                entry=entry,
                stop=stop,
                target=target,
                expires=expires,
            ),
            decision,
        )
        result.placed.append(
            messages.PlacedLine(
                p.yahoo_symbol, decision.quantity, float(entry), float(stop), float(target)
            )
        )
    broker = "simulator" if ctx.sim is not None else "IBKR"
    await ctx.notifier.send(
        messages.placement_summary(market, result.placed, result.rejected, broker)
    )
    log.info("place.done", market=market, placed=len(result.placed), rejected=len(result.rejected))
    return result


async def _announce(
    ctx: TradingContext, before: dict[UUID, Bracket], changed: Sequence[Bracket]
) -> None:
    async with ctx.sessions() as s:
        names = await market_repo.instruments(s, {b.instrument_id for b in changed})
    for b in changed:
        old = before.get(b.id)
        symbol = names[b.instrument_id].yahoo_symbol
        if b.entry_price is not None and b.filled_qty > (old.filled_qty if old else 0):
            await ctx.notifier.send(messages.fill_alert(symbol, b.filled_qty, float(b.entry_price)))
        if b.state == "closed" and b.exit_price is not None:
            await ctx.notifier.send(
                messages.exit_alert(
                    symbol, b.exit_qty, float(b.exit_price), b.exit_reason or "exit"
                )
            )
        if b.state in ("expired", "cancelled"):
            await ctx.notifier.send(messages.entry_ended_alert(symbol, b.state))


async def _sync(ctx: TradingContext) -> list[Bracket]:
    before = {b.id: b for b in await ctx.executor.open_brackets()}
    changed = await ctx.executor.sync()
    await _announce(ctx, before, changed)
    return changed


async def monitor(ctx: TradingContext) -> list[Bracket]:
    """Every few minutes in sessions; the simulator only moves at the end of the day."""
    if ctx.sim is not None:
        return []
    return await _sync(ctx)


async def end_of_day(ctx: TradingContext, market: Market) -> None:
    """After the market's EOD bars are stored."""
    now = ctx.clock()
    cal = calendars.CALENDAR_BY_MARKET[market]
    day = calendars.last_closed_session(cal, now)
    close_at = calendars.session_time(cal, day, "close") or now
    open_ = await ctx.executor.open_brackets()
    async with ctx.sessions() as s:
        instruments = await market_repo.instruments(s, {b.instrument_id for b in open_})
        bars = await market_repo.bars_on(s, instruments, day)
    mine = [b for b in open_ if instruments[b.instrument_id].market == market]
    if ctx.sim is not None:
        for inst_id in {b.instrument_id for b in mine}:
            if (bar := bars.get(inst_id)) is not None:
                ctx.sim.on_bar(inst_id, _sim_bar(bar), close_at)
    await _sync(ctx)
    await _manage_positions(ctx, market, day, bars)
    await rebuild_book(ctx, market, day)


def _sim_bar(bar: Bar) -> sim.Bar:
    return sim.Bar(float(bar.open), float(bar.high), float(bar.low), float(bar.close))


async def _manage_positions(
    ctx: TradingContext, market: Market, day: date, bars: dict[int, Bar]
) -> None:
    """Breakeven at +1R, the optional ATR trail and the time stop, on the day's bar as in the
    backtest."""
    cal = calendars.CALENDAR_BY_MARKET[market]
    loaded = await _load(ctx)
    fills = {f.bracket_id: f.day for f in loaded.fills if f.kind == "entry"}
    for b in await ctx.executor.open_brackets():
        inst = loaded.instruments.get(b.instrument_id)
        entered = fills.get(b.id)
        if inst is None or inst.market != market or b.state != "filled" or entered is None:
            continue
        held = len(calendars.sessions(cal, entered, day)) - 1
        if held >= ctx.rules.time_stop_sessions:
            await ctx.executor.request_exit(b.id, "time")
            continue
        bar = bars.get(b.instrument_id)
        if bar is None or entered == day or b.entry_price is None:
            continue
        trigger = float(b.entry_price) + ctx.rules.breakeven_r * float(b.entry - b.initial_stop)
        moved = sim.trailed_stop(_sim_bar(bar), float(b.stop), float(b.entry_price), trigger)
        if ctx.rules.trail_atr is not None and moved >= float(b.entry_price):
            async with ctx.sessions() as s:
                since = entered - timedelta(days=HISTORY_DAYS)
                history = [
                    x for x in await market_repo.bars(s, b.instrument_id, since) if x.date <= day
                ]
            highest = max(float(x.close) for x in history if x.date >= entered)
            last_atr = float(atr(bars_to_frame(history)).iloc[-1])
            moved = sim.chandelier_stop(
                moved, float(b.entry_price), highest, last_atr, ctx.rules.trail_atr
            )
        new_stop = to_tick(Decimal(str(moved)), True, await ctx.ticks(inst))
        if new_stop > b.stop:
            await ctx.executor.move_stop(b.id, new_stop)
            log.info("stop.moved", symbol=inst.yahoo_symbol, stop=str(new_stop))


async def rebuild_book(ctx: TradingContext, market: Market, day: date) -> None:
    """Round trips of the agent book from its brackets, and the sleeve's equity after `day`."""
    loaded = await _load(ctx)
    async with ctx.sessions() as s:
        found = await proposals_repo.proposals(s)
    signals = {p.id: (p.as_of, p.strategy) for p in found}

    def sessions_between(m: Market, start: date, end: date) -> int:
        return len(calendars.sessions(calendars.CALENDAR_BY_MARKET[m], start, end)) - 1

    rows = accounting.trades(
        ctx.book,
        [b for b, _ in loaded.brackets],
        loaded.fills,
        loaded.instruments,
        signals,
        sessions_between,
    )
    acct, _, _, key = await _sleeve_state(ctx, loaded, market, day)
    async with ctx.sessions.begin() as s:
        await trades_repo.replace_book(s, ctx.book, rows)
        await book_repo.upsert_equity(
            s,
            ctx.book,
            key,
            day,
            equity_eur=acct.equity_eur,
            cash_eur=acct.cash_eur + acct.unsettled_eur,
            invested_eur=acct.invested_eur,
        )
    log.info("book.updated", book=ctx.book, sleeve=key, equity_eur=str(acct.equity_eur))


async def cancel_entries(ctx: TradingContext, reason: str) -> int:
    """Kill switch halt: cancel every entry not yet filled; stops stay."""
    pending = [b for b in await ctx.executor.open_brackets() if b.state in accounting.PENDING]
    for b in pending:
        await ctx.executor.cancel_entry(b.id, reason)
    return len(pending)


async def expected_positions(ctx: TradingContext) -> dict[int, tuple[str, float]]:
    """conid -> (symbol, open quantity) of the agent book, for reconciliation with IBKR."""
    loaded = await _load(ctx)
    out: dict[int, tuple[str, float]] = {}
    for b, _ in loaded.brackets:
        inst = loaded.instruments[b.instrument_id]
        if b.open_qty > 0 and inst.conid is not None:
            symbol, qty = out.get(inst.conid, (inst.yahoo_symbol, 0.0))
            out[inst.conid] = (symbol, qty + b.open_qty)
    return out


# --- Telegram: /positions, /pnl ---


async def positions_text(ctx: TradingContext) -> str:
    loaded = await _load(ctx)
    held: list[messages.HeldLine] = []
    pending: list[messages.PendingLine] = []
    for b, _ in loaded.brackets:
        inst = loaded.instruments[b.instrument_id]
        if b.state in accounting.PENDING and b.quantity > b.filled_qty:
            pending.append(
                messages.PendingLine(
                    inst.yahoo_symbol, b.quantity - b.filled_qty, float(b.entry), b.expires
                )
            )
        if b.open_qty <= 0 or b.entry_price is None:
            continue
        last = loaded.marks.get(b.instrument_id, b.entry_price)
        risk = b.entry - b.initial_stop
        _, cfg = ctx.sleeve(inst.market)
        held.append(
            messages.HeldLine(
                symbol=inst.yahoo_symbol,
                quantity=b.open_qty,
                entry=float(b.entry_price),
                stop=float(b.stop),
                target=float(b.target),
                last=float(last),
                r_now=float((last - b.entry_price) / risk) if risk > 0 else 0.0,
                pnl_eur=float(b.open_qty * (last - b.entry_price) / loaded.rates[inst.currency]),
                budget_eur=float(cfg.capital.agent_budget_eur),
                exiting=b.state == "exiting",
            )
        )
    return messages.render_positions(ctx.book, held, pending)


def _change(history: Sequence[tuple[date, Decimal]], before: date, base: Decimal) -> float:
    """Last equity minus the last equity before `before` (the budget if none)."""
    earlier = [e for d, e in history if d < before]
    return float(history[-1][1] - (earlier[-1] if earlier else base))


async def pnl_text(ctx: TradingContext) -> str:
    markets = ctx.risk.markets.paper if ctx.mode == "paper" else ctx.risk.markets.live
    async with ctx.sessions() as s:
        baseline = await trades_repo.trades(s, "baseline_sim")
    lines: list[messages.PnlLine] = []
    for sleeve_markets, cfg in ctx.risk.sleeves(list(markets), ctx.mode):
        key = ",".join(sleeve_markets)
        budget = cfg.capital.agent_budget_eur
        async with ctx.sessions() as s:
            history = await book_repo.equity_history(s, ctx.book, key)
        as_of = history[-1][0] if history else None
        ref = as_of or ctx.clock().date()
        week_start = ref - timedelta(days=ref.weekday())
        month_start = ref.replace(day=1)
        mine = [t for t in baseline if t.market in sleeve_markets and t.exit_date is not None]

        def closed_since(start: date, trades: Sequence[Trade] = mine) -> float:
            return sum(t.pnl_net_eur or 0.0 for t in trades if t.exit_date and t.exit_date >= start)

        lines.append(
            messages.PnlLine(
                sleeve=key,
                budget_eur=float(budget),
                as_of=as_of,
                day=_change(history, ref, budget) if history else 0.0,
                week=_change(history, week_start, budget) if history else 0.0,
                month=_change(history, month_start, budget) if history else 0.0,
                total=float(history[-1][1] - budget) if history else 0.0,
                baseline=(
                    closed_since(week_start),
                    closed_since(month_start),
                    closed_since(date.min),
                ),
            )
        )
    return messages.render_pnl(ctx.book, lines)


def commands(ctx: TradingContext) -> dict[str, Command]:
    async def positions(_: list[str]) -> str:
        return await positions_text(ctx)

    async def pnl(_: list[str]) -> str:
        return await pnl_text(ctx)

    async def exit_(args: list[str]) -> str:
        if not args:
            return "Usage: /exit SYMBOL, e.g. /exit AAPL"
        return await manual_exit(ctx, args[0])

    return {
        "positions": Command("open positions and pending entries of the agent book", positions),
        "pnl": Command("P&L by day, week, month and since start, with the baseline", pnl),
        "exit": Command("sell SYMBOL's open position at the next open (market order)", exit_),
    }


async def manual_exit(ctx: TradingContext, symbol: str) -> str:
    """Your decision after a review alert: a market exit for the open position in `symbol`."""
    loaded = await _load(ctx)
    wanted = symbol.strip().upper()
    for b, _ in loaded.brackets:
        inst = loaded.instruments[b.instrument_id]
        if inst.yahoo_symbol.upper() != wanted or b.open_qty <= 0:
            continue
        if b.state != "filled":
            return f"{inst.yahoo_symbol}: an exit is already under way ({b.state})."
        await ctx.executor.request_exit(b.id, "manual")
        log.info("exit.manual", symbol=inst.yahoo_symbol, quantity=b.open_qty)
        return (
            f"Exit order for {inst.yahoo_symbol}: sell {b.open_qty} at the market, "
            "at the next open. Stop and target are cancelled when it fills."
        )
    return f"No open position in {wanted}."
