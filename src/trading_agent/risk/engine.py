"""Risk engine (IMPLEMENTATION.md 9): pure, no I/O, no LLM.

Every check runs and is recorded in order; a check that needs a value from a failed one is
skipped. The decision is approved only if every check passes.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from trading_agent.calc.fees import FeeSchedule, round_trip_fees
from trading_agent.calc.sizing import Sizing, position_size
from trading_agent.domain.proposals import Proposal
from trading_agent.domain.risk import (
    Check,
    CheckName,
    Controls,
    MarketSnapshot,
    Mode,
    PortfolioState,
    RiskDecision,
    Trip,
)
from trading_agent.risk.config import RiskConfig

CLUSTER_RHO = 0.7  # holdings correlated above this count as one cluster (risk.yaml comment)


@dataclass(frozen=True)
class _Plan:
    entry: Decimal
    stop: Decimal
    target: Decimal


def _check(name: CheckName, failures: list[str], passed: str = "") -> Check:
    if failures:
        return Check(name=name, outcome="fail", detail="; ".join(failures))
    return Check(name=name, outcome="pass", detail=passed)


def _skip(name: CheckName) -> Check:
    return Check(name=name, outcome="skip", detail="depends on a failed check")


def _proposal(proposal: Proposal) -> Check:
    failures = [] if proposal.status == "proposed" else [f"status is {proposal.status}"]
    return _check("proposal", failures)


def _global(controls: Controls, market: MarketSnapshot, limits: RiskConfig, now: datetime) -> Check:
    failures: list[str] = []
    if controls.trading != "active":
        failures.append(f"trading is {controls.trading}")
    if controls.app_mode == "live":
        if controls.gateway_mode != "live":
            failures.append("live mode needs a live gateway")
        if not controls.live_confirmed:
            failures.append("live mode not confirmed (/confirm_live)")
    elif controls.gateway_mode == "live":
        failures.append("paper mode on a live gateway")
    if market.session_open is None or market.session_close is None:
        failures.append("market closed today")
    else:
        first = timedelta(minutes=limits.execution.no_trading_first_minutes)
        last = timedelta(minutes=limits.execution.no_trading_last_minutes)
        if not market.session_open + first <= now <= market.session_close - last:
            failures.append("outside the trading window")
    return _check("global", failures)


def _instrument(
    proposal: Proposal, market: MarketSnapshot, mode: Mode, limits: RiskConfig
) -> Check:
    failures: list[str] = []
    allowed = limits.markets.live if mode == "live" else limits.markets.paper
    if proposal.market not in allowed:
        failures.append(f"{proposal.market} not allowed in {mode}")
    if not market.in_universe:
        failures.append("not in the universe")
    if market.mid < limits.instruments.min_price:
        failures.append(f"price {market.mid} below {limits.instruments.min_price}")
    if market.avg_daily_value < limits.instruments.min_avg_daily_dollar_volume:
        failures.append(f"average daily value {market.avg_daily_value:.0f} too low")
    buffer = limits.execution.no_new_entries_before_earnings_days
    if market.sessions_to_earnings is not None and market.sessions_to_earnings <= buffer:
        failures.append(f"earnings in {market.sessions_to_earnings} sessions")
    return _check("instrument", failures)


def _levels(
    proposal: Proposal, levels: Mapping[str, Decimal], market: MarketSnapshot, limits: RiskConfig
) -> tuple[Check, _Plan | None]:
    refs = {"entry": proposal.entry_ref, "stop": proposal.stop_ref, "target": proposal.target_ref}
    missing = [k for k, ref in refs.items() if ref is None or ref not in levels]
    if missing:
        return _check("levels", [f"unresolved {', '.join(missing)} ref"]), None
    entry, stop, target = (levels[str(ref)] for ref in refs.values())
    rules = limits.per_trade
    failures: list[str] = []
    if not stop < entry < target:
        failures.append("levels not ordered stop < entry < target")
    else:
        rr = (target - entry) / (entry - stop)
        if rr < rules.min_risk_reward:
            failures.append(f"R:R {rr:.2f} below {rules.min_risk_reward}")
        if market.atr <= 0:
            failures.append("no ATR")
        elif not rules.stop_atr_min <= (entry - stop) / market.atr <= rules.stop_atr_max:
            failures.append(f"stop {(entry - stop) / market.atr:.2f} ATR outside the bounds")
    if entry > market.mid * (1 + rules.max_limit_deviation_pct / 100):
        failures.append(f"entry more than {rules.max_limit_deviation_pct} % above mid")
    if market.mid <= stop:
        failures.append("price at or below the stop")
    return _check("levels", failures), None if failures else _Plan(entry, stop, target)


def _sizing(
    plan: _Plan, portfolio: PortfolioState, market: MarketSnapshot, limits: RiskConfig
) -> tuple[Check, Sizing | None]:
    sizing = position_size(
        entry=plan.entry,
        stop=plan.stop,
        settled_cash=portfolio.settled_cash_eur * market.eur_rate,
        eur_rate=market.eur_rate,
        limits=limits.sizing_limits(),
    )
    if sizing.quantity == 0:
        return _check("sizing", [str(sizing.reason)]), None
    return _check("sizing", [], f"{sizing.quantity} shares, {sizing.binding} limit binds"), sizing


def _fees(
    proposal: Proposal, plan: _Plan, sizing: Sizing, limits: RiskConfig, fees: FeeSchedule
) -> tuple[Check, Decimal]:
    estimate = round_trip_fees(fees, proposal.market, sizing.quantity, plan.entry, plan.target)
    cap = limits.per_trade.max_fee_to_risk_pct / 100 * sizing.risk
    failures = [f"fees {estimate} above {cap:.2f}"] if estimate > cap else []
    return _check("fees", failures, f"fees {estimate}"), estimate


def _portfolio(
    proposal: Proposal,
    sizing: Sizing | None,
    portfolio: PortfolioState,
    market: MarketSnapshot,
    limits: RiskConfig,
) -> Check:
    rules = limits.portfolio
    failures: list[str] = []
    if len(portfolio.holdings) >= rules.max_open_positions:
        failures.append(f"{len(portfolio.holdings)} positions and entries open")
    if sizing is not None:
        value_eur = sizing.value / market.eur_rate
        budget = limits.capital.agent_budget_eur
        sector = value_eur + sum(
            (h.value_eur for h in portfolio.holdings if h.sector == proposal.sector), Decimal(0)
        )
        if sector > rules.max_sector_pct / 100 * budget:
            failures.append(f"sector {proposal.sector} would be {sector:.2f} EUR")
        # A holding without a correlation estimate counts as correlated.
        cluster = value_eur + sum(
            (
                h.value_eur
                for h in portfolio.holdings
                if market.correlations.get(h.instrument_id, 1.0) > CLUSTER_RHO
            ),
            Decimal(0),
        )
        if cluster > rules.max_correlated_cluster_pct / 100 * budget:
            failures.append(f"correlated cluster would be {cluster:.2f} EUR")
    elif not failures:
        return _skip("portfolio")
    return _check("portfolio", failures)


def _loss_limits(portfolio: PortfolioState, limits: RiskConfig) -> tuple[Check, Trip | None]:
    budget = limits.capital.agent_budget_eur
    rules = limits.loss_limits
    failures: list[str] = []
    trip: Trip | None = None
    if portfolio.pnl_today_eur <= -rules.daily_loss_pct / 100 * budget:
        failures.append(f"daily loss limit ({portfolio.pnl_today_eur:.2f} EUR)")
        trip = "pause_day"
    if portfolio.pnl_week_eur <= -rules.weekly_loss_pct / 100 * budget:
        failures.append(f"weekly loss limit ({portfolio.pnl_week_eur:.2f} EUR)")
        trip = "pause_week"
    if portfolio.drawdown_eur >= rules.max_drawdown_pct / 100 * budget:
        failures.append(f"drawdown limit ({portfolio.drawdown_eur:.2f} EUR)")
        trip = "halt"
    return _check("loss_limits", failures), trip


def _rate_limits(proposal: Proposal, portfolio: PortfolioState, limits: RiskConfig) -> Check:
    failures: list[str] = []
    if portfolio.orders_today >= limits.execution.max_orders_per_day:
        failures.append(f"{portfolio.orders_today} orders today")
    if any(h.instrument_id == proposal.instrument_id for h in portfolio.holdings):
        failures.append("already held or pending")
    return _check("rate_limits", failures)


def evaluate(
    proposal: Proposal,
    levels: Mapping[str, Decimal],
    portfolio: PortfolioState,
    market: MarketSnapshot,
    controls: Controls,
    limits: RiskConfig,
    fees: FeeSchedule,
    now: datetime,
) -> RiskDecision:
    """Decides one long entry. `limits` is the RiskConfig of the proposal's budget sleeve."""
    if market.instrument_id != proposal.instrument_id:
        raise ValueError("market snapshot is for another instrument")
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    checks = [
        _proposal(proposal),
        _global(controls, market, limits, now),
        _instrument(proposal, market, controls.app_mode, limits),
    ]
    levels_check, plan = _levels(proposal, levels, market, limits)
    checks.append(levels_check)
    sizing: Sizing | None = None
    estimate = Decimal(0)
    if plan is None:
        checks += [_skip("sizing"), _skip("fees")]
    else:
        sizing_check, sizing = _sizing(plan, portfolio, market, limits)
        checks.append(sizing_check)
        if sizing is None:
            checks.append(_skip("fees"))
        else:
            fees_check, estimate = _fees(proposal, plan, sizing, limits, fees)
            checks.append(fees_check)
    checks.append(_portfolio(proposal, sizing, portfolio, market, limits))
    loss_check, trip = _loss_limits(portfolio, limits)
    checks += [loss_check, _rate_limits(proposal, portfolio, limits)]

    if plan is None or sizing is None or any(c.outcome != "pass" for c in checks):
        return RiskDecision(approved=False, checks=tuple(checks), trip=trip)
    return RiskDecision(
        approved=True,
        checks=tuple(checks),
        trip=trip,
        quantity=sizing.quantity,
        entry=plan.entry,
        stop=plan.stop,
        target=plan.target,
        risk=sizing.risk,
        value=sizing.value,
        fees=estimate,
    )
