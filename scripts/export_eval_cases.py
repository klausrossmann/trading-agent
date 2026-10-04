"""Export golden eval cases (tests/evals/cases) from stored market data.

Inputs are recorded from real days: baseline setups (technical), a few downtrends (technical,
expected neutral/avoid) and reports inside and outside the holding window (earnings). Needs a
backfilled database; re-run after an input schema change and commit the diff:

    uv run python scripts/export_eval_cases.py
"""

import asyncio
import json
import random
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from trading_agent import backtest
from trading_agent.data.universe import load_universe
from trading_agent.db.session import create_engine, session_factory
from trading_agent.domain.market import Market
from trading_agent.modules import earnings, technical
from trading_agent.modules.history import load_history
from trading_agent.risk.config import load_risk_config
from trading_agent.settings import Settings

OUT = Path(__file__).resolve().parents[1] / "tests" / "evals" / "cases"
START = date(2022, 1, 1)
SETUPS = {"US": 12, "EU": 6}
DOWNTRENDS = {"US": 1, "EU": 1}
EARNINGS_PER_SIDE = 5  # in window / outside
SEED = 5


def _write(module: str, name: str, expect: dict[str, Any], inp: dict[str, Any]) -> None:
    path = OUT / module / f"{name}.json"
    case = {"module": module, "expect": expect, "input": inp}
    path.write_text(json.dumps(case, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


async def main() -> None:
    settings = Settings()  # pyright: ignore[reportCallIssue]
    cfg_dir = settings.config_dir
    universe = load_universe(cfg_dir)
    p = backtest.load_backtest_config(cfg_dir).pullback
    per_trade = load_risk_config(cfg_dir).per_trade
    rules = technical.PlanRules(
        min_risk_reward=float(per_trade.min_risk_reward),
        stop_atr_min=float(per_trade.stop_atr_min),
        stop_atr_max=float(per_trade.stop_atr_max),
    )
    holding = p.entry_valid_sessions + p.time_stop_sessions
    engine = create_engine(settings.database_url)
    sessions = session_factory(engine)
    try:
        data = await backtest.load_market_data(sessions, dict(universe.benchmarks), p)
        rng = random.Random(SEED)  # noqa: S311  # reproducible sampling, not security
        setups: dict[Market, list[tuple[int, date]]] = {"US": [], "EU": []}
        downs: dict[Market, list[tuple[int, date]]] = {"US": [], "EU": []}
        for x in data.instruments.values():
            ind = x.ind
            down = (ind["close"] < ind["sma200"]) & (ind["sma50"] < ind["sma200"])
            for day, row in x.rows.items():
                if day < START:
                    continue
                if bool(x.setup.iloc[row]):
                    setups[x.instrument.market].append((x.id, day))
                elif bool(down.iloc[row]) and day.weekday() == 4:
                    downs[x.instrument.market].append((x.id, day))
        for d in (setups, downs):
            for pool in d.values():
                pool.sort()
                rng.shuffle(pool)

        for module in ("technical", "earnings"):
            (OUT / module).mkdir(parents=True, exist_ok=True)
            for old in (OUT / module).glob("*.json"):
                old.unlink()

        async def history(inst_id: int, day: date) -> Any:
            market = data.instruments[inst_id].instrument.market
            return await load_history(sessions, inst_id, day, universe.benchmarks.get(market))

        def pick(pool: list[tuple[int, date]], n: int) -> list[tuple[int, date]]:
            seen: set[int] = set()
            out: list[tuple[int, date]] = []
            for inst_id, day in pool:
                if inst_id not in seen:
                    seen.add(inst_id)
                    out.append((inst_id, day))
                if len(out) == n:
                    break
            return out

        for market in ("US", "EU"):
            for kind, pool, n, expect in (
                ("setup", setups[market], SETUPS[market], {}),
                ("downtrend", downs[market], DOWNTRENDS[market], {"ratings": ["neutral", "avoid"]}),
            ):
                for inst_id, day in pick(pool, n):
                    h = await history(inst_id, day)
                    inp = technical.compute(h.instrument, h.frame, h.benchmark_close, rules)
                    name = f"{kind}_{h.instrument.yahoo_symbol}_{day.isoformat()}"
                    _write("technical", name, expect, inp.model_dump(mode="json"))
                    print("technical", name)

        found = {True: 0, False: 0}
        mixed = [c for pair in zip(setups["US"], setups["EU"], strict=False) for c in pair]
        for inst_id, day in mixed:
            if all(v >= EARNINGS_PER_SIDE for v in found.values()):
                break
            h = await history(inst_id, day)
            if not h.events:
                continue
            inp = earnings.compute(h.instrument, h.frame, h.events, holding)
            side = inp.event_in_window
            if found[side] >= EARNINGS_PER_SIDE or not inp.history:
                continue
            found[side] += 1
            label = "in_window" if side else "outside"
            name = f"{label}_{h.instrument.yahoo_symbol}_{pd.Timestamp(day).date().isoformat()}"
            _write("earnings", name, {}, inp.model_dump(mode="json"))
            print("earnings", name)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
