"""IBKR adapter, bars provider, reconciliation and the connection supervisor, with a fake IB."""

from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from ib_async import IB, Contract, Stock
from pydantic import SecretStr

from trading_agent import broker
from trading_agent.data import ibkr
from trading_agent.domain.broker import BrokerPosition
from trading_agent.domain.market import Instrument
from trading_agent.execution.ibkr import IbkrBroker
from trading_agent.execution.reconcile import reconcile
from trading_agent.settings import Settings

US = Instrument(
    symbol="BRK.B",
    yahoo_symbol="BRK-B",
    name="Berkshire",
    market="US",
    exchange="SMART",
    currency="USD",
    kind="stock",
)
EU = Instrument(
    symbol="SAP",
    yahoo_symbol="SAP.DE",
    name="SAP",
    market="EU",
    exchange="IBIS",
    currency="EUR",
    kind="stock",
)


class FakeIB:
    def __init__(self) -> None:
        self.connected = False
        self.connect_ok = False
        self.history: list[Any] = []
        self.requests: list[dict[str, Any]] = []
        self.qualified: list[Any] = []
        self.summary: list[Any] = []
        self.position_list: list[Any] = []

    def isConnected(self) -> bool:
        return self.connected

    async def connectAsync(self, *args: Any, **kwargs: Any) -> None:
        if not self.connect_ok:
            raise ConnectionRefusedError
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    async def reqHistoricalDataAsync(self, contract: Contract, **kwargs: Any) -> list[Any]:
        self.requests.append({"contract": contract, **kwargs})
        return self.history

    async def qualifyContractsAsync(self, *contracts: Contract) -> list[Any]:
        return self.qualified

    async def accountSummaryAsync(self) -> list[Any]:
        return self.summary

    async def reqPositionsAsync(self) -> list[Any]:
        return self.position_list


def _ib(fake: FakeIB) -> IB:
    return cast(IB, fake)


async def test_pacer_allows_a_burst_then_waits_for_the_window() -> None:
    now = [0.0]
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    pacer = ibkr.Pacer(limit=3, window_s=10, clock=lambda: now[0], sleep=sleep)
    for t in (0.0, 1.0, 2.0):
        now[0] = t
        await pacer.wait()
    assert slept == []
    now[0] = 4.0
    await pacer.wait()  # the first request leaves the window at t=10
    assert slept == [6.0]
    now[0] = 30.0
    await pacer.wait()
    assert slept == [6.0]


def test_contracts() -> None:
    us = ibkr.contract_for(US)
    assert isinstance(us, Stock)
    assert (us.symbol, us.exchange, us.currency, us.primaryExchange) == (
        "BRK B",
        "SMART",
        "USD",
        "",
    )
    eu = ibkr.contract_for(EU)
    assert (eu.symbol, eu.currency, eu.primaryExchange) == ("SAP", "EUR", "IBIS")
    known = ibkr.contract_for(EU.model_copy(update={"conid": 14204}))
    assert (known.conId, known.exchange) == (14204, "SMART")


async def test_daily_bars_are_converted_and_cut_to_the_range() -> None:
    fake = FakeIB()
    start = date(2026, 9, 28)
    fake.history = [
        SimpleNamespace(
            date=start + timedelta(days=d),
            open=10,
            high=11,
            low=9.5,
            close=10.123456,
            volume=1500.0,
        )
        for d in range(-1, 6)
    ]
    provider = ibkr.IbkrPriceProvider(_ib(fake))
    series = await provider.daily_bars_async(US, start, start + timedelta(days=3))
    assert [b.date for b in series.bars] == [start + timedelta(days=d) for d in range(4)]
    assert str(series.bars[0].close) == "10.1235"
    assert series.bars[0].volume == 1500
    assert series.source == "ibkr"
    req = fake.requests[0]
    assert (req["durationStr"], req["barSizeSetting"], req["whatToShow"], req["useRTH"]) == (
        "10 D",
        "1 day",
        "TRADES",
        True,
    )
    await provider.daily_bars_async(US, date(2020, 1, 1), date(2026, 1, 1))
    assert fake.requests[1]["durationStr"] == "7 Y"
    fake.history = []
    with pytest.raises(LookupError, match="BRK-B"):
        await provider.daily_bars_async(US, start, start)


async def test_resolve_conids() -> None:
    fake = FakeIB()
    fake.qualified = [Contract(conId=72063691), None]
    found = await ibkr.resolve_conids(_ib(fake), [US, EU])
    assert found == {"BRK-B": 72063691, "SAP.DE": None}


async def test_account_and_positions_without_account_ids() -> None:
    fake = FakeIB()
    fake.summary = [
        SimpleNamespace(account="DU123", tag="NetLiquidation", value="1000000.5", currency="EUR"),
        SimpleNamespace(account="DU123", tag="SettledCash", value="999.0", currency="EUR"),
        SimpleNamespace(account="DU123", tag="AvailableFunds", value="n/a", currency="EUR"),
        SimpleNamespace(account="DU123", tag="Cushion", value="1", currency=""),
    ]
    contract = SimpleNamespace(conId=265598, symbol="AAPL", currency="USD")
    fake.position_list = [
        SimpleNamespace(account="DU123", contract=contract, position=3.0, avgCost=190.5),
        SimpleNamespace(account="DU123", contract=contract, position=0.0, avgCost=0.0),
    ]
    adapter = IbkrBroker(_ib(fake))
    acc = await adapter.account()
    assert (acc.currency, acc.net_liquidation, acc.settled_cash) == ("EUR", 1000000.5, 999.0)
    assert acc.available_funds is None
    assert "DU123" not in acc.model_dump_json()
    assert await adapter.positions() == [
        BrokerPosition(conid=265598, symbol="AAPL", currency="USD", quantity=3, avg_cost=190.5)
    ]


def test_reconcile() -> None:
    held = [
        BrokerPosition(conid=1, symbol="AAA", currency="USD", quantity=5, avg_cost=1),
        BrokerPosition(conid=2, symbol="BBB", currency="USD", quantity=3, avg_cost=1),
    ]
    report = reconcile(held, {2: ("BBB", 4), 3: ("CCC", 1)})
    assert [p.symbol for p in report.unknown] == ["AAA"]
    assert [(m.symbol, m.expected, m.actual) for m in report.missing] == [("CCC", 1, 0)]
    assert [(m.symbol, m.expected, m.actual) for m in report.quantity] == [("BBB", 4, 3)]
    assert not report.clean
    assert reconcile(held[:1], {1: ("AAA", 5)}).clean


class Inbox:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


async def test_link_alerts_once_after_ten_minutes_and_on_recovery() -> None:
    fake, inbox, now = FakeIB(), Inbox(), [0.0]
    connected: list[str] = []

    async def on_connect(link: broker.BrokerLink) -> None:
        connected.append(link.status())

    link = broker.BrokerLink(
        Settings(db_password=SecretStr("x")),
        inbox,
        ib=_ib(fake),
        on_connect=on_connect,
        clock=lambda: now[0],
    )
    assert await link.step() == 30
    assert link.status() == "unreachable for 0 min"
    for t in (300.0, 599.0):
        now[0] = t
        await link.step()
    assert inbox.sent == []
    now[0] = 660.0
    assert await link.step() == 300  # backoff capped
    now[0] = 900.0
    await link.step()
    assert inbox.sent == [broker.messages.gateway_down_alert(11)]

    fake.connect_ok = True
    now[0] = 1260.0
    assert await link.step() == 30
    assert inbox.sent[-1] == "🔌 IB Gateway connected again after 21 min"
    assert connected == ["connected (ib-gateway:4004)"]
    assert await link.step() == 30  # stays connected, no new messages
    assert len(inbox.sent) == 2


async def test_reconcile_job_skips_without_a_connection() -> None:
    assert await broker.reconcile_positions(None, cast(Any, None), Inbox()) is None
