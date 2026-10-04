from datetime import date
from types import SimpleNamespace
from typing import Any, cast

import pytest
from ib_async import Contract

from trading_agent import broker
from trading_agent.data import ingest
from trading_agent.data.ingest import Sessions
from trading_agent.data.universe import Universe
from trading_agent.db import market as repo
from trading_agent.domain.broker import BrokerPosition
from trading_agent.domain.market import Instrument

pytestmark = pytest.mark.db


def _inst(symbol: str) -> Instrument:
    return Instrument(
        symbol=symbol,
        yahoo_symbol=symbol,
        name=symbol,
        market="US",
        exchange="SMART",
        currency="USD",
        kind="stock",
        indices=("SP100",),
    )


class Inbox:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


async def test_conids_are_stored_and_positions_reconciled(sessions: Sessions) -> None:
    universe = Universe(
        generated=date(2026, 10, 5), benchmarks={}, instruments=[_inst("AAA"), _inst("BBB")]
    )
    await ingest.sync_universe(sessions, universe)

    async def qualify(*contracts: Contract) -> list[Any]:
        return [Contract(conId=101), None]

    held = [BrokerPosition(conid=999, symbol="ZZZ", currency="USD", quantity=1, avg_cost=5)]

    async def positions() -> list[BrokerPosition]:
        return held

    link = cast(
        broker.BrokerLink,
        SimpleNamespace(
            connected=True,
            ib=SimpleNamespace(qualifyContractsAsync=qualify),
            broker=SimpleNamespace(positions=positions),
        ),
    )
    assert await broker.sync_conids(link, sessions) == ["BBB"]
    async with sessions() as s:
        instruments = await repo.active_instruments(s)
    ids = {i.yahoo_symbol: k for k, i in instruments.items()}
    assert instruments[ids["AAA"]].conid == 101

    async def expected() -> dict[int, tuple[str, float]]:
        return {101: ("AAA", 4.0)}

    inbox = Inbox()
    halts: list[str] = []

    async def halt(reason: str) -> None:
        halts.append(reason)

    report = await broker.reconcile_positions(link, inbox, expected, halt)
    assert report is not None
    assert [p.symbol for p in report.unknown] == ["ZZZ"]
    assert [m.symbol for m in report.missing] == ["AAA"]
    assert inbox.sent[0].splitlines() == [
        "⚠️ Reconciliation: broker and book differ",
        "At the broker only: ZZZ 1",
        "In the book only: AAA 4",
    ]
    assert halts == ["reconciliation mismatch"]
    # orders still simulated: the paper account must hold nothing
    report = await broker.reconcile_positions(link, inbox)
    assert report is not None
    assert [p.symbol for p in report.unknown] == ["ZZZ"]
    assert report.missing == []
