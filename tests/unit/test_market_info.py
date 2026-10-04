"""IBKR price increments (market rules) and quotes, with a fake IB."""

import math
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

from ib_async import IB

from trading_agent.data.ibkr import MarketInfo
from trading_agent.domain.market import Instrument

D = Decimal
SAP = Instrument(
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
        self.connected = True
        self.details: list[Any] = [
            SimpleNamespace(validExchanges="SMART,IBIS,TGATE", marketRuleIds="26,2042,26")
        ]
        self.rules = {
            2042: [
                SimpleNamespace(lowEdge=0.0, increment=0.001),
                SimpleNamespace(lowEdge=100.0, increment=0.02),
            ]
        }
        self.tickers: list[Any] = []
        self.rule_requests: list[int] = []
        self.fail: Exception | None = None

    def isConnected(self) -> bool:
        return self.connected

    async def reqContractDetailsAsync(self, contract: Any) -> list[Any]:
        if self.fail:
            raise self.fail
        return self.details

    async def reqMarketRuleAsync(self, rule: int) -> list[Any] | None:
        self.rule_requests.append(rule)
        return self.rules.get(rule)

    async def reqTickersAsync(self, *contracts: Any) -> list[Any]:
        if self.fail:
            raise self.fail
        return self.tickers


def info(fake: FakeIB) -> MarketInfo:
    return MarketInfo(cast(IB, fake))


async def test_increments_use_the_primary_exchange_rule_and_are_cached() -> None:
    fake = FakeIB()
    m = info(fake)
    expected = ((D("0.0"), D("0.001")), (D("100.0"), D("0.02")))
    assert await m.increments(SAP) == expected
    fake.connected = False
    assert await m.increments(SAP) == expected  # cached
    assert fake.rule_requests == [2042]
    us = SAP.model_copy(update={"yahoo_symbol": "X", "market": "US", "exchange": "SMART"})
    fake.connected = True
    assert await m.increments(us) is None  # rule 26 unknown -> empty table


async def test_increments_fall_back_to_none() -> None:
    fake = FakeIB()
    fake.connected = False
    assert await info(fake).increments(SAP) is None
    fake.connected, fake.details = True, []
    assert await info(fake).increments(SAP) is None
    fake.details = [SimpleNamespace(validExchanges="SMART", marketRuleIds="2042")]
    assert await info(fake).increments(SAP) is not None  # venue missing: first rule
    fake.fail = TimeoutError()
    assert await info(fake).increments(SAP) is None


async def test_mid_from_bid_ask_or_last() -> None:
    fake = FakeIB()
    m = info(fake)
    fake.tickers = [SimpleNamespace(bid=99.9, ask=100.1, last=100.5)]
    assert await m.mid(SAP) == D("100.0")
    fake.tickers = [SimpleNamespace(bid=math.nan, ask=100.1, last=100.5)]
    assert await m.mid(SAP) == D("100.5")
    fake.tickers = [SimpleNamespace(bid=101.0, ask=100.0, last=math.nan)]  # crossed, no last
    assert await m.mid(SAP) is None
    fake.tickers = []
    assert await m.mid(SAP) is None
    fake.fail = ConnectionError()
    assert await m.mid(SAP) is None
    fake.connected = False
    assert await m.mid(SAP) is None
