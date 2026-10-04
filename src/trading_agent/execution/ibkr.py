"""IBKR broker adapter, read side (M6): account values and positions via IB Gateway.

The connection is opened with `readonly=True`; the gateway also runs with READ_ONLY_API=yes.
Orders arrive in M8.
"""

from ib_async import IB

from trading_agent.domain.broker import AccountSnapshot, BrokerPosition

SUMMARY_TAGS = {
    "NetLiquidation": "net_liquidation",
    "TotalCashValue": "total_cash",
    "SettledCash": "settled_cash",
    "AvailableFunds": "available_funds",
}


def _number(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


class IbkrBroker:
    def __init__(self, ib: IB) -> None:
        self.ib = ib

    async def account(self) -> AccountSnapshot:
        values: dict[str, float | None] = {}
        currency = ""
        for v in await self.ib.accountSummaryAsync():
            field = SUMMARY_TAGS.get(v.tag)
            if field is not None and v.currency not in ("", "BASE"):
                values[field] = _number(v.value)
                currency = v.currency
        return AccountSnapshot(currency=currency, **values)

    async def positions(self) -> list[BrokerPosition]:
        return [
            BrokerPosition(
                conid=p.contract.conId,
                symbol=p.contract.symbol,
                currency=p.contract.currency,
                quantity=float(p.position),
                avg_cost=float(p.avgCost),
            )
            for p in await self.ib.reqPositionsAsync()
            if p.position
        ]
