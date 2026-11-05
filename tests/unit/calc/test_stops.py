from trading_agent.calc.stops import breakeven_stop, chandelier_stop


def test_breakeven_stop_only_tightens() -> None:
    assert breakeven_stop(104, stop=95, entry=100, trigger=105) == 95
    assert breakeven_stop(105, stop=95, entry=100, trigger=105) == 100
    assert breakeven_stop(105, stop=101, entry=100, trigger=105) == 101


def test_atr_trail_starts_at_breakeven_and_only_tightens() -> None:
    assert chandelier_stop(95, entry=100, highest_close=110, atr=2, k=2) == 95  # before breakeven
    assert chandelier_stop(100, entry=100, highest_close=110, atr=2, k=2) == 106
    assert chandelier_stop(107, entry=100, highest_close=110, atr=2, k=2) == 107
    assert chandelier_stop(100, entry=100, highest_close=110, atr=float("nan"), k=2) == 100
