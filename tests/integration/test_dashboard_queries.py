from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from fake_broker import Bar as SimBar
from fake_broker import SimBroker
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from trading_agent.core import step_store as steps_repo
from trading_agent.core.db import Sessions
from trading_agent.core.settings import Settings
from trading_agent.core.steps import Step
from trading_agent.dashboard import queries
from trading_agent.ibkr.executor import Executor
from trading_agent.ibkr.types import BracketRequest
from trading_agent.llm.store import DbAnalysisStore
from trading_agent.llm.types import AnalysisRecord, LlmCall, ValidationIssue
from trading_agent.marketdata import ingest
from trading_agent.marketdata import store as repo
from trading_agent.marketdata.types import Bar, BarSeries, Instrument, Observation
from trading_agent.marketdata.universe import Universe
from trading_agent.portfolio import store as portfolio_repo
from trading_agent.portfolio.types import Trade
from trading_agent.proposer import store as proposals_repo
from trading_agent.proposer.types import Proposal
from trading_agent.risk import store as risk_repo
from trading_agent.risk.store import save_kill_switch
from trading_agent.risk.types import Check, KillSwitch, RiskDecision

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 7, 20, 0, tzinfo=UTC)


def _inst(symbol: str, market: str = "US") -> Instrument:
    return Instrument(
        symbol=symbol,
        yahoo_symbol=symbol,
        name=symbol,
        market=market,  # pyright: ignore[reportArgumentType]
        exchange="SMART",
        currency="USD" if market == "US" else "EUR",
        kind="stock",
        indices=("SP100",),
    )


def _bars(*days: int) -> tuple[Bar, ...]:
    return tuple(
        Bar(
            date=date(2026, 10, d),
            open=Decimal(100),
            high=Decimal(101),
            low=Decimal(99),
            close=Decimal(100 + d),
            volume=1000,
        )
        for d in days
    )


async def _seed(sessions: Sessions) -> dict[str, int]:
    universe = Universe(
        generated=date(2026, 10, 3),
        benchmarks={},
        instruments=[_inst("AAA"), _inst("BBB"), _inst("SAP.DE", "EU")],
    )
    await ingest.sync_universe(sessions, universe)
    async with sessions.begin() as s:
        ids = {i.yahoo_symbol: k for k, i in (await repo.active_instruments(s)).items()}
        for symbol, days in (("AAA", (5, 6, 7)), ("BBB", (5, 6)), ("SAP.DE", (6,))):
            await repo.upsert_bars(
                s,
                ids[symbol],
                BarSeries(yahoo_symbol=symbol, source="test", fetched_at=NOW, bars=_bars(*days)),
            )
        await repo.upsert_fx(
            s,
            "USD",
            [
                Observation(date=date(2026, 10, d), value=Decimal(v))
                for d, v in ((6, "1.16"), (7, "1.17"))
            ],
            source="ecb",
            fetched_at=NOW,
        )
        trade = Trade(
            book="agent_paper",
            strategy="pullback_uptrend",
            instrument_id=ids["AAA"],
            yahoo_symbol="AAA",
            market="US",
            sector=None,
            signal_date=date(2026, 10, 5),
            entry_date=date(2026, 10, 6),
            entry_price=100.0,
            quantity=3,
            stop=95.0,
            target=110.0,
            risk_eur=13.0,
            fees=2.0,
            fees_eur=1.71,
        )
        closed = trade.model_copy(
            update={
                "instrument_id": ids["BBB"],
                "yahoo_symbol": "BBB",
                "exit_date": date(2026, 10, 7),
                "exit_price": 110.0,
                "exit_reason": "target",
                "pnl_net_eur": 22.5,
                "r_multiple": 1.731,
                "holding_sessions": 1,
            }
        )
        await portfolio_repo.replace_book(s, "agent_paper", [trade, closed])
        proposal = Proposal(
            id=uuid4(),
            source="agent",
            as_of=date(2026, 10, 6),
            instrument_id=ids["AAA"],
            yahoo_symbol="AAA",
            market="US",
            sector=None,
            status="proposed",
            strategy="pullback_uptrend",
            entry=100.0,
            stop=96.0,
            target=108.0,
            rank=1,
            thesis="t",
            invalidation="i",
        )
        (proposal_id,) = await proposals_repo.save_proposals(s, [proposal])
        await proposals_repo.set_label(s, proposal_id, "agree", "clean")
    await DbAnalysisStore(sessions).save(
        AnalysisRecord(
            module="technical",
            prompt_version=1,
            model="google:gemini-3.8-flash",
            instrument_id=ids["AAA"],
            as_of=date(2026, 10, 6),
            input_hash="h1",
            input={"close": 106},
            output={"rating": "buy", "setup_quality": 0.7},
            issues=(ValidationIssue(code="ungrounded_number", message="42", severity="warning"),),
            status="ok",
            cost_usd=0.002,
        ),
        [
            LlmCall(
                role="analysis",
                model="google:gemini-3.8-flash",
                module="technical",
                tokens_in=1000,
                tokens_out=200,
                requests=1,
                cost_usd=0.002,
                latency_ms=900,
            )
        ],
    )
    return ids


async def test_dashboard_queries(sessions: Sessions, settings: Settings) -> None:
    ids = await _seed(sessions)
    async with sessions() as s:
        (proposal,) = await proposals_repo.proposals(s)
    hit = await DbAnalysisStore(sessions).cached("h1")
    assert hit is not None
    clock = lambda: datetime(2026, 10, 7, 14, 0, tzinfo=UTC)  # noqa: E731
    broker = SimBroker(clock=clock)
    ex = Executor(sessions, broker, "agent_paper", clock)
    inst = _inst("AAA").model_copy(update={"id": ids["AAA"]})
    request = BracketRequest(
        id=proposal.id,
        instrument=inst,
        quantity=Decimal(3),
        entry=Decimal(100),
        stop=Decimal(96),
        target=Decimal(108),
        expires=datetime(2026, 10, 8, 20, 0, tzinfo=UTC),
    )
    await ex.submit(request, RiskDecision(approved=True, checks=(), quantity=Decimal(3)))
    broker.on_bar(ids["AAA"], SimBar(101, 102, 99, 101), datetime(2026, 10, 7, 20, 0, tzinfo=UTC))
    clock = lambda: datetime(2026, 10, 7, 20, 35, tzinfo=UTC)  # noqa: E731
    ex.clock = clock
    await ex.sync()
    async with sessions.begin() as s:
        await steps_repo.add_steps(
            s,
            [
                Step(
                    day=date(2026, 10, 7),
                    market="US",
                    instrument_id=ids["AAA"],
                    symbol="AAA",
                    step="technical",
                    status="passed",
                    summary="rating buy",
                    analysis_id=hit.id,
                    proposal_id=proposal.id,
                )
            ],
        )
    engine = queries.read_only_engine(settings.database_url)
    try:
        async with AsyncSession(engine) as s:
            trades = await queries.trades(s)
            rate = await queries.usd_per_eur(s)
            status = await queries.data_status(s)
            costs = await queries.llm_costs(s)
            days = await queries.step_days(s)
            on_day = await queries.steps(s, day=date(2026, 10, 7))
            of_trade = await queries.steps(s, proposal_id=proposal.id)
            analysis = await queries.analysis(s, hit.id)
            missing = await queries.analysis(s, -1)
            positions = await queries.positions(s)
            sold = await queries.sold(s)
    finally:
        await engine.dispose()

    assert [(t.yahoo_symbol, t.exit_date) for t in trades] == [
        ("AAA", None),
        ("BBB", date(2026, 10, 7)),
    ]
    assert trades[1].pnl_net_eur == pytest.approx(22.5)
    assert rate == pytest.approx(1.17)
    assert status.values.tolist() == [
        ["Bars EU", date(2026, 10, 6), "1 symbols, 0 behind"],
        ["Bars US", date(2026, 10, 7), "2 symbols, 1 behind"],
        ["EUR/USD", date(2026, 10, 7), "ECB"],
    ]
    (call,) = costs.to_dict("records")
    assert (call["role"], call["calls"], call["errors"]) == ("analysis", 1, 0)
    assert call["cost_usd"] == pytest.approx(0.002)

    assert days == [date(2026, 10, 7)]
    assert list(on_day["step"]) == ["order", "broker", "fill", "technical"]
    assert list(of_trade["symbol"].unique()) == ["AAA"]
    assert len(of_trade) == len(on_day)
    assert analysis is not None
    assert (analysis["module"], analysis["input"], analysis["issues"][0]["code"]) == (
        "technical",
        {"close": 106},
        "ungrounded_number",
    )
    assert missing is None

    (held,) = positions.to_dict("records")
    assert (held["symbol"], held["state"], held["open_qty"], held["entry_price"]) == (
        "AAA",
        "filled",
        3.0,
        pytest.approx(100.05),
    )
    assert held["id"] == str(proposal.id)
    assert held["entered"] == datetime(2026, 10, 7, 20, 0, tzinfo=UTC)
    assert (held["last_date"], held["last_close"]) == (date(2026, 10, 7), 107.0)
    (row,) = sold.to_dict("records")
    assert (row["symbol"], row["exit_reason"], row["id"]) == ("BBB", "target", None)


async def test_dashboard_connection_is_read_only(sessions: Sessions, settings: Settings) -> None:
    engine = queries.read_only_engine(settings.database_url)
    try:
        async with AsyncSession(engine) as s:
            with pytest.raises(DBAPIError, match="read-only transaction"):
                await s.execute(text("DELETE FROM trades"))
    finally:
        await engine.dispose()


async def test_risk_and_report_queries(sessions: Sessions, settings: Settings) -> None:
    ids = await _seed(sessions)
    async with sessions.begin() as s:
        await s.execute(text("TRUNCATE kill_switch, equity_daily, reports"))
        p = Proposal(
            id=uuid4(),
            source="agent",
            as_of=date(2026, 10, 6),
            instrument_id=ids["AAA"],
            yahoo_symbol="AAA",
            market="US",
            sector=None,
            status="proposed",
            strategy="s",
            thesis="t",
            invalidation="i",
        )
        [proposal_id] = await proposals_repo.save_proposals(s, [p])  # may keep a seeded id
        decision = RiskDecision(
            approved=False,
            checks=(
                Check(name="proposal", outcome="pass"),
                Check(name="portfolio", outcome="fail", detail="4 open"),
            ),
        )
        await risk_repo.save_decision(s, proposal_id, decision, NOW)
        await portfolio_repo.upsert_equity(
            s,
            "agent_paper",
            "US",
            date(2026, 10, 7),
            equity_eur=Decimal("990.5"),
            cash_eur=Decimal(700),
            invested_eur=Decimal("290.5"),
        )
        await save_kill_switch(s, KillSwitch(state="halted", reason="/stop", since=NOW), "test")
    engine = queries.read_only_engine(settings.database_url)
    try:
        async with AsyncSession(engine) as s:
            equity = await queries.equity(s)
            state = await queries.kill_switch(s)
            brackets = await queries.open_brackets(s)
            closes = await queries.closes(s, [ids["AAA"], ids["BBB"]], date(2026, 10, 1))
            rejected = await queries.rejections(s, date(2026, 10, 1))
    finally:
        await engine.dispose()
    assert equity.values.tolist() == [["agent_paper", "US", date(2026, 10, 7), 990.5]]
    assert state == "halted (/stop)"
    assert brackets.empty
    assert list(closes.columns) == [ids["AAA"], ids["BBB"]]
    assert len(closes) == 3
    assert rejected.values.tolist() == [[date(2026, 10, 6), "AAA", "portfolio", "4 open"]]
