"""Book accounting from brackets and fills, and the market facts for the risk engine."""

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from trading_agent.domain.market import Bar, Instrument
from trading_agent.domain.orders import Bracket
from trading_agent.domain.risk import Holding
from trading_agent.portfolio import book as acc
from trading_agent.portfolio.snapshot import correlations, snapshot, to_tick

D = Decimal
USD = D("1.10")
AAPL = Instrument(
    id=1,
    symbol="AAPL",
    yahoo_symbol="AAPL",
    name="Apple",
    market="US",
    exchange="SMART",
    currency="USD",
    kind="stock",
    sector="Technology",
    indices=("SP100",),
)
MSFT = AAPL.model_copy(update={"id": 2, "symbol": "MSFT", "yahoo_symbol": "MSFT"})
INSTRUMENTS = {1: AAPL, 2: MSFT}
TODAY = date(2026, 10, 7)  # Wednesday


def bracket(**kw: object) -> Bracket:
    values: dict[str, object] = {
        "id": uuid4(),
        "book": "agent_paper",
        "instrument_id": 1,
        "state": "filled",
        "quantity": 3,
        "entry": D(100),
        "stop": D(96),
        "initial_stop": D(96),
        "target": D(108),
        "expires": datetime(2026, 10, 6, 20, tzinfo=UTC),
        "filled_qty": 3,
        "entry_price": D(100),
    }
    return Bracket.model_validate(values | kw)


def fill(b: Bracket, kind: str, price: str, day: date, settles: date | None = None) -> acc.BookFill:
    return acc.BookFill(
        bracket_id=b.id,
        kind=kind,  # pyright: ignore[reportArgumentType]
        day=day,
        quantity=3,
        price=D(price),
        fee=D("0.35"),
        eur_rate=USD,
        settles=settles or day,
    )


def test_open_position_and_pending_entry() -> None:
    held = bracket()
    pending = bracket(instrument_id=2, state="working", filled_qty=0, entry_price=None, entry=D(50))
    fills = [fill(held, "entry", "100", date(2026, 10, 5))]
    a = acc.account(D(1000), [held, pending], fills, INSTRUMENTS, {1: D(104)}, {"USD": USD}, TODAY)
    assert a.cash_eur == (D(1000) - D("300.35") / USD).quantize(D("0.01")) == D("726.95")
    assert a.invested_eur == D("283.64")  # 3 x 104 / 1.10
    assert a.reserved_eur == D("136.36")  # 3 x 50 / 1.10
    assert a.unsettled_eur == 0
    assert a.equity_eur == D("1010.59")
    assert a.available_eur == D("590.59")
    assert a.holdings == (
        Holding(instrument_id=1, sector="Technology", value_eur=D("283.64")),
        Holding(instrument_id=2, sector="Technology", value_eur=D("136.36")),
    )


def test_sale_proceeds_settle_later() -> None:
    closed = bracket(state="closed", exit_qty=3, exit_price=D(108), exit_reason="target")
    fills = [
        fill(closed, "entry", "100", date(2026, 10, 1)),
        fill(closed, "target", "108", TODAY, settles=TODAY + timedelta(days=1)),
    ]
    a = acc.account(D(1000), [closed], fills, INSTRUMENTS, {}, {"USD": USD}, TODAY)
    assert a.unsettled_eur == D("294.23")  # (324 - 0.35) / 1.10
    assert a.holdings == ()
    later = acc.account(
        D(1000), [closed], fills, INSTRUMENTS, {}, {"USD": USD}, TODAY + timedelta(1)
    )
    assert later.unsettled_eur == 0
    assert later.cash_eur == a.cash_eur + a.unsettled_eur


def test_portfolio_state_windows() -> None:
    a = acc.Account(D(900), D(0), D(0), D(80), ())
    history = [
        (date(2026, 10, 2), D(1050)),  # Friday: last close before the week
        (date(2026, 10, 5), D(1020)),
        (date(2026, 10, 6), D(1000)),
    ]
    s = acc.portfolio_state(a, D(1000), history, TODAY, orders_today=2)
    assert (s.pnl_today_eur, s.pnl_week_eur, s.drawdown_eur) == (D(-20), D(-70), D(70))
    assert (s.settled_cash_eur, s.orders_today) == (D(900), 2)
    fresh = acc.portfolio_state(a, D(1000), [], TODAY, orders_today=0)
    assert (fresh.pnl_today_eur, fresh.pnl_week_eur, fresh.drawdown_eur) == (D(-20), D(-20), D(20))


def test_trades_from_brackets() -> None:
    closed = bracket(state="closed", exit_qty=3, exit_price=D(108), exit_reason="target")
    open_ = bracket(instrument_id=2)
    unfilled = bracket(state="expired", filled_qty=0, entry_price=None)
    fills = [
        fill(closed, "entry", "100", date(2026, 10, 1)),
        fill(closed, "target", "108", date(2026, 10, 6)),
        fill(open_, "entry", "100", date(2026, 10, 6)),
    ]
    signals = {b.id: (date(2026, 9, 30), "pullback_uptrend") for b in (closed, open_, unfilled)}
    rows = acc.trades(
        "agent_paper",
        [closed, open_, unfilled],
        fills,
        INSTRUMENTS,
        signals,
        lambda market, start, end: (end - start).days,
    )
    assert len(rows) == 2
    t = rows[0]
    assert (t.entry_date, t.exit_date, t.exit_reason, t.holding_sessions) == (
        date(2026, 10, 1),
        date(2026, 10, 6),
        "target",
        5,
    )
    assert t.pnl_net_eur == pytest.approx((324 - 0.35 - 300 - 0.35) / 1.1)
    assert t.risk_eur == pytest.approx(12 / 1.1)
    assert t.pnl_net_eur is not None
    assert t.r_multiple == pytest.approx(t.pnl_net_eur / t.risk_eur)
    assert (t.fees, t.fees_eur) == (pytest.approx(0.7), pytest.approx(0.7 / 1.1))
    o = rows[1]
    assert (o.yahoo_symbol, o.exit_date, o.pnl_net_eur, o.holding_sessions) == (
        "MSFT",
        None,
        None,
        None,
    )


def _bars(closes: Sequence[float], start: date = date(2026, 6, 1)) -> list[Bar]:
    return [
        Bar(
            date=start + timedelta(days=i),
            open=D(str(c)),
            high=D(str(round(c * 1.01, 2))),
            low=D(str(round(c * 0.99, 2))),
            close=D(str(c)),
            volume=1_000_000,
        )
        for i, c in enumerate(closes)
    ]


def test_tick_rounding_never_adds_risk() -> None:
    assert to_tick(D("100.019"), up=False) == D("100.01")
    assert to_tick(D("96.001"), up=True) == D("96.01")
    xetra = ((D(0), D("0.001")), (D(10), D("0.005")), (D(50), D("0.01")), (D(100), D("0.02")))
    assert to_tick(D("123.457"), False, xetra) == D("123.44")
    assert to_tick(D("123.457"), True, xetra) == D("123.46")
    assert to_tick(D("12.3456"), True, xetra) == D("12.350")
    assert to_tick(D("5.12345"), False, xetra) == D("5.123")


def test_snapshot_and_correlations() -> None:
    up = [100 + i + (i % 3) for i in range(80)]
    same = [50 + i / 2 + (i % 3) / 2 for i in range(80)]
    opposite = [200 - i - (i % 3) for i in range(80)]
    bars = _bars(up)
    rho = correlations(bars, {2: _bars(same), 3: _bars(opposite), 4: _bars(up[:10])})
    assert rho[2] == pytest.approx(1.0)
    assert rho[3] < 0
    assert 4 not in rho  # too short to estimate
    assert correlations(bars, {5: _bars([10.0] * 80)}) == {}  # flat: no estimate

    open_ = datetime(2026, 10, 7, 13, 30, tzinfo=UTC)
    s = snapshot(
        AAPL,
        bars,
        {1: bars, 2: _bars(same)},
        session_open=open_,
        session_close=open_ + timedelta(hours=6, minutes=30),
        sessions_to_earnings=None,
        eur_rate=USD,
    )
    assert s.mid == D(str(up[-1]))
    assert s.atr > 0
    assert s.avg_daily_value == D(str(round(sum(up[-20:]) / 20 * 1_000_000)))
    assert set(s.correlations) == {2}  # not with itself
    assert s.in_universe
    with pytest.raises(ValueError, match="no id or no bars"):
        snapshot(
            AAPL,
            [],
            {},
            session_open=None,
            session_close=None,
            sessions_to_earnings=None,
            eur_rate=USD,
        )
