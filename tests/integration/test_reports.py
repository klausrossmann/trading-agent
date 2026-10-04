"""Weekly report end to end: trades, proposals and labels from the DB; stored report."""

from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from trading_agent import jobs, reports
from trading_agent.data import ingest
from trading_agent.data.ingest import Sessions
from trading_agent.data.universe import Universe
from trading_agent.db import market as market_repo
from trading_agent.db import proposals as proposals_repo
from trading_agent.db import reports as reports_repo
from trading_agent.db import trades as trades_repo
from trading_agent.domain.market import Instrument
from trading_agent.domain.proposals import Proposal
from trading_agent.domain.trading import Trade

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parents[2]
START = date(2026, 10, 5)
WEEK_END = date(2026, 10, 17)


class Inbox:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


async def test_weekly_report_is_stored_and_sent(sessions: Sessions) -> None:
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
    universe = Universe(generated=START, benchmarks={}, instruments=[inst])
    await ingest.sync_universe(sessions, universe)
    async with sessions.begin() as s:
        await s.execute(text("TRUNCATE reports, equity_daily"))
    async with sessions.begin() as s:
        inst_id = next(iter(await market_repo.active_instruments(s)))
        p = Proposal(
            id=uuid4(),
            source="agent",
            as_of=date(2026, 10, 6),
            instrument_id=inst_id,
            yahoo_symbol="AAA",
            market="US",
            sector=None,
            status="proposed",
            strategy="pullback_uptrend",
            confidence=0.7,
            thesis="t",
            invalidation="i",
        )
        await proposals_repo.save_proposals(s, [p])
        await proposals_repo.set_label(s, p.id, "agree")
        shadow = Trade(
            book="agent_shadow",
            strategy="pullback_uptrend",
            instrument_id=inst_id,
            yahoo_symbol="AAA",
            market="US",
            sector=None,
            signal_date=date(2026, 10, 6),
            entry_date=date(2026, 10, 7),
            entry_price=100,
            quantity=3,
            stop=96,
            target=108,
            risk_eur=10.9,
            fees=1.4,
            fees_eur=1.27,
            exit_date=WEEK_END - timedelta(days=2),
            exit_price=108,
            exit_reason="target",
            pnl_net_eur=20.5,
            r_multiple=1.88,
            holding_sessions=6,
        )
        await trades_repo.replace_book(s, "agent_shadow", [shadow])
    ctx = jobs.BookContext.load(ROOT / "config", sessions, universe)
    inbox = Inbox()
    body = await reports.weekly_report(ctx, inbox, WEEK_END)
    assert "| agent_shadow | 1 | 100 % | 1.88 | 20.50 | inf | 20.50 | 1.27 |" in body
    assert "| 0.7-0.8 | 1 | 100 % |" in body
    assert "1 closed trades labelled, 100 % right" in body
    assert "- [ ] 3 months of paper trading: 12 of 91 days" in body
    assert inbox.sent[0].startswith("📈 Weekly report, week ending 2026-10-17")
    async with sessions() as s:
        stored = await reports_repo.reports(s, "weekly")
    assert stored == [(WEEK_END, body)]
