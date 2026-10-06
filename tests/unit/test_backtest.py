"""Backtest engine on hand-made price paths (no network, no database)."""

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import pandas as pd
import pytest

from trading_agent import backtest as bt
from trading_agent.domain.market import Instrument
from trading_agent.domain.proposals import Proposal
from trading_agent.risk.config import RiskConfig, load_risk_config
from trading_agent.settings import load_fees

ROOT = Path(__file__).resolve().parents[2]
CFG = bt.BacktestConfig(baseline_book=bt.BaselineBook(start=date(2026, 1, 1)))
RISK_REPO = load_risk_config(ROOT / "config")
# The hand-made paths expect fixed-size limits on a EUR 1,000 US account.
RISK = RISK_REPO.model_copy(
    update={
        "capital": RISK_REPO.capital.model_copy(
            update={"paper_budget_eur": {"US": Decimal(1000), "EU": Decimal(5000)}}
        ),
        "per_trade": RISK_REPO.per_trade.model_copy(
            update={
                "min_risk_pct": Decimal("1.5"),
                "max_risk_pct": Decimal("1.5"),
                "min_position_pct": Decimal(30),
                "max_position_pct": Decimal(30),
                "min_position_eur": Decimal(200),
            }
        ),
        "portfolio": RISK_REPO.portfolio.model_copy(update={"max_open_positions": 4}),
        "execution": RISK_REPO.execution.model_copy(update={"max_orders_per_day": 6}),
    }
)
FEES = load_fees(ROOT / "config")
DAYS = pd.bdate_range("2026-02-02", periods=45)  # row % 5 is the weekday, 0 = Monday
SIGNAL = 19  # setup on bar 19 -> limit 50.10, stop 48.10 (2 x ATR 1), target 54.10


def risk_with(section: str, **values: object) -> RiskConfig:
    part = getattr(RISK, section)
    return RISK.model_copy(update={section: part.model_copy(update=values)})


# Flat test prices have no correlation estimate, so every holding would count as correlated.
NO_CLUSTER = risk_with("portfolio", max_correlated_cluster_pct=Decimal(100))


def instrument(n: int, sector: str = "Tech") -> bt.InstrumentData:
    inst = Instrument(
        id=n,
        symbol=f"S{n}",
        yahoo_symbol=f"S{n}",
        name=f"S{n}",
        market="US",
        exchange="SMART",
        currency="USD",
        kind="stock",
        sector=sector,
        indices=("SP100",),
    )
    frame = pd.DataFrame(
        {"open": 50.0, "high": 50.5, "low": 49.5, "close": 50.0, "volume": 1e6}, index=DAYS
    )
    ind = pd.DataFrame({"atr": 1.0, "rs": float(n)}, index=DAYS)
    setup = pd.Series(False, index=DAYS)
    setup.iloc[SIGNAL] = True
    return bt.InstrumentData(
        id=n,
        instrument=inst,
        frame=frame,
        ind=ind,
        setup=setup,
        earnings=[],
        rows={d.date(): i for i, d in enumerate(DAYS)},
    )


def set_bar(x: bt.InstrumentData, row: int, o: float, h: float, lo: float, c: float) -> None:
    x.frame.iloc[row, :4] = [o, h, lo, c]
    x.returns = x.frame["close"].pct_change().iloc[1:]


def signal_on(x: bt.InstrumentData, row: int) -> bt.InstrumentData:
    x.setup[:] = False
    x.setup.iloc[row] = True
    return x


def run(
    *xs: bt.InstrumentData, risk: RiskConfig = RISK, cfg: bt.BacktestConfig = CFG, **data: Any
) -> bt.BacktestResult:
    market = bt.MarketData(
        {x.id: x for x in xs}, pd.Series([1.0], index=DAYS[:1]), benchmarks={}, **data
    )
    return bt.run(
        market, cfg, risk, FEES, start=DAYS[0].date(), end=DAYS[-1].date(), markets=["US"]
    )


def only_trade(result: bt.BacktestResult) -> bt.Trade:
    assert len(result.trades) == 1
    return result.trades[0]


def assert_books_balance(result: bt.BacktestResult) -> None:
    pnl = sum(t.pnl_net_eur or 0 for t in result.trades)
    assert float(result.equity.iloc[-1]) == pytest.approx(1000 + pnl, abs=1e-6)


def test_target_hit() -> None:
    x = instrument(1)
    set_bar(x, 21, 52, 55, 51.5, 54)
    t = only_trade(result := run(x))
    assert (t.entry_date, t.exit_date) == (DAYS[20].date(), DAYS[21].date())
    assert t.entry_price == pytest.approx(50 * 1.0005)  # opened below the 50.10 limit
    # risk cap 15 / 2.00 = 7.5 shares, position cap 300 / 50.10 = 5.988 shares
    assert t.quantity == pytest.approx(5.988)
    assert t.exit_reason == "target"
    assert t.exit_price == pytest.approx(54.10 * 0.9995)
    assert t.risk_eur == pytest.approx(5.988 * 2.0)
    assert t.r_multiple == pytest.approx((t.pnl_net_eur or 0) / t.risk_eur)
    assert 1.8 < (t.r_multiple or 0) < 2.1
    assert_books_balance(result)


def test_stop_hit() -> None:
    x = instrument(1)
    set_bar(x, 21, 49, 49.5, 47, 47.5)
    t = only_trade(result := run(x))
    assert t.exit_reason == "stop"
    assert t.exit_price == pytest.approx(48.10 * 0.9995)
    assert -1.3 < (t.r_multiple or 0) < -0.9
    assert_books_balance(result)


def test_breakeven_then_stopped_flat() -> None:
    x = instrument(1)
    set_bar(x, 21, 51, 52.1, 50.5, 52)  # +1R reached -> stop to the entry price
    set_bar(x, 22, 51, 51.2, 49.9, 50)
    t = only_trade(run(x))
    assert t.exit_reason == "stop"
    assert t.exit_price == pytest.approx(50 * 1.0005 * 0.9995)


def test_time_stop_after_15_sessions() -> None:
    t = only_trade(run(instrument(1)))
    assert t.exit_reason == "time"
    assert t.holding_sessions == 15
    assert t.exit_date == DAYS[35].date()


def test_unfilled_limit_expires() -> None:
    x = instrument(1)
    for row in (20, 21):
        set_bar(x, row, 51, 52, 50.5, 51)  # never trades down to 50.10
    result = run(x)
    assert result.trades == []
    assert float(result.equity.iloc[-1]) == 1000


def test_external_signals_replace_the_baseline_setups() -> None:
    a, b, c = instrument(1, "A"), instrument(2, "B"), instrument(3, "C")
    set_bar(b, 25, 50, 50.5, 49.5, 50)
    plan = bt.TradePlan(entry=50.10, stop=48.10, target=54.10)
    external = {
        DAYS[22].date(): [
            bt.Signal(2, plan, score=-2, strategy="agent"),
            bt.Signal(3, plan, score=-1, strategy="agent"),
        ],
        DAYS[24].date(): [bt.Signal(9, plan, score=-1, strategy="agent")],  # unknown instrument
    }
    data = bt.MarketData({x.id: x for x in (a, b, c)}, pd.Series([1.0], index=DAYS[:1]), {})
    small = RISK.model_copy(
        update={"portfolio": RISK.portfolio.model_copy(update={"max_open_positions": 1})}
    )
    result = bt.run(
        data,
        CFG,
        small,
        FEES,
        start=DAYS[0].date(),
        end=DAYS[-1].date(),
        markets=["US"],
        external=external,
    )
    t = only_trade(result)  # the baseline setup on bar 19 is ignored
    assert (t.instrument_id, t.strategy, t.signal_date) == (3, "agent", DAYS[22].date())
    assert result.signals == 2
    assert result.rejections["max open positions"] == 1


def test_max_open_positions_and_ranking() -> None:
    small = NO_CLUSTER.model_copy(
        update={"per_trade": RISK.per_trade.model_copy(update={"max_position_pct": Decimal(22)})}
    )
    xs = [instrument(n, sector=f"S{n}") for n in range(1, 7)]
    result = run(*xs, risk=small)
    held = {t.instrument_id for t in result.trades}
    assert held == {3, 4, 5, 6}  # four slots, best relative strength first
    assert result.rejections["max open positions"] == 2


def test_cash_reserve_limits_positions() -> None:
    # 2 x EUR 300 reserved, then 3rd gets (1000 - 602 - 100 reserve) = 298; the 4th has nothing
    # above the reserve
    xs = [instrument(n, sector=f"S{n}") for n in range(1, 7)]
    result = run(*xs, risk=NO_CLUSTER)
    assert {t.instrument_id for t in result.trades} == {4, 5, 6}
    assert result.rejections["sizing: no_capacity"] == 3


def test_sector_cap() -> None:
    xs = [instrument(n, sector="Tech") for n in range(1, 4)]
    smaller = risk_with("per_trade", min_position_pct=Decimal(25), max_position_pct=Decimal(25))
    result = run(*xs, risk=smaller)
    assert len(result.trades) == 2  # 2 x ~EUR 250 fits 60 % of EUR 1,000, a third does not
    assert result.rejections["sector cap"] == 1


def test_fee_limit() -> None:
    strict = RISK.model_copy(
        update={"per_trade": RISK.per_trade.model_copy(update={"max_fee_to_risk_pct": Decimal(5)})}
    )
    result = run(instrument(1), risk=strict)
    assert result.trades == []
    assert result.rejections["fees above limit"] == 1


def test_earnings_buffer() -> None:
    x = instrument(1)
    x.earnings.append(DAYS[21].date())
    result = run(x)
    assert result.trades == []
    assert result.rejections["earnings within buffer"] == 1


def test_blacklist() -> None:
    result = run(instrument(1), instrument(2, "B"), risk=risk_with("instruments", blacklist=["S2"]))
    assert {t.instrument_id for t in result.trades} == {1}
    assert result.rejections["blacklisted"] == 1


def noisy(n: int, seed: int) -> bt.InstrumentData:
    """Small random closes before a setup on bar 30, so returns have a correlation estimate."""
    x = signal_on(instrument(n, sector=f"S{n}"), 30)
    noise = np.random.default_rng(seed).normal(0, 0.002, 30)
    x.frame.iloc[:30, 3] = 50 * (1 + noise)
    x.returns = x.frame["close"].pct_change().iloc[1:]
    return x


def test_correlated_cluster() -> None:
    # S3 and S1 move together (rho 1), S2 on its own; cap 40 % of EUR 1,000 = 400 < 2 x ~250
    risk = risk_with("portfolio", max_correlated_cluster_pct=Decimal(40))
    result = run(noisy(1, seed=7), noisy(2, seed=8), noisy(3, seed=7), risk=risk)
    assert {t.instrument_id for t in result.trades} == {2, 3}
    assert result.rejections["correlated cluster"] == 1


def gap_loss(row: int = 21) -> bt.InstrumentData:
    """S1 enters on bar 20 and gaps through its stop on `row`: about -EUR 32 (5 x 6.05 + fees)."""
    x = instrument(1, "A")
    set_bar(x, row, 44, 44.5, 43.5, 44)
    return x


def test_daily_loss_blocks_that_close() -> None:
    later = signal_on(instrument(3, "C"), 22)
    result = run(gap_loss(), signal_on(instrument(2, "B"), 21), later)
    assert {t.instrument_id for t in result.trades} == {1, 3}
    assert result.rejections["loss limits: daily"] == 1


def test_weekly_loss_pauses_until_monday() -> None:
    risk = RISK.model_copy(
        update={
            "loss_limits": RISK.loss_limits.model_copy(
                update={"daily_loss_pct": Decimal(50), "weekly_loss_pct": Decimal(3)}
            )
        }
    )
    # breach on Tuesday (bar 21): Thursday's signal is paused, Friday's is placed on Monday
    thursday, friday = signal_on(instrument(2, "B"), 23), signal_on(instrument(3, "C"), 24)
    result = run(gap_loss(), thursday, friday, risk=risk)
    assert {t.instrument_id for t in result.trades} == {1, 3}
    assert result.rejections["loss limits: weekly"] == 1


def test_drawdown_halts_and_cancels_entries() -> None:
    risk = risk_with("loss_limits", max_drawdown_pct=Decimal(2))
    waiting = signal_on(instrument(4, "D"), 20)
    set_bar(waiting, 21, 52, 53, 51, 52)  # its limit is still open at the halt
    later = signal_on(instrument(2, "B"), 30)
    result = run(gap_loss(), waiting, later, risk=risk)
    assert {t.instrument_id for t in result.trades} == {1}
    assert result.rejections["loss limits: drawdown (entry cancelled)"] == 1
    assert result.rejections["loss limits: drawdown"] == 1


def test_regime_gate() -> None:
    gated = CFG.model_copy(update={"pullback": CFG.pullback.model_copy(update={"regime_sma": 200})})
    weak = {"US": pd.Series(False, index=DAYS)}
    result = run(instrument(1), cfg=gated, above_sma=weak)
    assert result.trades == []
    assert result.rejections["regime: benchmark below its SMA"] == 1
    assert len(run(instrument(1), cfg=CFG, above_sma=weak).trades) == 1  # gate off


def test_vix_gate() -> None:
    gated = CFG.model_copy(
        update={"pullback": CFG.pullback.model_copy(update={"regime_max_vix": 30.0})}
    )
    vix = pd.Series([35.0, 25.0], index=[DAYS[0], DAYS[SIGNAL]])
    assert len(run(instrument(1), cfg=gated, vix=vix).trades) == 1  # 25 at the signal
    result = run(instrument(1), cfg=gated, vix=vix.iloc[:1])
    assert result.rejections["regime: VIX above the limit"] == 1


def test_atr_trail_after_breakeven() -> None:
    trail = CFG.model_copy(update={"pullback": CFG.pullback.model_copy(update={"trail_atr": 1.0})})
    x = instrument(1)
    set_bar(x, 21, 51, 52.1, 50.5, 52)  # +1R reached; highest close 52 - 1 ATR -> stop 51
    set_bar(x, 22, 51.5, 51.6, 50.9, 51)
    t = only_trade(run(x, cfg=trail))
    assert (t.exit_reason, t.exit_date) == ("stop", DAYS[22].date())
    assert t.exit_price == pytest.approx(51 * 0.9995)


# --- agent_shadow: every proposal the risk engine would approve on an empty account ---


def proposal(inst: int, row: int, target: float = 54.10, status: str = "proposed") -> Proposal:
    return Proposal.model_validate(
        {
            "id": uuid4(),
            "source": "agent",
            "as_of": DAYS[row].date(),
            "instrument_id": inst,
            "yahoo_symbol": f"S{inst}",
            "market": "US",
            "sector": "Tech",
            "status": status,
            "strategy": "agent",
            "entry_ref": "e",
            "stop_ref": "s",
            "target_ref": "t",
            "entry": 50.10,
            "stop": 48.10,
            "target": target,
            "thesis": "t",
            "invalidation": "i",
        }
    )


def shadow(*props: Proposal, end: int = 44, xs: int = 6) -> tuple[list[bt.Trade], dict[str, int]]:
    items = [instrument(n) for n in range(1, xs + 1)]
    for x in items:
        x.ind["avg_dollar_volume"] = 50e6
    set_bar(items[1], 23, 52, 53, 51, 52)  # S2 never trades down to the limit
    set_bar(items[1], 24, 52, 53, 51, 52)
    data = bt.MarketData({x.id: x for x in items}, pd.Series([1.0], index=DAYS[:1]), {})
    trades, rejections = bt.shadow_book(data, CFG, RISK, FEES, props, end=DAYS[end].date())
    return trades, dict(rejections)


def test_shadow_ignores_portfolio_capacity() -> None:
    props = [proposal(n, 22) for n in (1, 3, 4, 5, 6)]  # five at once, four slots in the book
    trades, rejections = shadow(*props)
    assert len(trades) == 5
    assert rejections == {}
    t = trades[0]
    assert (t.book, t.signal_date, t.entry_date) == (
        "agent_shadow",
        DAYS[22].date(),
        DAYS[23].date(),
    )
    assert t.quantity == pytest.approx(5.988)
    assert (t.exit_reason, t.holding_sessions) == ("time", 15)


def test_shadow_rejections_and_open_trades() -> None:
    trades, rejections = shadow(
        proposal(1, 22, target=53.10),  # R:R 1.5
        proposal(2, 22),  # limit never reached
        proposal(3, 22, status="no_trade"),
        proposal(4, 44),  # no next session yet
        proposal(9, 22),  # unknown instrument
        proposal(5, 22),
        end=30,
    )
    assert rejections == {"levels": 1, "entry not filled": 1}
    [t] = trades
    assert (t.instrument_id, t.exit_date, t.pnl_net_eur, t.holding_sessions) == (
        5,
        None,
        None,
        None,
    )


def test_shadow_stop_on_the_entry_bar() -> None:
    items = [instrument(1)]
    items[0].ind["avg_dollar_volume"] = 50e6
    set_bar(items[0], 23, 50, 50.5, 47, 47.5)
    data = bt.MarketData({1: items[0]}, pd.Series([1.0], index=DAYS[:1]), {})
    [t], _ = bt.shadow_book(data, CFG, RISK, FEES, [proposal(1, 22)], end=DAYS[44].date())
    assert (t.exit_reason, t.exit_date, t.holding_sessions) == ("stop", DAYS[23].date(), 0)
    assert t.pnl_net_eur is not None
    assert t.pnl_net_eur < 0
