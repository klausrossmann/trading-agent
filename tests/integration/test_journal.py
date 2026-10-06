from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from trading_agent import jobs, journal
from trading_agent.data import ingest
from trading_agent.data.ingest import Sessions
from trading_agent.data.universe import Universe
from trading_agent.db import book as book_repo
from trading_agent.db import market as market_repo
from trading_agent.db import proposals as repo
from trading_agent.db import trades as trades_repo
from trading_agent.db.analyses import DbAnalysisStore
from trading_agent.domain.analysis import AnalysisRecord
from trading_agent.domain.market import Instrument
from trading_agent.domain.proposals import Proposal
from trading_agent.domain.risk import Check, RiskDecision
from trading_agent.domain.trading import Trade
from trading_agent.notify import messages

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parents[2]
DAY = date(2026, 10, 5)


def _inst(symbol: str) -> Instrument:
    return Instrument(
        symbol=symbol,
        yahoo_symbol=symbol,
        name=symbol,
        market="US",
        exchange="SMART",
        currency="USD",
        kind="stock",
        indices=("SP100",) if symbol != "SPY" else (),
    )


def _proposal(inst_id: int, symbol: str, status: str, rank: int | None, as_of: date) -> Proposal:
    plan = {"entry": 100.0, "stop": 96.0, "target": 108.0} if status != "no_trade" else {}
    return Proposal.model_validate(
        {
            "id": uuid4(),
            "source": "agent",
            "as_of": as_of,
            "instrument_id": inst_id,
            "yahoo_symbol": symbol,
            "market": "US",
            "sector": None,
            "status": status,
            "strategy": "pullback_uptrend",
            "rank": rank,
            "thesis": f"{symbol} thesis.",
            "invalidation": "Below the stop.",
            **plan,
        }
    )


async def _seed(sessions: Sessions) -> tuple[Universe, dict[str, int]]:
    universe = Universe(
        generated=DAY,
        benchmarks={"US": "SPY"},
        instruments=[_inst("SPY"), _inst("AAA"), _inst("BBB")],
    )
    await ingest.sync_universe(sessions, universe)
    async with sessions.begin() as s:
        ids = {i.yahoo_symbol: k for k, i in (await market_repo.active_instruments(s)).items()}
        await repo.save_proposals(
            s,
            [
                _proposal(ids["AAA"], "AAA", "proposed", 1, date(2026, 10, 2)),  # older scan
                _proposal(ids["AAA"], "AAA", "proposed", 1, DAY),
                _proposal(ids["BBB"], "BBB", "no_trade", None, DAY),
            ],
        )
    return universe, ids


async def test_review_steps_through_the_latest_proposals(sessions: Sessions) -> None:
    await _seed(sessions)
    review = journal.Review(sessions)
    card = await review.start([])
    assert card.text.startswith("2 to label\n\n🧠 AAA, as of Mon 5 Oct: proposed, rank 1")
    (agree, disagree), (_skip,) = card.buttons
    assert agree[1].endswith(":a")

    card = await review.on_button(disagree[1])
    assert card.text.startswith(
        "Saved AAA: disagree. Reply with a reason if you like.\n\n1 to label"
    )
    assert "BBB" in card.text
    assert await review.on_text("  Too extended. ") == "Reason saved for AAA."
    assert await review.on_text("chatter") is None

    skip_bbb = card.buttons[1][0][1]
    assert (await review.on_button(skip_bbb)).text == "Nothing left to label."
    assert (await review.on_button("l:not-a-uuid:a")).text == "Unknown button."
    assert (await review.on_button(f"l:{uuid4()}:a")).text == "That proposal no longer exists."
    assert (await review.start([])).text.startswith("1 to label")  # skips reset

    cmds = journal.commands(sessions, review)
    listing = str(await cmds["proposals"].run([]))
    assert listing.splitlines()[:3] == [
        "🧠 Proposals, as of Mon 5 Oct",
        "1. AAA 100.00, stop 96.00, target 108.00, R:R 2.0, no critique 👎",
        "Passed: BBB",
    ]
    why = str(await cmds["why"].run(["aaa"]))
    assert why.endswith("Your label: disagree (Too extended.)")
    assert await cmds["why"].run(["ZZZ"]) == "No proposal for ZZZ."


async def test_why_links_the_trace_of_the_proposer(sessions: Sessions) -> None:
    _, ids = await _seed(sessions)
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    analysis_id = await DbAnalysisStore(sessions).save(
        AnalysisRecord(
            module="proposer",
            prompt_version=2,
            model="google:m",
            instrument_id=ids["BBB"],
            as_of=DAY,
            input_hash="h-bbb",
            input={},
            output={},
            issues=(),
            status="ok",
            cost_usd=0.001,
            trace_id=trace_id,
        ),
        [],
    )
    assert analysis_id is not None
    async with sessions.begin() as s:
        p = _proposal(ids["BBB"], "BBB", "no_trade", None, DAY)
        await repo.save_proposals(s, [p.model_copy(update={"analyses": {"proposer": analysis_id}})])
    review = journal.Review(sessions)
    url = "https://cloud.langfuse.com/project/p1"
    why = str(await journal.commands(sessions, review, url)["why"].run(["BBB"]))
    assert why.endswith(f"\nTrace: {url}/traces/{trace_id}")
    plain = str(await journal.commands(sessions, review)["why"].run(["BBB"]))
    assert "Trace:" not in plain
    no_trace = str(await journal.commands(sessions, review, url)["why"].run(["AAA"]))
    assert "Trace:" not in no_trace


async def test_digest_lists_proposals_and_book_changes(sessions: Sessions) -> None:
    universe, ids = await _seed(sessions)
    trade = Trade(
        book="agent_shadow",
        strategy="pullback_uptrend",
        instrument_id=ids["AAA"],
        yahoo_symbol="AAA",
        market="US",
        sector=None,
        signal_date=date(2026, 9, 30),
        entry_date=date(2026, 10, 1),
        entry_price=100,
        quantity=2,
        stop=96,
        target=108,
        risk_eur=7,
        fees=2,
        fees_eur=1.7,
        exit_date=DAY,
        exit_price=108,
        exit_reason="target",
        pnl_net_eur=12.3,
        r_multiple=1.76,
        holding_sessions=2,
    )
    async with sessions.begin() as s:
        await trades_repo.replace_book(s, "agent_shadow", [trade])
        aaa = next(p for p in await repo.proposals(s, start=DAY) if p.yahoo_symbol == "AAA")
        rejected = RiskDecision(
            approved=False,
            checks=(
                Check(name="portfolio", outcome="fail", detail="4 positions and entries open"),
            ),
        )
        await book_repo.save_decision(s, aaa.id, rejected, datetime(2026, 10, 5, 14, tzinfo=UTC))
    book = jobs.BookContext.load(ROOT / "config", sessions, universe)
    text = messages.render_digest(await journal.build_digest(book, DAY))
    assert text.splitlines() == [
        "🌙 Evening digest Mon 5 Oct",
        "Proposals (Mon 5 Oct): 1 no trade, 1 proposed",
        "  2 to label: /review",
        "Risk engine: placed none",
        "  rejected AAA: portfolio: 4 positions and entries open",
        "agent_paper: 0 open",
        "agent_shadow: 0 open; closed AAA +€12.30 (+0.1 %) (target)",
        "baseline_sim: 0 open",
        "LLM today: $0.0000",
    ]
