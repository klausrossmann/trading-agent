"""Placement and the daily execution cycle with the simulator broker (M8 step 4)."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from trading_agent import jobs, reports, trading
from trading_agent.backtest import load_backtest_config
from trading_agent.controls import ControlCenter
from trading_agent.data import calendars, ingest
from trading_agent.data.ingest import Sessions
from trading_agent.data.universe import Universe
from trading_agent.db import book as book_repo
from trading_agent.db import market as market_repo
from trading_agent.db import proposals as proposals_repo
from trading_agent.db import reports as reports_repo
from trading_agent.db import trades as trades_repo
from trading_agent.db.models import BracketRow
from trading_agent.domain.market import Bar, BarSeries, Instrument, Observation
from trading_agent.domain.proposals import Proposal
from trading_agent.execution.sim_broker import SimBroker
from trading_agent.executor import Executor
from trading_agent.risk.config import load_risk_config
from trading_agent.settings import load_fees

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parents[2]
D = Decimal
TODAY = date(2026, 10, 7)
OPEN = datetime(2026, 10, 7, 13, 30, tzinfo=UTC)


class Inbox:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


class Clock:
    def __init__(self) -> None:
        self.now = OPEN + timedelta(minutes=15)

    def __call__(self) -> datetime:
        return self.now

    def eod(self, day: date) -> None:
        close = calendars.session_time("XNYS", day, "close")
        assert close is not None
        self.now = close + timedelta(minutes=35)


def _inst(symbol: str) -> Instrument:
    return Instrument(
        symbol=symbol,
        yahoo_symbol=symbol,
        name=symbol,
        market="US",
        exchange="SMART",
        currency="USD",
        kind="stock",
        sector="Technology" if symbol == "AAA" else "Energy",
        indices=("SP100",),
    )


def _bar(day: date, o: float, h: float, low: float, c: float) -> Bar:
    return Bar(
        date=day, open=D(str(o)), high=D(str(h)), low=D(str(low)), close=D(str(c)), volume=1_000_000
    )


async def _add_bar(sessions: Sessions, inst_id: int, bar: Bar) -> None:
    async with sessions.begin() as s:
        await market_repo.upsert_bars(
            s, inst_id, BarSeries(yahoo_symbol="x", source="test", fetched_at=OPEN, bars=(bar,))
        )


def _proposal(inst_id: int, symbol: str, target: float, rank: int) -> Proposal:
    return Proposal(
        id=uuid4(),
        source="agent",
        as_of=date(2026, 10, 6),
        instrument_id=inst_id,
        yahoo_symbol=symbol,
        market="US",
        sector=None,
        status="proposed",
        strategy="pullback_uptrend",
        entry_ref="close",
        stop_ref="swing_low",
        target_ref="r1",
        entry=100.0,
        stop=96.0,
        target=target,
        rank=rank,
        thesis="t",
        invalidation="i",
    )


async def _setup(
    sessions: Sessions, *, time_stop: int = 15
) -> tuple[trading.TradingContext, Clock, Inbox, dict[str, int]]:
    async with sessions.begin() as s:
        await s.execute(text("TRUNCATE kill_switch, equity_daily, audit_log"))
    universe = Universe(generated=TODAY, benchmarks={}, instruments=[_inst("AAA"), _inst("BBB")])
    await ingest.sync_universe(sessions, universe)
    days = calendars.sessions("XNYS", TODAY - timedelta(days=200), date(2026, 10, 6))
    async with sessions.begin() as s:
        ids = {i.yahoo_symbol: k for k, i in (await market_repo.active_instruments(s)).items()}
        for inst_id in ids.values():
            await market_repo.upsert_bars(
                s,
                inst_id,
                BarSeries(
                    yahoo_symbol="x",
                    source="test",
                    fetched_at=OPEN,
                    bars=tuple(_bar(d, 100, 101, 99, 100) for d in days),
                ),
            )
        await market_repo.upsert_fx(
            s,
            "USD",
            [Observation(date=d, value=D("1.10")) for d in days],
            source="test",
            fetched_at=OPEN,
        )
        await proposals_repo.save_proposals(
            s, [_proposal(ids["AAA"], "AAA", 108.0, 1), _proposal(ids["BBB"], "BBB", 106.0, 2)]
        )
    clock, inbox = Clock(), Inbox()
    sim = SimBroker(clock=clock)
    center = ControlCenter(sessions, "paper", notifier=inbox, clock=clock)
    rules = load_backtest_config(ROOT / "config").pullback.model_copy(
        update={"time_stop_sessions": time_stop}
    )
    ctx = trading.TradingContext(
        sessions=sessions,
        executor=Executor(sessions, sim, "agent_paper", clock),
        center=center,
        risk=load_risk_config(ROOT / "config"),
        fees=load_fees(ROOT / "config"),
        rules=rules,
        mode="paper",
        notifier=inbox,
        sim=sim,
        clock=clock,
    )
    center.on_halt = lambda: trading.cancel_entries(ctx, "halted")
    return ctx, clock, inbox, ids


async def test_place_fill_breakeven_and_stop(sessions: Sessions) -> None:
    ctx, clock, inbox, ids = await _setup(sessions)

    result = await trading.place(ctx, "US")
    assert [p.symbol for p in result.placed] == ["AAA"]
    assert result.rejected == [("BBB", "levels: R:R 1.50 below 2.0")]
    assert (await trading.positions_text(ctx)).endswith(
        "pending AAA 3 limit 100.00 until Thu 08 Oct 22:00"
    )
    assert inbox.sent[-1].splitlines()[:2] == [
        "📤 Orders US (simulator)",
        "  AAA 3 @ 100.00, stop 96.00, target 108.00",
    ]
    async with sessions() as s:
        stored = await book_repo.decisions(s, [p.id for p in await proposals_repo.proposals(s)])
    assert sorted(d.approved for d in stored.values()) == [False, True]
    assert await trading.monitor(ctx) == []  # the simulator moves only at the end of the day

    await _add_bar(sessions, ids["AAA"], _bar(TODAY, 100.5, 101, 99.5, 100))
    clock.eod(TODAY)
    await trading.end_of_day(ctx, "US")
    assert inbox.sent[-1] == "🟢 Bought AAA 3 @ 100.05"
    async with sessions() as s:
        [trade] = await trades_repo.book_trades(s, "agent_paper")
        history = await book_repo.equity_history(s, "agent_paper", "US")
    assert (trade.quantity, trade.exit_date) == (3, None)
    assert history[0][0] == TODAY
    cmds = trading.commands(ctx)
    positions = await cmds["positions"].run([])
    assert positions == (
        "📊 agent_paper\n  AAA 3 @ 100.05, stop 96.00, target 108.00, last 100.00: -0.01 R, "
        "−€0.14 (−0.0 %)"
    )
    pnl = str(await cmds["pnl"].run([]))
    assert pnl.splitlines()[0] == "💶 P&L agent_paper (equity at the last close)"
    assert pnl.splitlines()[1].startswith("US (budget €1,000, Wed 7 Oct): day −€0.")
    assert pnl.splitlines()[3].startswith("EU (budget €5,000, no close yet): day €0.00")

    day2 = date(2026, 10, 8)
    await _add_bar(sessions, ids["AAA"], _bar(day2, 101, 105, 100.5, 104))
    clock.eod(day2)
    await trading.end_of_day(ctx, "US")
    [b] = await ctx.executor.open_brackets()
    assert b.stop == D("100.05")  # breakeven after +1R

    day3 = date(2026, 10, 9)
    await _add_bar(sessions, ids["AAA"], _bar(day3, 101, 101.5, 99, 99.5))
    clock.eod(day3)
    await trading.end_of_day(ctx, "US")
    assert inbox.sent[-1] == "🔴 Sold AAA 3 @ 100.00 (stop)"
    async with sessions() as s:
        [trade] = await trades_repo.book_trades(s, "agent_paper")
        history = await book_repo.equity_history(s, "agent_paper", "US")
    assert (trade.exit_date, trade.exit_reason) == (day3, "stop")
    assert trade.pnl_net_eur is not None
    assert trade.pnl_net_eur < 0  # breakeven minus fees and slippage
    assert [d for d, _ in history] == [TODAY, day2, day3]

    universe = Universe(generated=TODAY, benchmarks={}, instruments=[_inst("AAA")])
    book = jobs.BookContext.load(ROOT / "config", sessions, universe)
    body = await reports.tax_report(book, 2026, "agent_paper")
    assert "| 2026-10-09 | AAA | 3 | 2026-10-07 |" in body
    async with sessions() as s:
        assert [d for d, _ in await reports_repo.reports(s, "tax:agent_paper")] == [
            date(2026, 12, 31)
        ]


async def test_time_stop_exits_at_the_next_open(sessions: Sessions) -> None:
    ctx, clock, inbox, ids = await _setup(sessions, time_stop=1)
    await trading.place(ctx, "US")
    await _add_bar(sessions, ids["AAA"], _bar(TODAY, 100.5, 101, 99.5, 100))
    clock.eod(TODAY)
    await trading.end_of_day(ctx, "US")
    day2 = date(2026, 10, 8)
    await _add_bar(sessions, ids["AAA"], _bar(day2, 100, 101, 99.5, 100.5))
    clock.eod(day2)
    await trading.end_of_day(ctx, "US")
    [b] = await ctx.executor.open_brackets()
    assert (b.state, b.exit_reason) == ("exiting", "time")
    day3 = date(2026, 10, 9)
    await _add_bar(sessions, ids["AAA"], _bar(day3, 102, 103, 101, 102))
    clock.eod(day3)
    await trading.end_of_day(ctx, "US")
    assert inbox.sent[-1] == "🔴 Sold AAA 3 @ 101.95 (time)"
    assert await ctx.executor.open_brackets() == []


async def test_halt_cancels_pending_entries(sessions: Sessions) -> None:
    ctx, clock, inbox, _ = await _setup(sessions)
    await trading.place(ctx, "US")
    await ctx.center.halt("test")
    async with sessions() as s:
        [row] = (await s.scalars(select(BracketRow))).all()
    assert row.cancel_reason == "halted"
    clock.eod(TODAY)
    await trading.end_of_day(ctx, "US")
    assert inbox.sent[-1] == "⌛ AAA: entry cancelled without a fill"
    assert await ctx.executor.open_brackets() == []
    # halted: tomorrow's placement is refused by the risk engine
    clock.now = datetime(2026, 10, 8, 13, 45, tzinfo=UTC)
    async with sessions.begin() as s:
        await proposals_repo.save_proposals(
            s,
            [
                _proposal(row.instrument_id, "AAA", 108.0, 1).model_copy(
                    update={"as_of": TODAY, "id": uuid4()}
                )
            ],
        )
    result = await trading.place(ctx, "US")
    assert result.rejected == [("AAA", "global: trading is halted")]
