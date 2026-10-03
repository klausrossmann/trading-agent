from datetime import date
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, model_validator

from trading_agent.domain.market import Instrument, Market


class Universe(BaseModel):
    generated: date
    benchmarks: dict[Market, str]  # market -> yahoo symbol, e.g. US -> SPY
    instruments: list[Instrument]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        symbols = [i.yahoo_symbol for i in self.instruments]
        if dupes := {s for s in symbols if symbols.count(s) > 1}:
            raise ValueError(f"duplicate yahoo symbols: {sorted(dupes)}")
        if missing := set(self.benchmarks.values()) - set(symbols):
            raise ValueError(f"benchmarks not in instruments: {sorted(missing)}")
        return self


def load_universe(config_dir: Path) -> Universe:
    raw = yaml.safe_load((config_dir / "universe.yaml").read_text(encoding="utf-8"))
    return Universe.model_validate(raw)
