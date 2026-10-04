from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from trading_agent.dashboard import queries
from trading_agent.data import ingest
from trading_agent.data.ingest import Sessions
from trading_agent.data.universe import Universe
from trading_agent.db import market as repo
from trading_agent.db import proposals as proposals_repo
from trading_agent.db import trades as trades_repo
from trading_agent.db.analyses import DbAnalysisStore
from trading_agent.domain.analysis import AnalysisRecord, LlmCall, ValidationIssue
from trading_agent.domain.market import Bar, BarSeries, Instrument, Observation
from trading_agent.domain.proposals import Proposal
from trading_agent.domain.trading import Trade
from trading_agent.settings import Settings

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
            book="baseline_sim",
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
        await trades_repo.replace_book(s, "baseline_sim", [trade, closed])
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
    engine = queries.read_only_engine(settings.database_url)
    try:
        async with AsyncSession(engine) as s:
            trades = await queries.trades(s)
            closes = await queries.open_trade_closes(s)
            rate = await queries.usd_per_eur(s)
            status = await queries.data_status(s)
            analyses = await queries.analyses(s)
            costs = await queries.llm_costs(s)
            proposals = await queries.proposals(s)
    finally:
        await engine.dispose()

    (p,) = proposals.to_dict("records")
    assert (p["symbol"], p["status"], p["label"], p["reason"]) == (
        "AAA",
        "proposed",
        "agree",
        "clean",
    )
    assert p["risk_reward"] == pytest.approx(2.0)

    assert [(t.yahoo_symbol, t.exit_date) for t in trades] == [
        ("AAA", None),
        ("BBB", date(2026, 10, 7)),
    ]
    assert trades[1].pnl_net_eur == pytest.approx(22.5)
    assert closes == {ids["AAA"]: (date(2026, 10, 7), 107.0)}
    assert rate == pytest.approx(1.17)
    assert status.values.tolist() == [
        ["Bars EU", date(2026, 10, 6), "1 symbols, 0 behind"],
        ["Bars US", date(2026, 10, 7), "2 symbols, 1 behind"],
        ["EUR/USD", date(2026, 10, 7), "ECB"],
    ]
    (row,) = analyses.to_dict("records")
    assert (row["symbol"], row["verdict"], row["score"], row["status"]) == ("AAA", "buy", 0.7, "ok")
    assert row["issues"][0]["code"] == "ungrounded_number"
    (call,) = costs.to_dict("records")
    assert (call["role"], call["calls"], call["errors"]) == ("analysis", 1, 0)
    assert call["cost_usd"] == pytest.approx(0.002)


async def test_dashboard_connection_is_read_only(sessions: Sessions, settings: Settings) -> None:
    engine = queries.read_only_engine(settings.database_url)
    try:
        async with AsyncSession(engine) as s:
            with pytest.raises(DBAPIError, match="read-only transaction"):
                await s.execute(text("DELETE FROM trades"))
    finally:
        await engine.dispose()
