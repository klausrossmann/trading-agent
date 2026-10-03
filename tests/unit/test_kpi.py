import math
from datetime import date

import pandas as pd
import pytest

from trading_agent.domain.trading import Trade
from trading_agent.evaluation.kpi import equity_stats, max_drawdown_pct, trade_stats


def trade(pnl: float | None, r: float | None = None, fees: float = 1.0) -> Trade:
    return Trade(
        book="backtest",
        strategy="s",
        instrument_id=1,
        yahoo_symbol="X",
        market="US",
        sector=None,
        signal_date=date(2026, 1, 1),
        entry_date=date(2026, 1, 2),
        entry_price=100,
        quantity=1,
        stop=90,
        target=120,
        risk_eur=10,
        fees=fees,
        fees_eur=fees,
        exit_date=date(2026, 1, 9) if pnl is not None else None,
        pnl_net_eur=pnl,
        r_multiple=r,
        holding_sessions=5 if pnl is not None else None,
    )


def test_trade_stats() -> None:
    s = trade_stats([trade(20, 2), trade(-10, -1), trade(-5, -0.5), trade(None)])
    assert s.trades == 3  # the open trade is ignored
    assert s.win_rate_pct == pytest.approx(100 / 3)
    assert s.avg_r == pytest.approx(0.5 / 3)
    assert s.expectancy_eur == pytest.approx(5 / 3)
    assert s.profit_factor == pytest.approx(20 / 15)
    assert (s.total_pnl_eur, s.fees_eur) == (5, 3)


def test_trade_stats_empty_and_no_losses() -> None:
    assert trade_stats([]).trades == 0
    assert math.isinf(trade_stats([trade(5, 0.5)]).profit_factor)


def test_drawdown_and_cagr() -> None:
    idx = pd.to_datetime(["2025-01-01", "2025-07-01", "2026-01-01"])
    equity = pd.Series([1000.0, 1500.0, 2000.0 * 365 / 365], index=idx)
    assert max_drawdown_pct(pd.Series([100.0, 120, 90, 130])) == pytest.approx(-25)
    es = equity_stats(equity)
    assert es.total_return_pct == pytest.approx(100)
    assert es.cagr_pct == pytest.approx(100, rel=0.01)  # one year (365 of 365.25 days)
    assert es.max_drawdown_pct == 0
