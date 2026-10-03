"""Book simulation: runs the baseline strategy day by day through the simulator.

Used for the historical backtest (`trading-agent backtest`) and for the forward `baseline_sim`
book, which replays from its start date every evening. Pure apart from `load_market_data`.

Portfolio rules applied here are a subset of the risk engine (M8): sizing, max positions,
sector cap, fee-to-risk, orders per day, settled cash. Not yet: correlation clusters and loss
limits (reported as drawdown instead).
"""

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_agent.calc.fees import FeeSchedule, order_fees, round_trip_fees
from trading_agent.calc.indicators import bars_to_frame
from trading_agent.calc.sizing import position_size
from trading_agent.data import calendars
from trading_agent.db import market as repo
from trading_agent.domain.market import Instrument, Market
from trading_agent.domain.trading import Book, Trade
from trading_agent.execution import sim
from trading_agent.risk.config import RiskConfig
from trading_agent.strategies import pullback
from trading_agent.strategies.pullback import PullbackParams, TradePlan

SETTLEMENT_SESSIONS: dict[Market, int] = {"US": 1, "EU": 2}  # T+1 US, T+2 Xetra


class BaselineBook(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    start: date  # forward book: first signal date


class BacktestConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    pullback: PullbackParams = Field(default_factory=PullbackParams)
    slippage_pct: float = Field(default=0.05, ge=0)
    baseline_book: BaselineBook


def load_backtest_config(config_dir: Path) -> BacktestConfig:
    raw = yaml.safe_load((config_dir / "strategies.yaml").read_text(encoding="utf-8"))
    return BacktestConfig.model_validate(raw)


@dataclass
class InstrumentData:
    id: int
    instrument: Instrument
    frame: pd.DataFrame  # open/high/low/close/volume, date index
    ind: pd.DataFrame
    setup: pd.Series
    earnings: list[date]
    rows: dict[date, int]

    def bar(self, row: int) -> sim.Bar:
        r = self.frame.iloc[row]
        return sim.Bar(float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))


@dataclass
class MarketData:
    instruments: dict[int, InstrumentData]
    usd_per_eur: pd.Series  # ECB reference rate, date index
    benchmarks: dict[Market, pd.DataFrame]

    def rate(self, currency: str, day: date) -> float:
        """Units of `currency` per EUR on `day` (last known ECB rate)."""
        if currency == "EUR":
            return 1.0
        known = self.usd_per_eur.loc[: pd.Timestamp(day)]
        return float(known.iloc[-1]) if not known.empty else float(self.usd_per_eur.iloc[0])


def prepare(
    frames: dict[int, pd.DataFrame],
    instruments: dict[int, Instrument],
    earnings: dict[int, list[date]],
    usd_per_eur: pd.Series,
    benchmarks: dict[Market, str],
    p: PullbackParams,
) -> MarketData:
    by_symbol = {i.yahoo_symbol: k for k, i in instruments.items()}
    bench_frames: dict[Market, pd.DataFrame] = {
        m: frames[by_symbol[s]] for m, s in benchmarks.items() if s in by_symbol
    }
    data: dict[int, InstrumentData] = {}
    for inst_id, inst in instruments.items():
        frame = frames.get(inst_id)
        if frame is None or frame.empty or not inst.indices:  # benchmarks are not traded
            continue
        bench = bench_frames.get(inst.market)
        bench_close = bench["close"] if bench is not None else frame["close"] * math.nan
        ind = pullback.indicators(frame, bench_close, p)
        data[inst_id] = InstrumentData(
            id=inst_id,
            instrument=inst,
            frame=frame,
            ind=ind,
            setup=pullback.setup_mask(ind, p),
            earnings=sorted(earnings.get(inst_id, [])),
            rows={pd.Timestamp(d).date(): i for i, d in enumerate(frame.index)},
        )
    return MarketData(data, usd_per_eur.sort_index(), bench_frames)


async def load_market_data(
    sessions: async_sessionmaker[AsyncSession], benchmarks: dict[Market, str], p: PullbackParams
) -> MarketData:
    async with sessions() as s:
        instruments = await repo.active_instruments(s)
        bars = await repo.all_bars(s)
        earnings = await repo.earnings_dates(s)
        fx = await repo.fx_rates(s, "USD")
    frames = {k: bars_to_frame(v) for k, v in bars.items()}
    usd = pd.Series(
        [float(o.value) for o in fx], index=pd.DatetimeIndex([pd.Timestamp(o.date) for o in fx])
    )
    return prepare(frames, instruments, earnings, usd, benchmarks, p)


@dataclass
class _Pending:
    data: InstrumentData
    signal_date: date
    signal_row: int
    plan: TradePlan
    quantity: int
    reserved_eur: float


@dataclass
class _Position:
    data: InstrumentData
    signal_date: date
    plan: TradePlan
    quantity: int
    entry_date: date
    entry_row: int
    entry_price: float
    stop: float
    fees: float
    fees_eur: float
    cost_eur: float
    risk_eur: float


@dataclass
class BacktestResult:
    trades: list[Trade]
    equity: pd.Series  # EUR, per session date
    invested: pd.Series  # EUR in open positions
    rejections: Counter[str] = field(default_factory=Counter[str])
    signals: int = 0


def run(
    data: MarketData,
    cfg: BacktestConfig,
    risk: RiskConfig,
    fees: FeeSchedule,
    *,
    start: date,
    end: date,
    markets: list[Market],
    book: Book = "backtest",
) -> BacktestResult:
    p = cfg.pullback
    slip = cfg.slippage_pct
    limits = risk.sizing_limits()
    budget = float(risk.capital.agent_budget_eur)
    days = sorted({d for x in data.instruments.values() for d in x.rows if start <= d <= end})

    cash = budget
    unsettled: list[tuple[date, float]] = []
    pending: list[_Pending] = []
    open_: list[_Position] = []
    trades: list[Trade] = []
    equity: dict[date, float] = {}
    invested: dict[date, float] = {}
    rejections: Counter[str] = Counter()
    signals = 0

    def to_eur(amount: float, inst: Instrument, day: date) -> float:
        return amount / data.rate(inst.currency, day)

    def close_position(pos: _Position, day: date, row: int, fill: sim.Fill) -> None:
        inst = pos.data.instrument
        sell_fee = float(
            order_fees(fees, inst.market, "sell", pos.quantity, Decimal(str(round(fill.price, 4))))
        )
        proceeds_eur = to_eur(pos.quantity * fill.price - sell_fee, inst, day)
        cal = calendars.CALENDAR_BY_MARKET[inst.market]
        settles = calendars.session_offset(cal, day, SETTLEMENT_SESSIONS[inst.market])
        unsettled.append((settles, proceeds_eur))
        pnl = proceeds_eur - pos.cost_eur
        trades.append(
            Trade(
                book=book,
                strategy=pullback.NAME,
                instrument_id=pos.data.id,
                yahoo_symbol=inst.yahoo_symbol,
                market=inst.market,
                sector=inst.sector,
                signal_date=pos.signal_date,
                entry_date=pos.entry_date,
                entry_price=pos.entry_price,
                quantity=pos.quantity,
                stop=pos.plan.stop,
                target=pos.plan.target,
                risk_eur=pos.risk_eur,
                fees=pos.fees + sell_fee,
                fees_eur=pos.fees_eur + to_eur(sell_fee, inst, day),
                exit_date=day,
                exit_price=fill.price,
                exit_reason=fill.reason,
                pnl_net_eur=pnl,
                r_multiple=pnl / pos.risk_eur if pos.risk_eur > 0 else None,
                holding_sessions=row - pos.entry_row,
            )
        )
        open_.remove(pos)

    for day in days:
        cash += sum(a for d, a in unsettled if d <= day)
        unsettled = [(d, a) for d, a in unsettled if d > day]

        # 1. Exits for positions entered on earlier bars
        for pos in list(open_):
            row = pos.data.rows.get(day)
            if row is None or row == pos.entry_row:
                continue
            bar = pos.data.bar(row)
            fill = sim.check_exit(bar, pos.stop, pos.plan.target, slip)
            if fill is None and row - pos.entry_row >= p.time_stop_sessions:
                fill = sim.time_exit(bar, slip)
            if fill is not None:
                close_position(pos, day, row, fill)
            else:
                trigger = pos.entry_price + p.breakeven_r * pos.plan.risk_per_share
                pos.stop = sim.trailed_stop(bar, pos.stop, pos.entry_price, trigger)

        # 2. Pending limit entries
        for order in list(pending):
            row = order.data.rows.get(day)
            if row is None:
                continue
            inst = order.data.instrument
            price = sim.fill_entry(order.data.bar(row), order.plan.entry, slip)
            if price is None:
                if row - order.signal_row >= p.entry_valid_sessions:
                    pending.remove(order)
                continue
            pending.remove(order)
            buy_fee = float(
                order_fees(fees, inst.market, "buy", order.quantity, Decimal(str(round(price, 4))))
            )
            cost_eur = to_eur(order.quantity * price + buy_fee, inst, day)
            cash -= cost_eur
            pos = _Position(
                data=order.data,
                signal_date=order.signal_date,
                plan=order.plan,
                quantity=order.quantity,
                entry_date=day,
                entry_row=row,
                entry_price=price,
                stop=order.plan.stop,
                fees=buy_fee,
                fees_eur=to_eur(buy_fee, inst, day),
                cost_eur=cost_eur,
                risk_eur=to_eur(order.quantity * order.plan.risk_per_share, inst, day),
            )
            open_.append(pos)
            stopped = sim.check_exit(order.data.bar(row), pos.stop, None, slip)
            if stopped is not None:
                close_position(pos, day, row, stopped)

        # 3. New signals at today's close, ranked by relative strength
        candidates: list[tuple[float, InstrumentData, int, TradePlan]] = []
        busy = {x.data.id for x in pending} | {x.data.id for x in open_}
        for x in data.instruments.values():
            row = x.rows.get(day)
            if row is None or not bool(x.setup.iloc[row]) or x.instrument.market not in markets:
                continue
            signals += 1
            if x.id in busy:
                rejections["already held or pending"] += 1
                continue
            cal = calendars.CALENDAR_BY_MARKET[x.instrument.market]
            buffer_end = calendars.session_offset(cal, day, p.earnings_buffer_sessions)
            if not pullback.earnings_clear(day, buffer_end, x.earnings):
                rejections["earnings within buffer"] += 1
                continue
            plan = pullback.plan_trade(x.frame.iloc[: row + 1], float(x.ind["atr"].iloc[row]), p)
            if plan is None:
                rejections["no valid stop"] += 1
                continue
            rs = float(x.ind["rs"].iloc[row])
            candidates.append((rs if math.isfinite(rs) else -math.inf, x, row, plan))
        candidates.sort(key=lambda c: c[0], reverse=True)

        placed = 0
        for _, x, row, plan in candidates:
            inst = x.instrument
            if len(open_) + len(pending) >= risk.portfolio.max_open_positions:
                rejections["max open positions"] += 1
                continue
            if placed >= risk.execution.max_orders_per_day:
                rejections["max orders per day"] += 1
                continue
            rate = data.rate(inst.currency, day)
            available_eur = cash - sum(o.reserved_eur for o in pending)
            entry, stop = Decimal(str(round(plan.entry, 4))), Decimal(str(round(plan.stop, 4)))
            sizing = position_size(
                entry=entry,
                stop=stop,
                settled_cash=Decimal(str(round(max(available_eur, 0) * rate, 2))),
                eur_rate=Decimal(str(rate)),
                limits=limits,
            )
            if sizing.quantity == 0:
                rejections[f"sizing: {sizing.rejection}"] += 1
                continue
            q = sizing.quantity
            target = Decimal(str(round(plan.target, 4)))
            fee_cap = risk.per_trade.max_fee_to_risk_pct / 100 * sizing.risk
            if round_trip_fees(fees, inst.market, q, entry, target) > fee_cap:
                rejections["fees above limit"] += 1
                continue
            value_eur = q * plan.entry / rate
            same_sector = sum(
                o.reserved_eur for o in pending if o.data.instrument.sector == inst.sector
            ) + sum(pos.cost_eur for pos in open_ if pos.data.instrument.sector == inst.sector)
            if same_sector + value_eur > float(risk.portfolio.max_sector_pct) / 100 * budget:
                rejections["sector cap"] += 1
                continue
            buy_fee = float(order_fees(fees, inst.market, "buy", q, entry))
            pending.append(_Pending(x, day, row, plan, q, (q * plan.entry + buy_fee) / rate))
            placed += 1

        # 4. Mark to market
        held = 0.0
        for pos in open_:
            closes = pos.data.frame["close"].loc[: pd.Timestamp(day)]
            held += to_eur(pos.quantity * float(closes.iloc[-1]), pos.data.instrument, day)
        equity[day] = cash + sum(a for _, a in unsettled) + held
        invested[day] = held

    for pos in open_:
        trades.append(
            Trade(
                book=book,
                strategy=pullback.NAME,
                instrument_id=pos.data.id,
                yahoo_symbol=pos.data.instrument.yahoo_symbol,
                market=pos.data.instrument.market,
                sector=pos.data.instrument.sector,
                signal_date=pos.signal_date,
                entry_date=pos.entry_date,
                entry_price=pos.entry_price,
                quantity=pos.quantity,
                stop=pos.plan.stop,
                target=pos.plan.target,
                risk_eur=pos.risk_eur,
                fees=pos.fees,
                fees_eur=pos.fees_eur,
            )
        )

    def series(values: dict[date, float]) -> pd.Series:
        return pd.Series(
            list(values.values()),
            index=pd.DatetimeIndex([pd.Timestamp(d) for d in values]),
            dtype=float,
        )

    return BacktestResult(trades, series(equity), series(invested), rejections, signals)


def benchmark_equity(
    data: MarketData, market: Market, start: date, end: date, capital: float
) -> pd.Series | None:
    """Buy and hold the market's benchmark ETF with `capital` EUR, valued in EUR."""
    frame = data.benchmarks.get(market)
    if frame is None:
        return None
    close = frame["close"].loc[pd.Timestamp(start) : pd.Timestamp(end)]
    if close.empty:
        return None
    currency: Literal["USD", "EUR"] = "USD" if market == "US" else "EUR"
    in_eur = pd.Series(
        [
            float(v) / data.rate(currency, ts.date())
            for ts, v in zip(pd.DatetimeIndex(close.index), close.to_numpy(), strict=True)
        ],
        index=close.index,
    )
    return capital * in_eur / float(in_eur.iloc[0])
