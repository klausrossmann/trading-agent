from datetime import date

import pytest

from trading_agent.data import ingest
from trading_agent.data.ingest import Sessions
from trading_agent.data.universe import Universe
from trading_agent.db import market as repo
from trading_agent.db import trades as trades_repo
from trading_agent.domain.market import Instrument
from trading_agent.domain.trading import Trade

pytestmark = pytest.mark.db


async def test_replace_book_is_idempotent(sessions: Sessions) -> None:
    inst = Instrument(
        symbol="AAA",
        yahoo_symbol="AAA",
        name="AAA",
        market="US",
        exchange="SMART",
        currency="USD",
        kind="stock",
        indices=("SP100",),
    )
    await ingest.sync_universe(
        sessions, Universe(generated=date(2026, 10, 3), benchmarks={}, instruments=[inst])
    )
    async with sessions() as s:
        (inst_id,) = (await repo.active_instruments(s)).keys()
    trade = Trade(
        book="baseline_sim",
        strategy="pullback_uptrend",
        instrument_id=inst_id,
        yahoo_symbol="AAA",
        market="US",
        sector=None,
        signal_date=date(2026, 10, 1),
        entry_date=date(2026, 10, 2),
        entry_price=50.025,
        quantity=5,
        stop=48.1,
        target=54.1,
        risk_eur=10,
        fees=0.74,
        fees_eur=0.68,
        exit_date=date(2026, 10, 5),
        exit_price=54.07296,
        exit_reason="target",
        pnl_net_eur=18.5123,
        r_multiple=1.85123,
        holding_sessions=1,
    )
    open_trade = trade.model_copy(
        update={k: None for k in ("exit_date", "exit_price", "exit_reason", "pnl_net_eur")}
    )
    for _ in range(2):
        async with sessions.begin() as s:
            await trades_repo.replace_book(s, "baseline_sim", [trade, open_trade])
    async with sessions() as s:
        rows = await trades_repo.book_trades(s, "baseline_sim")
    assert len(rows) == 2
    assert str(rows[0].pnl_net_eur) in {"18.51", "None"}
    assert {str(r.exit_price) for r in rows} == {"54.0730", "None"}
