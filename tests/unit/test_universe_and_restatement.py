from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from trading_agent.data.ingest import restated
from trading_agent.data.universe import Universe, load_universe
from trading_agent.domain.market import Bar, Instrument

ROOT = Path(__file__).resolve().parents[2]


def test_repo_universe_is_consistent() -> None:
    universe = load_universe(ROOT / "config")
    by_index = {
        idx: [i for i in universe.instruments if idx in i.indices] for idx in ("SP100", "DAX")
    }
    assert len(by_index["SP100"]) >= 100
    assert len(by_index["DAX"]) == 40
    assert all(i.market == "EU" and i.yahoo_symbol.endswith(".DE") for i in by_index["DAX"])
    assert universe.benchmarks == {"US": "SPY", "EU": "EXS1.DE"}


def _inst(symbol: str) -> Instrument:
    return Instrument(
        symbol=symbol,
        yahoo_symbol=symbol,
        name=symbol,
        market="US",
        exchange="SMART",
        currency="USD",
        kind="stock",
    )


def test_universe_rejects_duplicates_and_unknown_benchmarks() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        Universe(generated=date(2026, 10, 3), benchmarks={}, instruments=[_inst("A"), _inst("A")])
    with pytest.raises(ValidationError, match="benchmarks"):
        Universe(generated=date(2026, 10, 3), benchmarks={"US": "SPY"}, instruments=[_inst("A")])


def _bar(day: int, close: str) -> Bar:
    c = Decimal(close)
    return Bar(date=date(2026, 10, day), open=c, high=c, low=c, close=c, volume=1)


def test_restatement_detection() -> None:
    stored = [_bar(1, "100"), _bar(2, "102")]
    assert not restated(stored, [_bar(2, "102.3"), _bar(5, "50")], tolerance_pct=0.5)
    assert restated(stored, [_bar(2, "51"), _bar(5, "50")], tolerance_pct=0.5)  # 2:1 split
    assert not restated([], [_bar(5, "50")], tolerance_pct=0.5)
