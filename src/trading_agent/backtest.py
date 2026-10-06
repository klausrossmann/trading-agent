"""Book simulation: runs the baseline strategy day by day through the simulator.

Used for the historical backtest (`trading-agent backtest`) and for the forward `baseline_sim`
book, which replays from its start date every evening. Pure apart from `load_market_data`.

Portfolio rules applied here mirror the risk engine (M8): sizing, max positions, sector cap,
correlation cluster, fee-to-risk, orders per day, settled cash, blacklist and the loss limits.
Loss limits are measured at each close: a daily or weekly breach blocks the signals that would
be placed before the pause ends; the drawdown limit cancels pending entries and stops new
ones for the rest of the run (a halt needs a manual reset).
"""

import math
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
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
from trading_agent.domain.numbers import to_decimal
from trading_agent.domain.proposals import Proposal
from trading_agent.domain.risk import Controls, MarketSnapshot, PortfolioState
from trading_agent.domain.trading import Book, Trade
from trading_agent.execution import sim
from trading_agent.portfolio.snapshot import CORRELATION_SESSIONS, correlation, proposal_levels
from trading_agent.risk import engine
from trading_agent.risk.config import RiskConfig
from trading_agent.strategies import pullback
from trading_agent.strategies.pullback import PullbackParams, TradePlan

SETTLEMENT_SESSIONS: dict[Market, int] = {"US": 1, "EU": 2}  # T+1 US, T+2 Xetra
VIX_SERIES = "VIXCLS"


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
    returns: pd.Series = field(init=False)

    def __post_init__(self) -> None:
        self.returns = self.frame["close"].pct_change().iloc[1:]

    def bar(self, row: int) -> sim.Bar:
        r = self.frame.iloc[row]
        return sim.Bar(float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))

    def recent_returns(self, day: date) -> pd.Series:
        return self.returns.loc[: pd.Timestamp(day)].tail(CORRELATION_SESSIONS)

    def close_on(self, day: date) -> float:
        """The last close up to `day` (the instrument's market may be closed that day)."""
        close = self.frame["close"]
        return float(close.iloc[close.index.searchsorted(pd.Timestamp(day), side="right") - 1])


@dataclass
class MarketData:
    instruments: dict[int, InstrumentData]
    usd_per_eur: pd.Series  # ECB reference rate, date index
    benchmarks: dict[Market, pd.DataFrame]
    above_sma: dict[Market, pd.Series] = field(default_factory=dict[Market, pd.Series])
    vix: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))

    def rate(self, currency: str, day: date) -> float:
        """Units of `currency` per EUR on `day` (last known ECB rate)."""
        if currency == "EUR":
            return 1.0
        known = self.usd_per_eur.loc[: pd.Timestamp(day)]
        return float(known.iloc[-1]) if not known.empty else float(self.usd_per_eur.iloc[0])

    def regime_block(self, market: Market, day: date, p: PullbackParams) -> str | None:
        """Why the market regime blocks new entries at `day`'s close; unknown data never blocks."""
        ts = pd.Timestamp(day)
        trend = self.above_sma.get(market)
        if p.regime_sma is not None and trend is not None:
            known = trend.loc[:ts]
            if not known.empty and not bool(known.iloc[-1]):
                return "regime: benchmark below its SMA"
        if p.regime_max_vix is not None:
            vix = self.vix.loc[:ts]
            if not vix.empty and float(vix.iloc[-1]) > p.regime_max_vix:
                return "regime: VIX above the limit"
        return None


def prepare(
    frames: dict[int, pd.DataFrame],
    instruments: dict[int, Instrument],
    earnings: dict[int, list[date]],
    usd_per_eur: pd.Series,
    benchmarks: dict[Market, str],
    p: PullbackParams,
    vix: pd.Series | None = None,
) -> MarketData:
    by_symbol = {i.yahoo_symbol: k for k, i in instruments.items()}
    bench_frames: dict[Market, pd.DataFrame] = {
        m: frames[by_symbol[s]] for m, s in benchmarks.items() if s in by_symbol
    }
    above_sma: dict[Market, pd.Series] = {}
    if p.regime_sma is not None:
        for m, f in bench_frames.items():
            avg = f["close"].rolling(p.regime_sma).mean()
            above_sma[m] = (f["close"] > avg) | avg.isna()
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
    return MarketData(
        data,
        usd_per_eur.sort_index(),
        bench_frames,
        above_sma,
        vix.sort_index() if vix is not None else pd.Series(dtype=float),
    )


def _series(values: Mapping[date, float]) -> pd.Series:
    return pd.Series(
        list(values.values()),
        index=pd.DatetimeIndex([pd.Timestamp(d) for d in values]),
        dtype=float,
    )


async def load_market_data(
    sessions: async_sessionmaker[AsyncSession], benchmarks: dict[Market, str], p: PullbackParams
) -> MarketData:
    async with sessions() as s:
        instruments = await repo.active_instruments(s)
        bars = await repo.all_bars(s)
        earnings = await repo.earnings_dates(s)
        fx = await repo.fx_rates(s, "USD")
        vix = await repo.macro_series(s, VIX_SERIES) if p.regime_max_vix is not None else []
    frames = {k: bars_to_frame(v) for k, v in bars.items()}
    return prepare(
        frames,
        instruments,
        earnings,
        _series({o.date: float(o.value) for o in fx}),
        benchmarks,
        p,
        _series({o.date: float(o.value) for o in vix}),
    )


@dataclass
class _Pending:
    data: InstrumentData
    signal_date: date
    signal_row: int
    plan: TradePlan
    quantity: Decimal
    reserved_eur: float
    strategy: str


@dataclass
class _Position:
    data: InstrumentData
    signal_date: date
    plan: TradePlan
    quantity: Decimal
    entry_date: date
    entry_row: int
    entry_price: float
    stop: float
    fees: float
    fees_eur: float
    cost_eur: float
    risk_eur: float
    strategy: str
    highest_close: float

    def trade(self, book: Book) -> Trade:
        """The position as a trade that is still open."""
        inst = self.data.instrument
        return Trade(
            book=book,
            strategy=self.strategy,
            instrument_id=self.data.id,
            yahoo_symbol=inst.yahoo_symbol,
            market=inst.market,
            sector=inst.sector,
            signal_date=self.signal_date,
            entry_date=self.entry_date,
            entry_price=self.entry_price,
            quantity=float(self.quantity),
            stop=self.plan.stop,
            target=self.plan.target,
            risk_eur=self.risk_eur,
            fees=self.fees,
            fees_eur=self.fees_eur,
        )


@dataclass(frozen=True)
class Signal:
    """A trade idea from outside the baseline rules (agent proposals), at `day`'s close."""

    instrument_id: int
    plan: TradePlan
    score: float  # higher is placed first
    strategy: str


@dataclass
class BacktestResult:
    trades: list[Trade]
    equity: pd.Series  # EUR, per session date
    invested: pd.Series  # EUR in open positions
    rejections: Counter[str] = field(default_factory=Counter[str])
    signals: int = 0


def _not_admitted(
    x: InstrumentData, day: date, busy: set[int], p: PullbackParams, blacklist: Collection[str]
) -> str | None:
    if x.instrument.yahoo_symbol in blacklist:
        return "blacklisted"
    if x.id in busy:
        return "already held or pending"
    cal = calendars.CALENDAR_BY_MARKET[x.instrument.market]
    buffer_end = calendars.session_offset(cal, day, p.earnings_buffer_sessions)
    if not pullback.earnings_clear(day, buffer_end, x.earnings):
        return "earnings within buffer"
    return None


def _next_stop(
    x: InstrumentData,
    row: int,
    stop: float,
    entry: float,
    plan: TradePlan,
    highest_close: float,
    p: PullbackParams,
) -> float:
    """Breakeven at +1R, then the optional ATR trail, on `row`'s bar."""
    stop = sim.trailed_stop(x.bar(row), stop, entry, entry + p.breakeven_r * plan.risk_per_share)
    if p.trail_atr is None:
        return stop
    atr = float(x.ind["atr"].iloc[row])
    return sim.chandelier_stop(stop, entry, highest_close, atr, p.trail_atr)


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
    external: Mapping[date, Sequence[Signal]] | None = None,
) -> BacktestResult:
    """Simulates the baseline's setups, or only the `external` signals when given."""
    p = cfg.pullback
    slip = cfg.slippage_pct
    limits = risk.sizing_limits()
    budget = float(risk.capital.agent_budget_eur)
    loss = risk.loss_limits
    blacklist = set(risk.instruments.blacklist)
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
    last_equity = week_base = peak = budget
    pause: tuple[date, str] | None = None  # placements before this day are blocked
    halted = False

    def to_eur(amount: float, inst: Instrument, day: date) -> float:
        return amount / data.rate(inst.currency, day)

    def close_position(pos: _Position, day: date, row: int, fill: sim.Fill) -> None:
        inst = pos.data.instrument
        sell_fee = float(
            order_fees(fees, inst.market, "sell", pos.quantity, to_decimal(fill.price))
        )
        proceeds_eur = to_eur(float(pos.quantity) * fill.price - sell_fee, inst, day)
        cal = calendars.CALENDAR_BY_MARKET[inst.market]
        settles = calendars.session_offset(cal, day, SETTLEMENT_SESSIONS[inst.market])
        unsettled.append((settles, proceeds_eur))
        pnl = proceeds_eur - pos.cost_eur
        trades.append(
            pos.trade(book).model_copy(
                update={
                    "fees": pos.fees + sell_fee,
                    "fees_eur": pos.fees_eur + to_eur(sell_fee, inst, day),
                    "exit_date": day,
                    "exit_price": fill.price,
                    "exit_reason": fill.reason,
                    "pnl_net_eur": pnl,
                    "r_multiple": pnl / pos.risk_eur if pos.risk_eur > 0 else None,
                    "holding_sessions": row - pos.entry_row,
                }
            )
        )
        open_.remove(pos)

    for i, day in enumerate(days):
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
                pos.highest_close = max(pos.highest_close, bar.close)
                pos.stop = _next_stop(
                    pos.data, row, pos.stop, pos.entry_price, pos.plan, pos.highest_close, p
                )

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
            buy_fee = float(order_fees(fees, inst.market, "buy", order.quantity, to_decimal(price)))
            cost_eur = to_eur(float(order.quantity) * price + buy_fee, inst, day)
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
                risk_eur=to_eur(float(order.quantity) * order.plan.risk_per_share, inst, day),
                strategy=order.strategy,
                highest_close=order.data.bar(row).close,
            )
            open_.append(pos)
            stopped = sim.check_exit(order.data.bar(row), pos.stop, None, slip)
            if stopped is not None:
                close_position(pos, day, row, stopped)

        # 3. Mark to market at the close
        held = sum(
            to_eur(float(pos.quantity) * pos.data.close_on(day), pos.data.instrument, day)
            for pos in open_
        )
        equity[day] = eq = cash + sum(a for _, a in unsettled) + held
        invested[day] = held

        # 4. Loss limits at the close, against the previous close and the last close before
        # the week; a breach pauses the placements of the next session (and the week).
        placement = days[i + 1] if i + 1 < len(days) else day + timedelta(days=1)
        if i > 0 and days[i - 1] < day - timedelta(days=day.weekday()):
            week_base = last_equity
        breaches: list[tuple[date, str]] = []
        if eq - last_equity <= -float(loss.daily_loss_pct) / 100 * budget:
            breaches.append((placement + timedelta(days=1), "loss limits: daily"))
        monday = placement + timedelta(days=7 - placement.weekday())
        # A placement in the next week measures that week, which has no P&L yet (as live).
        same_week = day >= placement - timedelta(days=placement.weekday())
        if same_week and eq - week_base <= -float(loss.weekly_loss_pct) / 100 * budget:
            breaches.append((monday, "loss limits: weekly"))
        for breach in breaches:
            if pause is None or breach[0] > pause[0]:
                pause = breach
        peak = max(peak, eq)
        if not halted and peak - eq >= float(loss.max_drawdown_pct) / 100 * budget:
            halted = True
            if pending:
                rejections["loss limits: drawdown (entry cancelled)"] += len(pending)
            pending.clear()
        last_equity = eq
        blocked = "loss limits: drawdown" if halted else None
        if blocked is None and pause is not None and placement < pause[0]:
            blocked = pause[1]
        weak = {m: data.regime_block(m, day, p) for m in markets}

        # 5. New signals at today's close, best first
        candidates: list[tuple[float, InstrumentData, int, TradePlan, str]] = []
        busy = {x.data.id for x in pending} | {x.data.id for x in open_}
        if external is not None:
            for sig in external.get(day, ()):
                x = data.instruments.get(sig.instrument_id)
                row = x.rows.get(day) if x is not None else None
                if x is None or row is None or x.instrument.market not in markets:
                    continue
                signals += 1
                if why := weak[x.instrument.market] or _not_admitted(x, day, busy, p, blacklist):
                    rejections[why] += 1
                    continue
                candidates.append((sig.score, x, row, sig.plan, sig.strategy))
        for x in data.instruments.values() if external is None else ():
            row = x.rows.get(day)
            if row is None or not bool(x.setup.iloc[row]) or x.instrument.market not in markets:
                continue
            signals += 1
            if why := weak[x.instrument.market] or _not_admitted(x, day, busy, p, blacklist):
                rejections[why] += 1
                continue
            plan = pullback.plan_trade(x.frame.iloc[: row + 1], float(x.ind["atr"].iloc[row]), p)
            if plan is None:
                rejections["no valid stop"] += 1
                continue
            rs = float(x.ind["rs"].iloc[row])
            candidates.append((rs if math.isfinite(rs) else -math.inf, x, row, plan, pullback.NAME))
        candidates.sort(key=lambda c: c[0], reverse=True)
        held_returns = (
            {o.data.id: o.data.recent_returns(day) for o in pending}
            | {pos.data.id: pos.data.recent_returns(day) for pos in open_}
            if candidates
            else {}
        )

        placed = 0
        for _, x, row, plan, strategy in candidates:
            inst = x.instrument
            if blocked is not None:
                rejections[blocked] += 1
                continue
            if len(open_) + len(pending) >= risk.portfolio.max_open_positions:
                rejections["max open positions"] += 1
                continue
            if placed >= risk.execution.max_orders_per_day:
                rejections["max orders per day"] += 1
                continue
            rate = data.rate(inst.currency, day)
            available_eur = cash - sum(o.reserved_eur for o in pending)
            entry, stop = to_decimal(plan.entry), to_decimal(plan.stop)
            sizing = position_size(
                entry=entry,
                stop=stop,
                settled_cash=to_decimal(max(available_eur, 0) * rate, 2),
                eur_rate=Decimal(str(rate)),
                limits=limits,
            )
            if sizing.quantity == 0:
                rejections[f"sizing: {sizing.rejection}"] += 1
                continue
            q = sizing.quantity
            target = to_decimal(plan.target)
            fee_cap = risk.per_trade.max_fee_to_risk_pct / 100 * sizing.risk
            if round_trip_fees(fees, inst.market, q, entry, target) > fee_cap:
                rejections["fees above limit"] += 1
                continue
            value_eur = float(q) * plan.entry / rate
            same_sector = sum(
                o.reserved_eur for o in pending if o.data.instrument.sector == inst.sector
            ) + sum(pos.cost_eur for pos in open_ if pos.data.instrument.sector == inst.sector)
            if same_sector + value_eur > float(risk.portfolio.max_sector_pct) / 100 * budget:
                rejections["sector cap"] += 1
                continue
            mine = x.recent_returns(day)
            holdings = [(o.data.id, o.reserved_eur) for o in pending] + [
                (pos.data.id, pos.cost_eur) for pos in open_
            ]
            # As in the risk engine: a holding without a correlation estimate counts as correlated.
            cluster = value_eur + sum(
                value
                for other, value in holdings
                if (rho := correlation(mine, held_returns[other])) is None
                or rho > engine.CLUSTER_RHO
            )
            if cluster > float(risk.portfolio.max_correlated_cluster_pct) / 100 * budget:
                rejections["correlated cluster"] += 1
                continue
            buy_fee = float(order_fees(fees, inst.market, "buy", q, entry))
            pending.append(
                _Pending(x, day, row, plan, q, (float(q) * plan.entry + buy_fee) / rate, strategy)
            )
            held_returns[x.id] = mine
            placed += 1

    trades += [pos.trade(book) for pos in open_]
    return BacktestResult(trades, _series(equity), _series(invested), rejections, signals)


def simulate_trade(
    data: MarketData,
    x: InstrumentData,
    signal_row: int,
    plan: TradePlan,
    quantity: Decimal,
    cfg: BacktestConfig,
    fees: FeeSchedule,
    *,
    end: date,
    book: Book,
    strategy: str,
) -> Trade | None:
    """One trade on its own, with `run`'s fill, exit and stop rules; None if the entry
    didn't fill within its validity (or hasn't yet by `end`)."""
    p, slip, inst = cfg.pullback, cfg.slippage_pct, x.instrument
    entry: tuple[int, date, float, float] | None = None  # row, day, price, buy fee
    stop = plan.stop
    highest_close = 0.0

    def done(
        entry: tuple[int, date, float, float], row: int, day: date, fill: sim.Fill | None
    ) -> Trade:
        e_row, e_day, e_price, buy_fee = entry
        e_rate = data.rate(inst.currency, e_day)
        cost_eur = (float(quantity) * e_price + buy_fee) / e_rate
        sell_fee = 0.0
        pnl = r = None
        if fill is not None:
            sell_fee = float(
                order_fees(fees, inst.market, "sell", quantity, to_decimal(fill.price))
            )
            x_rate = data.rate(inst.currency, day)
            pnl = (float(quantity) * fill.price - sell_fee) / x_rate - cost_eur
        risk_eur = float(quantity) * plan.risk_per_share / e_rate
        if pnl is not None and risk_eur > 0:
            r = pnl / risk_eur
        return Trade(
            book=book,
            strategy=strategy,
            instrument_id=x.id,
            yahoo_symbol=inst.yahoo_symbol,
            market=inst.market,
            sector=inst.sector,
            signal_date=pd.Timestamp(x.frame.index[signal_row]).date(),
            entry_date=e_day,
            entry_price=e_price,
            quantity=float(quantity),
            stop=plan.stop,
            target=plan.target,
            risk_eur=risk_eur,
            fees=buy_fee + sell_fee,
            fees_eur=buy_fee / e_rate + sell_fee / data.rate(inst.currency, day),
            exit_date=day if fill else None,
            exit_price=fill.price if fill else None,
            exit_reason=fill.reason if fill else None,
            pnl_net_eur=pnl,
            r_multiple=r,
            holding_sessions=row - e_row if fill else None,
        )

    last_row, last_day = signal_row, pd.Timestamp(x.frame.index[signal_row]).date()
    for row in range(signal_row + 1, len(x.frame)):
        day = pd.Timestamp(x.frame.index[row]).date()
        if day > end:
            break
        last_row, last_day = row, day
        bar = x.bar(row)
        if entry is None:
            if row - signal_row > p.entry_valid_sessions:
                return None
            price = sim.fill_entry(bar, plan.entry, slip)
            if price is None:
                continue
            fee = float(order_fees(fees, inst.market, "buy", quantity, to_decimal(price)))
            entry = (row, day, price, fee)
            highest_close = bar.close
            stopped = sim.check_exit(bar, stop, None, slip)
            if stopped is not None:
                return done(entry, row, day, stopped)
            continue
        fill = sim.check_exit(bar, stop, plan.target, slip)
        if fill is None and row - entry[0] >= p.time_stop_sessions:
            fill = sim.time_exit(bar, slip)
        if fill is not None:
            return done(entry, row, day, fill)
        highest_close = max(highest_close, bar.close)
        stop = _next_stop(x, row, stop, entry[2], plan, highest_close, p)
    return done(entry, last_row, last_day, None) if entry is not None else None


def shadow_book(
    data: MarketData,
    cfg: BacktestConfig,
    risk: RiskConfig,
    fees: FeeSchedule,
    proposals: Sequence[Proposal],
    *,
    end: date,
) -> tuple[list[Trade], Counter[str]]:
    """`agent_shadow` (CONCEPT.md 13): every proposal the risk engine would approve on an
    empty account, i.e. its per-trade checks without portfolio capacity, each simulated alone."""
    trades: list[Trade] = []
    rejections: Counter[str] = Counter()
    for prop in proposals:
        x = data.instruments.get(prop.instrument_id)
        row = x.rows.get(prop.as_of) if x is not None else None
        if prop.status != "proposed" or x is None or row is None or row + 1 >= len(x.frame):
            continue
        inst = x.instrument
        day = pd.Timestamp(x.frame.index[row + 1]).date()  # the session it would be placed
        if day > end:
            continue
        _, sleeve = next(
            (ms, c)
            for ms, c in risk.sleeves([*risk.markets.paper, inst.market], "paper")
            if inst.market in ms
        )
        now = datetime.combine(day, time(12), UTC)
        cal = calendars.CALENDAR_BY_MARKET[inst.market]
        upcoming = [d for d in x.earnings if d >= day]
        market = MarketSnapshot(
            instrument_id=x.id,
            in_universe=bool(inst.indices),
            session_open=now - timedelta(hours=1),  # replay: the trading window is not checked
            session_close=now + timedelta(hours=1),
            mid=to_decimal(float(x.frame["close"].iloc[row])),
            atr=to_decimal(float(x.ind["atr"].iloc[row])),
            avg_daily_value=to_decimal(float(x.ind["avg_dollar_volume"].iloc[row]), 0),
            sessions_to_earnings=len(calendars.sessions(cal, day, min(upcoming))) - 1
            if upcoming
            else None,
            eur_rate=Decimal(str(data.rate(inst.currency, prop.as_of))),
            correlations={},
        )
        empty = PortfolioState(
            holdings=(),
            settled_cash_eur=sleeve.capital.agent_budget_eur,
            pnl_today_eur=Decimal(0),
            pnl_week_eur=Decimal(0),
            drawdown_eur=Decimal(0),
            orders_today=0,
        )
        controls = Controls(
            trading="active", app_mode="paper", gateway_mode=None, live_confirmed=False
        )
        decision = engine.evaluate(
            prop, proposal_levels(prop), empty, market, controls, sleeve, fees, now
        )
        entry, stop, target = decision.entry, decision.stop, decision.target
        if not decision.approved or entry is None or stop is None or target is None:
            rejections[decision.failures[0].name if decision.failures else "rejected"] += 1
            continue
        plan = TradePlan(float(entry), float(stop), float(target))
        trade = simulate_trade(
            data,
            x,
            row,
            plan,
            decision.quantity,
            cfg,
            fees,
            end=end,
            book="agent_shadow",
            strategy=prop.strategy,
        )
        if trade is None:
            rejections["entry not filled"] += 1
        else:
            trades.append(trade)
    return trades, rejections


def latest_setups(data: MarketData, p: PullbackParams) -> list[tuple[str, float]]:
    """(symbol, relative strength) with a complete setup at the latest close, best first."""
    latest: dict[Market, date] = {}
    for x in data.instruments.values():
        if not x.frame.empty:
            last = pd.Timestamp(x.frame.index[-1]).date()
            latest[x.instrument.market] = max(latest.get(x.instrument.market, last), last)
    found: list[tuple[str, float]] = []
    for x in data.instruments.values():
        if x.frame.empty or not bool(x.setup.iloc[-1]):
            continue
        day = pd.Timestamp(x.frame.index[-1]).date()
        if day != latest[x.instrument.market]:  # stale data
            continue
        if data.regime_block(x.instrument.market, day, p):
            continue
        cal = calendars.CALENDAR_BY_MARKET[x.instrument.market]
        if not pullback.earnings_clear(
            day, calendars.session_offset(cal, day, p.earnings_buffer_sessions), x.earnings
        ):
            continue
        rs = float(x.ind["rs"].iloc[-1])
        found.append((x.instrument.yahoo_symbol, rs if math.isfinite(rs) else -math.inf))
    return sorted(found, key=lambda f: f[1], reverse=True)


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
