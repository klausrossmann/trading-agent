import pytest

from trading_agent.execution.sim import (
    Bar,
    Fill,
    chandelier_stop,
    check_exit,
    fill_entry,
    time_exit,
    trailed_stop,
)

SLIP = 0.05


def test_entry_fills_at_limit_or_better() -> None:
    assert fill_entry(Bar(101, 102, 100.5, 101), limit=100, slippage_pct=SLIP) is None
    assert fill_entry(Bar(101, 102, 99, 100), limit=100, slippage_pct=0) == 100  # touched
    assert fill_entry(Bar(98, 99, 97, 98), limit=100, slippage_pct=0) == 98  # gapped below
    assert fill_entry(Bar(101, 102, 99, 100), limit=100, slippage_pct=SLIP) == pytest.approx(100.05)


@pytest.mark.parametrize(
    ("bar", "expected"),
    [
        (Bar(94, 96, 93, 95), Fill(94, "stop")),  # gap below the stop: open
        (Bar(112, 113, 111, 112), Fill(112, "target")),  # gap above the target: open
        (Bar(100, 101, 94, 100), Fill(95, "stop")),
        (Bar(100, 111, 99, 110), Fill(110, "target")),
        (Bar(100, 111, 94, 100), Fill(95, "stop")),  # both inside the bar: stop wins
        (Bar(100, 105, 96, 104), None),
    ],
)
def test_exits(bar: Bar, expected: Fill | None) -> None:
    assert check_exit(bar, stop=95, target=110, slippage_pct=0) == expected


def test_entry_bar_checks_only_the_stop() -> None:
    assert check_exit(Bar(100, 120, 99, 119), stop=95, target=None, slippage_pct=0) is None
    assert check_exit(Bar(100, 120, 94, 119), stop=95, target=None, slippage_pct=0) == Fill(
        95, "stop"
    )


def test_sell_slippage_and_time_exit() -> None:
    fill = check_exit(Bar(100, 101, 94, 100), stop=95, target=110, slippage_pct=SLIP)
    assert fill is not None
    assert fill.price == pytest.approx(95 * 0.9995)
    exit_fill = time_exit(Bar(100, 101, 99, 100), SLIP)
    assert (exit_fill.price, exit_fill.reason) == (pytest.approx(99.95), "time")


def test_breakeven_stop_only_tightens() -> None:
    assert trailed_stop(Bar(100, 104, 99, 103), stop=95, entry=100, trigger=105) == 95
    assert trailed_stop(Bar(100, 105, 99, 103), stop=95, entry=100, trigger=105) == 100
    assert trailed_stop(Bar(100, 105, 99, 103), stop=101, entry=100, trigger=105) == 101


def test_atr_trail_starts_at_breakeven_and_only_tightens() -> None:
    assert chandelier_stop(95, entry=100, highest_close=110, atr=2, k=2) == 95  # before breakeven
    assert chandelier_stop(100, entry=100, highest_close=110, atr=2, k=2) == 106
    assert chandelier_stop(107, entry=100, highest_close=110, atr=2, k=2) == 107
    assert chandelier_stop(100, entry=100, highest_close=110, atr=float("nan"), k=2) == 100
