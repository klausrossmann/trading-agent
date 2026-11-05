"""The agent's book at work: the trading context, the book from IBKR's fills, the IBKR sync
with its alerts, the end-of-day rebuild with the loss limits, the yearly tax report, and the
Telegram commands /positions, /pnl and /exit.

Orders go to IBKR or nowhere: while the gateway is disconnected nothing is placed, and the
step log and Telegram say so.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from trading_agent.core.db import Sessions
from trading_agent.ibkr import store as orders_repo
from trading_agent.ibkr.executor import Executor
from trading_agent.ibkr.market import MarketInfo
from trading_agent.ibkr.types import Bracket
from trading_agent.marketdata import calendars
from trading_agent.marketdata import store as market_repo
from trading_agent.marketdata.types import BERLIN, Increments, Instrument, Market, Observation
from trading_agent.news.rules import NewsParams
from trading_agent.portfolio import book as accounting
from trading_agent.portfolio import store as portfolio_repo
from trading_agent.portfolio import tax
from trading_agent.portfolio.fees import FeeSchedule, order_fees
from trading_agent.portfolio.snapshot import CENT_TICKS
from trading_agent.portfolio.types import Book, book_for_mode
from trading_agent.proposer import store as proposals_repo
from trading_agent.risk import engine
from trading_agent.risk.config import RiskConfig
from trading_agent.risk.controls import ControlCenter
from trading_agent.risk.types import Mode, PortfolioState
from trading_agent.screen.rules import PullbackParams
from trading_agent.telegram import messages
from trading_agent.telegram.bot import Command, Notifier

log = structlog.get_logger(__name__)

SETTLEMENT_SESSIONS: dict[Market, int] = {"US": 1, "EU": 2}  # T+1 US, T+2 Xetra


@dataclass
class TradingContext:
    sessions: Sessions
    executor: Executor
    center: ControlCenter
    risk: RiskConfig  # as in risk.yaml; `limits` has the mode's budget
    fees: FeeSchedule
    rules: PullbackParams  # entry validity, breakeven and time stop
    mode: Mode
    notifier: Notifier
    market_info: MarketInfo | None = None  # IBKR ticks and quotes
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    news: NewsParams = field(default_factory=NewsParams)  # news trades a day

    async def ticks(self, inst: Instrument) -> Increments:
        found = await self.market_info.increments(inst) if self.market_info else None
        return found or CENT_TICKS

    @property
    def book(self) -> Book:
        return book_for_mode(self.mode)

    @property
    def limits(self) -> RiskConfig:
        return self.risk.for_mode(self.mode)

    @property
    def account(self) -> str:
        """Key of the equity history: the markets of this mode, e.g. "US,EU"."""
        markets = self.risk.markets.paper if self.mode == "paper" else self.risk.markets.live
        return ",".join(markets)

    def connected(self) -> bool:
        return self.executor.broker.connected()


@dataclass
class LoadedBook:
    """Everything the accounting needs, loaded once per step."""

    brackets: list[tuple[Bracket, datetime]]
    fills: list[accounting.BookFill]
    instruments: dict[int, Instrument]
    marks: dict[int, Decimal]
    rates: dict[str, Decimal]


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


async def load(ctx: TradingContext) -> LoadedBook:
    async with ctx.sessions() as s:
        brackets, fills, instruments, usd = await _read_book(s, ctx.book, ctx.fees)
        marks = await market_repo.last_closes(s, instruments)
    today = ctx.clock().date()
    rates = {"EUR": Decimal(1)} | ({"USD": _rate_on(usd, today)} if usd else {})
    return LoadedBook(brackets, fills, instruments, marks, rates)


async def state(
    ctx: TradingContext, loaded: LoadedBook, today: date
) -> tuple[accounting.Account, PortfolioState]:
    """The account (both markets, one budget) and the risk engine's view of it."""
    budget = ctx.limits.capital.agent_budget_eur
    acct = accounting.account(
        budget,
        [b for b, _ in loaded.brackets],
        loaded.fills,
        loaded.instruments,
        loaded.marks,
        loaded.rates,
        today,
    )
    async with ctx.sessions() as s:
        history = await portfolio_repo.equity_history(s, ctx.book, ctx.account)
    orders_today = sum(
        1 for _, created in loaded.brackets if created.astimezone(BERLIN).date() == today
    )
    portfolio = accounting.portfolio_state(acct, budget, history, today, orders_today)
    return acct, portfolio


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


async def sync(ctx: TradingContext) -> list[Bracket]:
    before = {b.id: b for b in await ctx.executor.open_brackets()}
    changed = await ctx.executor.sync()
    await _announce(ctx, before, changed)
    return changed


async def rebuild_book(ctx: TradingContext, market: Market, day: date) -> None:
    """Round trips of the agent book from its brackets, the account's equity after `day`, and
    the loss limits measured at that close (a breach pauses the next session)."""
    loaded = await load(ctx)
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
    acct, portfolio = await state(ctx, loaded, day)
    key = ctx.account
    async with ctx.sessions.begin() as s:
        await portfolio_repo.replace_book(s, ctx.book, rows)
        await portfolio_repo.upsert_equity(
            s,
            ctx.book,
            key,
            day,
            equity_eur=acct.equity_eur,
            cash_eur=acct.cash_eur + acct.unsettled_eur,
            invested_eur=acct.invested_eur,
        )
    log.info("book.updated", book=ctx.book, account=key, equity_eur=str(acct.equity_eur))
    if trips := engine.loss_trips(portfolio, ctx.limits):
        log.warning(
            "loss_limits.breached",
            account=key,
            trips=trips,
            pnl_today_eur=str(portfolio.pnl_today_eur),
            pnl_week_eur=str(portfolio.pnl_week_eur),
            drawdown_eur=str(portfolio.drawdown_eur),
        )
        cal = calendars.CALENDAR_BY_MARKET[market]
        await ctx.center.apply_close_trips(trips, calendars.session_offset(cal, day, 1))


async def cancel_entries(ctx: TradingContext, reason: str) -> int:
    """Kill switch halt: cancel every entry not yet filled; stops stay."""
    pending = [b for b in await ctx.executor.open_brackets() if b.state in accounting.PENDING]
    for b in pending:
        await ctx.executor.cancel_entry(b.id, reason)
    return len(pending)


async def expected_positions(ctx: TradingContext) -> dict[int, tuple[str, float]]:
    """conid -> (symbol, open quantity) of the agent book after a sync with IBKR, for the
    reconciliation (a fill not yet synced would look like a mismatch)."""
    if ctx.connected():
        await sync(ctx)
    loaded = await load(ctx)
    out: dict[int, tuple[str, float]] = {}
    for b, _ in loaded.brackets:
        inst = loaded.instruments[b.instrument_id]
        if b.open_qty > 0 and inst.conid is not None:
            symbol, qty = out.get(inst.conid, (inst.yahoo_symbol, 0.0))
            out[inst.conid] = (symbol, qty + float(b.open_qty))
    return out


async def tax_report(
    sessions: Sessions, fees: FeeSchedule, year: int, book: Book = "agent_live"
) -> str:
    """The yearly tax helper for one book, stored as report `tax:<book>` at 31 Dec."""
    brackets, fills, instruments = await load_book(sessions, book, fees)
    body = tax.render(year, book, tax.sales([b for b, _ in brackets], fills, instruments, year))
    async with sessions.begin() as s:
        await portfolio_repo.save_report(s, f"tax:{book}", date(year, 12, 31), body)
    return body


# --- Telegram: /positions, /pnl ---


async def positions_text(ctx: TradingContext) -> str:
    loaded = await load(ctx)
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
                budget_eur=float(ctx.limits.capital.agent_budget_eur),
                exiting=b.state == "exiting",
            )
        )
    return messages.render_positions(ctx.book, held, pending)


def _change(history: Sequence[tuple[date, Decimal]], before: date, base: Decimal) -> float:
    """Last equity minus the last equity before `before` (the budget if none)."""
    earlier = [e for d, e in history if d < before]
    return float(history[-1][1] - (earlier[-1] if earlier else base))


async def pnl_text(ctx: TradingContext) -> str:
    budget = ctx.limits.capital.agent_budget_eur
    async with ctx.sessions() as s:
        history = await portfolio_repo.equity_history(s, ctx.book, ctx.account)
    as_of = history[-1][0] if history else None
    ref = as_of or ctx.clock().date()
    week_start = ref - timedelta(days=ref.weekday())
    month_start = ref.replace(day=1)
    line = messages.PnlLine(
        account=ctx.account,
        budget_eur=float(budget),
        as_of=as_of,
        day=_change(history, ref, budget) if history else 0.0,
        week=_change(history, week_start, budget) if history else 0.0,
        month=_change(history, month_start, budget) if history else 0.0,
        total=float(history[-1][1] - budget) if history else 0.0,
    )
    return messages.render_pnl(ctx.book, line)


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
        "pnl": Command("P&L by day, week, month and since start", pnl),
        "exit": Command("sell SYMBOL's open position at the next open (market order)", exit_),
    }


async def manual_exit(ctx: TradingContext, symbol: str) -> str:
    """Your decision after a review alert: a market exit for the open position in `symbol`."""
    loaded = await load(ctx)
    wanted = symbol.strip().upper()
    for b, _ in loaded.brackets:
        inst = loaded.instruments[b.instrument_id]
        if inst.yahoo_symbol.upper() != wanted or b.open_qty <= 0:
            continue
        if b.state != "filled":
            return f"{inst.yahoo_symbol}: an exit is already under way ({b.state})."
        if not ctx.connected():
            return f"IBKR not connected: no exit order for {inst.yahoo_symbol}. Try again later."
        await ctx.executor.request_exit(b.id, "manual")
        log.info("exit.manual", symbol=inst.yahoo_symbol, quantity=str(b.open_qty))
        return (
            f"Exit order for {inst.yahoo_symbol}: sell {messages.shares(b.open_qty)} at the "
            "market, at the next open. Stop and target are cancelled when it fills."
        )
    return f"No open position in {wanted}."
