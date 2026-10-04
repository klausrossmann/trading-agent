"""IBKR broker adapter via IB Gateway: account and positions (M6), orders (M8).

Orders need a connection without `readonly` and the gateway with READ_ONLY_API=no.
"""

from collections.abc import Sequence
from datetime import UTC
from decimal import Decimal

from ib_async import IB, Order, Trade

from trading_agent.data.ibkr import contract_for
from trading_agent.domain.broker import AccountSnapshot, BrokerPosition
from trading_agent.domain.orders import BrokerFill, BrokerOrder, BrokerStatus, OrderSpec

SUMMARY_TAGS = {
    "NetLiquidation": "net_liquidation",
    "TotalCashValue": "total_cash",
    "SettledCash": "settled_cash",
    "AvailableFunds": "available_funds",
}
STATUS: dict[str, BrokerStatus] = {
    "PendingSubmit": "pending",
    "ApiPending": "pending",
    "PreSubmitted": "working",  # also children waiting for their parent
    "Submitted": "working",
    "ApiUpdate": "working",
    "PendingCancel": "working",
    "Filled": "filled",
    "Cancelled": "inactive",
    "ApiCancelled": "inactive",
    "Inactive": "inactive",
}


def _number(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def _decimal(value: float) -> Decimal:
    return Decimal(str(round(value, 4)))


def _commission(value: float) -> Decimal | None:
    """None until IBKR sends the commission report (it reports a huge sentinel before)."""
    return _decimal(value) if 0 < value < 1e9 else None


def ib_order(spec: OrderSpec, order_id: int, parent_id: int, transmit: bool) -> Order:
    order = Order(
        orderId=order_id,
        action=spec.action,
        totalQuantity=spec.quantity,
        orderType=spec.order_type,
        tif=spec.tif,
        orderRef=spec.order_ref,
        outsideRth=False,
        parentId=parent_id,
        transmit=transmit,
    )
    if spec.limit_price is not None:
        order.lmtPrice = float(spec.limit_price)
    if spec.stop_price is not None:
        order.auxPrice = float(spec.stop_price)
    if spec.good_till is not None:
        order.goodTillDate = f"{spec.good_till.astimezone(UTC):%Y%m%d-%H:%M:%S}"  # UTC format
    if spec.oca_group is not None:
        order.ocaGroup, order.ocaType = spec.oca_group, 1  # cancel the others on a fill
    return order


def _view(trade: Trade) -> BrokerOrder:
    s = trade.orderStatus
    return BrokerOrder(
        order_ref=trade.order.orderRef,
        status=STATUS.get(s.status, "pending"),
        filled=int(s.filled),
        avg_fill_price=_decimal(s.avgFillPrice) if s.filled else None,
        broker_order_id=trade.order.orderId or None,
        perm_id=trade.order.permId or None,
    )


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

    # --- orders (M8) ---

    async def _trades(self) -> dict[str, Trade]:
        """Our orders by order_ref: completed ones first, so open ones win."""
        trades = await self.ib.reqCompletedOrdersAsync(apiOnly=True)
        return {t.order.orderRef: t for t in [*trades, *self.ib.openTrades()] if t.order.orderRef}

    async def place(self, specs: Sequence[OrderSpec]) -> list[BrokerOrder]:
        """Skips any order_ref the broker knows from open or completed orders or executions."""
        known = await self._trades()
        executed = {f.execution.orderRef for f in await self.ib.reqExecutionsAsync()}
        ids: dict[str, int] = {}
        out: list[BrokerOrder] = []
        for i, spec in enumerate(specs):
            trade = known.get(spec.order_ref)
            if trade is not None:
                ids[spec.order_ref] = trade.order.orderId
                out.append(_view(trade))
                continue
            if spec.order_ref in executed:
                out.append(BrokerOrder(order_ref=spec.order_ref, status="filled"))
                continue
            order_id = self.ib.client.getReqId()
            ids[spec.order_ref] = order_id
            parent = ids.get(spec.parent_ref, 0) if spec.parent_ref else 0
            order = ib_order(spec, order_id, parent, transmit=i == len(specs) - 1)
            out.append(_view(self.ib.placeOrder(contract_for(spec.instrument), order)))
        return out

    async def cancel(self, order_ref: str) -> None:
        for trade in self.ib.openTrades():
            if trade.order.orderRef == order_ref:
                self.ib.cancelOrder(trade.order)

    async def modify(self, spec: OrderSpec) -> None:
        for trade in self.ib.openTrades():
            if trade.order.orderRef == spec.order_ref:
                if spec.limit_price is not None:
                    trade.order.lmtPrice = float(spec.limit_price)
                if spec.stop_price is not None:
                    trade.order.auxPrice = float(spec.stop_price)
                trade.order.transmit = True
                self.ib.placeOrder(trade.contract, trade.order)

    async def orders(self) -> list[BrokerOrder]:
        return [_view(t) for t in (await self._trades()).values()]

    async def fills(self) -> list[BrokerFill]:
        return [
            BrokerFill(
                exec_id=f.execution.execId,
                order_ref=f.execution.orderRef,
                ts=f.time,
                quantity=int(f.execution.shares),
                price=_decimal(f.execution.price),
                commission=_commission(f.commissionReport.commission),
            )
            for f in await self.ib.reqExecutionsAsync()
            if f.execution.orderRef
        ]
