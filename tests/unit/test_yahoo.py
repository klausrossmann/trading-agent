from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd

from trading_agent.data.yahoo import bars_from_history, earnings_from_frame

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _frame(name: str, tz: str) -> pd.DataFrame:
    frame = pd.read_csv(FIXTURES / name, index_col=0)
    frame.index = pd.to_datetime(frame.index, utc=True).tz_convert(tz)
    return frame


def test_bars_from_recorded_history() -> None:
    bars = bars_from_history(_frame("yahoo_history_aapl.csv", "America/New_York"))
    assert [b.date for b in bars][:2] == [date(2026, 9, 21), date(2026, 9, 22)]
    assert len(bars) == 10
    first = bars[0]
    assert (first.open, first.close, first.volume) == (
        Decimal("335.28"),
        Decimal("338.98"),
        34999200,
    )
    assert all(b.low <= min(b.open, b.close) and b.high >= max(b.open, b.close) for b in bars)


def test_bars_use_exchange_local_dates() -> None:
    bars = bars_from_history(_frame("yahoo_history_sap_de.csv", "Europe/Berlin"))
    assert bars[0].date == date(2026, 9, 21)  # midnight CEST would be the 20th in UTC


def test_bars_skip_incomplete_rows_and_duplicates() -> None:
    idx = pd.DatetimeIndex(
        ["2026-10-01 00:00", "2026-10-01 00:00", "2026-10-02 00:00"], tz="America/New_York"
    )
    frame = pd.DataFrame(
        {
            "Open": [10.0, 99.0, float("nan")],
            "High": [11.0, 99.0, 12.0],
            "Low": [9.0, 99.0, 10.0],
            "Close": [10.5, 99.0, 11.0],
            "Volume": [100, 100, 100],
        },
        index=idx,
    )
    bars = bars_from_history(frame)
    assert [(b.date, b.close) for b in bars] == [(date(2026, 10, 1), Decimal("10.5"))]


def test_earnings_from_recorded_frame() -> None:
    events = earnings_from_frame(_frame("yahoo_earnings_aapl.csv", "America/New_York"), "US")
    assert len(events) == 6
    assert [e.date for e in events] == sorted(e.date for e in events)
    upcoming = events[-1]
    assert upcoming.date == date(2026, 10, 29)
    assert upcoming.timing == "amc"  # 16:00 ET
    assert upcoming.eps_estimate == Decimal("1.98")
    assert upcoming.eps_actual is None
    assert upcoming.ts == datetime(2026, 10, 29, 20, 0, tzinfo=UTC)


def test_earnings_timing_classification() -> None:
    idx = pd.DatetimeIndex(
        ["2026-10-01 00:00", "2026-10-02 07:00", "2026-10-05 11:00", "2026-10-06 22:00"],
        tz="Europe/Berlin",
    )
    frame = pd.DataFrame({"EPS Estimate": [1.0] * 4, "Reported EPS": [None] * 4}, index=idx)
    timings = [e.timing for e in earnings_from_frame(frame, "EU")]
    assert timings == ["unknown", "bmo", "during", "amc"]
