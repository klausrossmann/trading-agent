from datetime import date
from decimal import Decimal

import pytest

from trading_agent.data.calendars import sessions
from trading_agent.data.quality import check_bars
from trading_agent.domain.market import Bar
from trading_agent.settings import QualityConfig

CFG = QualityConfig(max_jump_pct=40, stale_sessions=2, block_window_sessions=20)
EXPECTED = sessions("XNYS", date(2026, 6, 1), date(2026, 10, 2))


def bar(day: date, close: str = "100", **kw: object) -> Bar:
    c = Decimal(close)
    fields: dict[str, object] = {
        "date": day,
        "open": c,
        "high": c + 1,
        "low": c - 1,
        "close": c,
        "volume": 1000,
    }
    return Bar.model_validate(fields | kw)


def clean() -> list[Bar]:
    return [bar(d) for d in EXPECTED]


def checks(bars: list[Bar], expected: list[date] = EXPECTED) -> list[tuple[str, date | None, bool]]:
    return [(i.check, i.date, i.blocking) for i in check_bars("X", bars, expected, CFG)]


def test_clean_series_has_no_issues() -> None:
    assert checks(clean()) == []


def test_no_data_blocks() -> None:
    assert checks([]) == [("no_data", None, True)]


def test_stale_blocks() -> None:
    assert checks(clean()[:-3]) == [("stale", EXPECTED[-4], True)]
    assert checks(clean()[:-2]) == []  # within tolerance


@pytest.mark.parametrize(("index", "blocking"), [(-5, True), (10, False)])
def test_missing_session_blocks_only_if_recent(index: int, blocking: bool) -> None:
    bars = clean()
    del bars[index]
    assert checks(bars) == [("missing_session", EXPECTED[index], blocking)]


def test_unexpected_date() -> None:
    bars = sorted([*clean(), bar(date(2026, 9, 26))], key=lambda b: b.date)  # a Saturday
    assert checks(bars) == [("unexpected_date", date(2026, 9, 26), True)]


def test_jump_ohlc_and_zero_volume() -> None:
    bars = clean()
    bars[-3] = bar(EXPECTED[-3], close="150")  # +50 %, then -33 % back
    bars[-2] = bar(EXPECTED[-2], high=Decimal("99"))  # high below close
    bars[-1] = bar(EXPECTED[-1], volume=0)
    assert checks(bars) == [
        ("jump", EXPECTED[-3], True),
        ("ohlc_invalid", EXPECTED[-2], True),
        ("zero_volume", EXPECTED[-1], True),
    ]


def test_zero_volume_before_latest_bar_does_not_block() -> None:
    bars = clean()
    bars[-2] = bar(EXPECTED[-2], volume=0)
    assert checks(bars) == [("zero_volume", EXPECTED[-2], False)]
