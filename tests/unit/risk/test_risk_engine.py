"""Risk engine: one test per rule, plus property tests that no approval can break a limit."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from hypothesis import find, given, settings
from hypothesis import strategies as st

from trading_agent.calc.fees import round_trip_fees
from trading_agent.domain.market import Market
from trading_agent.domain.proposals import Proposal
from trading_agent.domain.risk import (
    Controls,
    Holding,
    MarketSnapshot,
    PortfolioState,
    RiskDecision,
)
from trading_agent.risk.config import RiskConfig, load_risk_config
from trading_agent.risk.engine import CLUSTER_RHO, evaluate, loss_trips
from trading_agent.settings import load_fees

D = Decimal
CONFIG = Path(__file__).resolve().parents[3] / "config"
FEES = load_fees(CONFIG)
REPO = load_risk_config(CONFIG)
# The rules below are tested against fixed-size limits (confidence has no effect) in a EUR 1,000
# US and a EUR 5,000 EU sleeve; the confidence scaling has its own tests with the repo's values.
FIXED = REPO.model_copy(
    update={
        "capital": REPO.capital.model_copy(
            update={"paper_budget_eur": {"US": D(1000), "EU": D(5000)}}
        ),
        "per_trade": REPO.per_trade.model_copy(
            update={
                "min_risk_pct": D("1.5"),
                "max_risk_pct": D("1.5"),
                "min_position_pct": D(30),
                "max_position_pct": D(30),
                "min_position_eur": D(200),
            }
        ),
        "portfolio": REPO.portfolio.model_copy(update={"max_open_positions": 4}),
        "execution": REPO.execution.model_copy(update={"max_orders_per_day": 6}),
    }
)
SLEEVES: dict[Market, RiskConfig] = {ms[0]: cfg for ms, cfg in FIXED.sleeves(["US", "EU"], "paper")}
OPEN = datetime(2026, 10, 5, 13, 30, tzinfo=UTC)  # NYSE 09:30 New York
CLOSE = datetime(2026, 10, 5, 20, 0, tzinfo=UTC)
NOW = OPEN + timedelta(minutes=30)

PROPOSAL = Proposal(
    id=uuid4(),
    source="agent",
    as_of=date(2026, 10, 2),
    instrument_id=1,
    yahoo_symbol="AAPL",
    market="US",
    sector="Technology",
    status="proposed",
    strategy="agent",
    entry_ref="ema20",
    stop_ref="swing_low",
    target_ref="resistance",
    thesis="pullback to the EMA20 in an uptrend",
    invalidation="close below the swing low",
)
LEVELS = {"ema20": D(100), "swing_low": D(96), "resistance": D(108)}
PORTFOLIO = PortfolioState(
    holdings=(),
    settled_cash_eur=D(900),
    pnl_today_eur=D(0),
    pnl_week_eur=D(0),
    drawdown_eur=D(0),
    orders_today=0,
)
MARKET = MarketSnapshot(
    instrument_id=1,
    in_universe=True,
    session_open=OPEN,
    session_close=CLOSE,
    mid=D("100.50"),
    atr=D(2),
    avg_daily_value=D(1_000_000_000),
    sessions_to_earnings=None,
    eur_rate=D("1.10"),
    correlations={},
)
PAPER = Controls(trading="active", app_mode="paper", gateway_mode="paper", live_confirmed=False)


def run(
    *,
    proposal: dict[str, Any] | None = None,
    levels: dict[str, Decimal] | None = None,
    portfolio: dict[str, Any] | None = None,
    market: dict[str, Any] | None = None,
    controls: dict[str, Any] | None = None,
    now: datetime = NOW,
) -> RiskDecision:
    p = PROPOSAL.model_copy(update=proposal or {})
    return evaluate(
        p,
        LEVELS if levels is None else levels,
        PORTFOLIO.model_copy(update=portfolio or {}),
        MARKET.model_copy(update=market or {}),
        PAPER.model_copy(update=controls or {}),
        SLEEVES[p.market],
        FEES,
        now,
    )


def outcome(decision: RiskDecision, name: str) -> tuple[str, str]:
    check = next(c for c in decision.checks if c.name == name)
    return check.outcome, check.detail


def test_approves_a_clean_proposal() -> None:
    # B = 1100 USD: risk cap 16.5 / 4 = 4.125, position cap 330 / 100 = 3.3 shares
    d = run()
    assert d.approved
    assert d.failures == []
    assert [c.name for c in d.checks] == [
        "proposal",
        "global",
        "instrument",
        "levels",
        "sizing",
        "fees",
        "portfolio",
        "loss_limits",
        "rate_limits",
    ]
    assert (d.quantity, d.entry, d.stop, d.target) == (D("3.3"), D(100), D(96), D(108))
    assert (d.risk, d.value, d.fees) == (D("13.2"), D(330), D("0.75"))
    assert d.trip is None
    assert outcome(d, "sizing") == ("pass", "3.3 shares, position limit binds, conviction 0%")


def test_eu_uses_its_own_sleeve() -> None:
    # EU sleeve EUR 5,000: risk cap 75 / 4 = 18.75, position cap 1500 / 100 = 15 -> 15 shares
    d = run(
        proposal={"market": "EU", "yahoo_symbol": "SAP.DE"},
        portfolio={"settled_cash_eur": D(5000)},
        market={"eur_rate": D(1)},
    )
    assert d.approved
    assert d.quantity == 15


@pytest.mark.parametrize(
    ("confidence", "quantity", "detail"),
    [
        (0.2, D(11), "11 shares, position limit binds, conviction 0%"),  # 10 % of 11,000 USD
        (0.45, D("19.25"), "19.25 shares, position limit binds, conviction 50%"),
        (0.6, D("27.5"), "27.5 shares, position limit binds, conviction 100%"),
        (0.9, D("27.5"), "27.5 shares, position limit binds, conviction 100%"),
        (None, D(11), "11 shares, position limit binds, conviction 0%"),
    ],
)
def test_the_more_confident_the_bigger_the_position(
    confidence: float | None, quantity: Decimal, detail: str
) -> None:
    us = {ms[0]: cfg for ms, cfg in REPO.sleeves(["US", "EU"], "paper")}["US"]
    d = evaluate(
        PROPOSAL.model_copy(update={"confidence": confidence}),
        LEVELS,
        PORTFOLIO.model_copy(update={"settled_cash_eur": D(10000)}),
        MARKET,
        PAPER,
        us,
        FEES,
        NOW,
    )
    assert d.approved
    assert d.quantity == quantity
    assert outcome(d, "sizing") == ("pass", detail)


@pytest.mark.parametrize(
    ("change", "check", "detail"),
    [
        ({"proposal": {"status": "no_trade"}}, "proposal", "status is no_trade"),
        ({"controls": {"trading": "paused"}}, "global", "trading is paused"),
        ({"controls": {"trading": "halted"}}, "global", "trading is halted"),
        ({"controls": {"gateway_mode": "live"}}, "global", "paper mode on a live gateway"),
        ({"controls": {"app_mode": "live"}}, "global", "live mode needs a live gateway"),
        (
            {"controls": {"app_mode": "live", "gateway_mode": "live"}},
            "global",
            "live mode not confirmed",
        ),
        ({"market": {"session_open": None}}, "global", "market closed today"),
        ({"market": {"session_close": None}}, "global", "market closed today"),
        ({"now": OPEN + timedelta(minutes=14)}, "global", "outside the trading window"),
        ({"now": CLOSE - timedelta(minutes=9)}, "global", "outside the trading window"),
        ({"market": {"in_universe": False}}, "instrument", "not in the universe"),
        ({"market": {"mid": D("4.99")}}, "instrument", "price 4.99 below 5"),
        ({"market": {"avg_daily_value": D(19_999_999)}}, "instrument", "average daily value"),
        ({"market": {"sessions_to_earnings": 3}}, "instrument", "earnings in 3 sessions"),
        ({"market": {"sessions_to_earnings": 0}}, "instrument", "earnings in 0 sessions"),
        ({"proposal": {"entry_ref": "nowhere"}}, "levels", "unresolved entry ref"),
        ({"proposal": {"stop_ref": None}}, "levels", "unresolved stop ref"),
        ({"levels": {**LEVELS, "swing_low": D(101)}}, "levels", "not ordered"),
        ({"levels": {**LEVELS, "resistance": D(100)}}, "levels", "not ordered"),
        ({"levels": {**LEVELS, "resistance": D(107)}}, "levels", "R:R 1.75 below 2.0"),
        ({"market": {"atr": D(0)}}, "levels", "no ATR"),
        ({"market": {"atr": D(5)}}, "levels", "stop 0.80 ATR outside the bounds"),
        ({"market": {"atr": D("0.9")}}, "levels", "stop 4.44 ATR outside the bounds"),
        ({"market": {"mid": D(98)}}, "levels", "entry more than 1.0 % above mid"),
        ({"market": {"mid": D(96)}}, "levels", "price at or below the stop"),
        ({"portfolio": {"settled_cash_eur": D(100)}}, "sizing", "no capacity (cash limit"),
        ({"portfolio": {"settled_cash_eur": D(290)}}, "sizing", "below the minimum size"),
        # risk 3.3 x 1 USD: fee cap 0.33 < 0.75
        (
            {
                "levels": {**LEVELS, "swing_low": D(99), "resistance": D(102)},
                "market": {"atr": D("0.5")},
            },
            "fees",
            "fees 0.75 above 0.33",
        ),
        ({"portfolio": {"orders_today": 6}}, "rate_limits", "6 orders today"),
    ],
)
def test_each_rule_rejects(change: dict[str, Any], check: str, detail: str) -> None:
    d = run(**change)
    assert not d.approved
    assert (d.quantity, d.entry, d.risk) == (0, None, D(0))
    got, text = outcome(d, check)
    assert got == "fail"
    assert detail in text


@pytest.mark.parametrize(
    "change",
    [
        {"now": OPEN + timedelta(minutes=15)},
        {"now": CLOSE - timedelta(minutes=10)},
        {"market": {"sessions_to_earnings": 4}},
        {"market": {"mid": D("99.01")}},  # entry 100 is exactly 1 % above
        {"market": {"mid": D(110)}},  # a resting limit far below the price is fine
        {"market": {"atr": D(4)}},  # stop exactly 1 ATR
        {"market": {"atr": D(1)}},  # stop exactly 4 ATR
        {"portfolio": {"pnl_today_eur": D("-29.99"), "pnl_week_eur": D("-59.99")}},
        {"portfolio": {"drawdown_eur": D("149.99"), "orders_today": 5}},
        {"controls": {"app_mode": "live", "gateway_mode": "live", "live_confirmed": True}},
    ],
)
def test_boundaries_pass(change: dict[str, Any]) -> None:
    assert run(**change).approved


def test_live_mode_allows_us_only() -> None:
    live = {"app_mode": "live", "gateway_mode": "live", "live_confirmed": True}
    d = run(
        proposal={"market": "EU"},
        portfolio={"settled_cash_eur": D(5000)},
        market={"eur_rate": D(1)},
        controls=live,
    )
    assert outcome(d, "instrument") == ("fail", "EU not allowed in live")


def test_blacklisted_symbol_is_rejected() -> None:
    us = SLEEVES["US"]
    banned = us.model_copy(
        update={"instruments": us.instruments.model_copy(update={"blacklist": ["AAPL"]})}
    )
    d = evaluate(PROPOSAL, LEVELS, PORTFOLIO, MARKET, PAPER, banned, FEES, NOW)
    assert outcome(d, "instrument") == ("fail", "blacklisted")


def test_dependent_checks_are_skipped() -> None:
    d = run(proposal={"target_ref": None})
    assert [c.outcome for c in d.checks] == [
        "pass",
        "pass",
        "pass",
        "fail",
        "skip",
        "skip",
        "skip",
        "pass",
        "pass",
    ]
    d = run(portfolio={"settled_cash_eur": D(0)})
    assert [outcome(d, n)[0] for n in ("sizing", "fees", "portfolio")] == ["fail", "skip", "skip"]


def holding(instrument_id: int, sector: str | None, value: str) -> Holding:
    return Holding(instrument_id=instrument_id, sector=sector, value_eur=D(value))


def test_max_open_positions_counts_pending_entries() -> None:
    four = tuple(holding(i, f"S{i}", "100") for i in range(2, 6))
    corr = {i: 0.0 for i in range(2, 6)}
    d = run(portfolio={"holdings": four}, market={"correlations": corr})
    assert outcome(d, "portfolio") == ("fail", "4 positions and entries open")
    # also reported when the proposal's levels are broken
    d = run(portfolio={"holdings": four}, proposal={"entry_ref": None})
    assert outcome(d, "portfolio")[0] == "fail"


def test_sector_cap() -> None:
    # new 330 USD = 300 EUR; 300 + 330 > 600
    d = run(
        portfolio={"holdings": (holding(2, "Technology", "330"),)},
        market={"correlations": {2: 0.0}},
    )
    assert outcome(d, "portfolio") == ("fail", "sector Technology would be 630.00 EUR")
    d = run(
        portfolio={"holdings": (holding(2, "Technology", "290"),)},
        market={"correlations": {2: 0.0}},
    )
    assert d.approved


def test_correlated_cluster_cap() -> None:
    other = (holding(2, "Energy", "330"),)
    d = run(portfolio={"holdings": other}, market={"correlations": {2: 0.71}})
    assert outcome(d, "portfolio") == ("fail", "correlated cluster would be 630.00 EUR")
    # without an estimate the holding counts as correlated
    assert not run(portfolio={"holdings": other}).approved
    assert run(portfolio={"holdings": other}, market={"correlations": {2: CLUSTER_RHO}}).approved


@pytest.mark.parametrize(
    ("portfolio", "trip"),
    [
        ({"pnl_today_eur": D(-30)}, "pause_day"),
        ({"pnl_week_eur": D(-60)}, "pause_week"),
        ({"pnl_today_eur": D(-30), "pnl_week_eur": D(-60)}, "pause_week"),
        ({"drawdown_eur": D(150)}, "halt"),
        ({"pnl_today_eur": D(-40), "drawdown_eur": D(160)}, "halt"),
    ],
)
def test_loss_limits_trip_the_kill_switch(portfolio: dict[str, Any], trip: str) -> None:
    d = run(portfolio=portfolio)
    assert not d.approved
    assert outcome(d, "loss_limits")[0] == "fail"
    assert d.trip == trip


def test_trip_is_reported_even_outside_the_window() -> None:
    d = run(portfolio={"drawdown_eur": D(150)}, market={"session_open": None})
    assert d.trip == "halt"


@pytest.mark.parametrize(
    ("portfolio", "trips"),
    [
        ({"pnl_today_eur": D("-29.99"), "pnl_week_eur": D("-59.99")}, []),
        ({"pnl_today_eur": D(-30)}, ["pause_day"]),
        (
            {"pnl_today_eur": D(-30), "pnl_week_eur": D(-60), "drawdown_eur": D(150)},
            ["pause_day", "pause_week", "halt"],
        ),
    ],
)
def test_loss_trips_at_the_close_lists_every_breach(
    portfolio: dict[str, Any], trips: list[str]
) -> None:
    assert loss_trips(PORTFOLIO.model_copy(update=portfolio), SLEEVES["US"]) == trips


def test_duplicate_instrument_is_rejected() -> None:
    d = run(portfolio={"holdings": (holding(1, "Technology", "100"),)})
    assert outcome(d, "rate_limits") == ("fail", "already held or pending")


def test_programming_errors_raise() -> None:
    with pytest.raises(ValueError, match="another instrument"):
        run(market={"instrument_id": 2})
    with pytest.raises(ValueError, match="timezone-aware"):
        run(now=datetime(2026, 10, 5, 14, 0))  # noqa: DTZ001


# --- Property tests -----------------------------------------------------------------------


def dec(lo: str, hi: str, places: int = 2) -> st.SearchStrategy[Decimal]:
    return st.decimals(min_value=D(lo), max_value=D(hi), places=places)


def mostly(
    valid: st.SearchStrategy[Any], anything: st.SearchStrategy[Any]
) -> st.SearchStrategy[Any]:
    """Usually a value inside the limits, sometimes one from the wider range around them."""
    return st.sampled_from(range(10)).flatmap(lambda i: valid if i < 9 else anything)


CONTROLS = [
    ("paper", "paper", False),
    ("paper", None, False),
    ("live", "live", True),
    ("paper", "live", False),
    ("live", "paper", True),
    ("live", "live", False),
]


@st.composite
def scenarios(draw: st.DrawFn) -> dict[str, Any]:
    """Most draws fall inside the limits so approvals are common; the rest probe the edges."""
    market = draw(st.sampled_from(["US", "US", "EU"]))
    entry = draw(mostly(dec("10", "300"), dec("3", "1500")))
    risk_pct = draw(mostly(dec("0.025", "0.05", 3), dec("0.003", "0.15", 3)))
    stop = (entry * (1 - risk_pct)).quantize(D("0.01"))
    atr = ((entry - stop) / draw(mostly(dec("1", "4"), dec("0.5", "5")))).quantize(D("0.0001"))
    target = (entry + (entry - stop) * draw(mostly(dec("2", "4"), dec("1.5", "4")))).quantize(
        D("0.01")
    )
    deviation = draw(mostly(dec("-0.0099", "0.05", 4), dec("-0.05", "0.05", 4)))  # mid vs entry
    mid = (entry * (1 + deviation)).quantize(D("0.01"))
    positions = draw(
        st.lists(
            st.tuples(
                mostly(st.integers(2, 40), st.integers(1, 3)),
                st.sampled_from(["Technology", "Energy", None]),
                mostly(dec("0", "250"), dec("0", "1500")),
                st.none() | st.floats(0, 1),
            ),
            max_size=draw(mostly(st.just(2), st.just(5))),
        )
    )
    mode, gateway, confirmed = draw(
        mostly(st.sampled_from(CONTROLS[:3]), st.sampled_from(CONTROLS))
    )
    return {
        "proposal": {"market": market, "sector": draw(st.sampled_from(["Technology", "Energy"]))},
        "levels": {"ema20": entry, "swing_low": stop, "resistance": target},
        "portfolio": {
            "holdings": tuple(holding(i, sec, str(v)) for i, sec, v, _ in positions),
            "settled_cash_eur": draw(mostly(dec("2000", "6000"), dec("0", "6000"))),
            "pnl_today_eur": draw(mostly(dec("-29", "50"), dec("-200", "50"))),
            "pnl_week_eur": draw(mostly(dec("-59", "100"), dec("-400", "100"))),
            "drawdown_eur": draw(mostly(dec("0", "149"), dec("0", "1000"))),
            "orders_today": draw(mostly(st.integers(0, 5), st.integers(0, 8))),
        },
        "market": {
            "mid": mid,
            "atr": atr,
            "avg_daily_value": draw(mostly(dec("20000000", "1e9", 0), dec("0", "1e9", 0))),
            "sessions_to_earnings": draw(mostly(st.none() | st.integers(4, 30), st.integers(0, 3))),
            "in_universe": draw(mostly(st.just(True), st.booleans())),
            "eur_rate": D("1.10") if market == "US" else D(1),
            "correlations": {i: rho for i, _, _, rho in positions if rho is not None},
        },
        "controls": {
            "trading": draw(mostly(st.just("active"), st.sampled_from(["paused", "halted"]))),
            "app_mode": mode,
            "gateway_mode": gateway,
            "live_confirmed": confirmed,
        },
        "now": OPEN + timedelta(minutes=draw(mostly(st.integers(15, 380), st.integers(-30, 420)))),
    }


def test_scenarios_reach_approvals() -> None:
    """Guards the property test below against only ever seeing rejections."""
    assert run(**find(scenarios(), lambda s: run(**s).approved)).approved


@settings(max_examples=300)
@given(scenarios())
def test_no_approval_breaks_a_limit(s: dict[str, Any]) -> None:
    d = run(**s)
    assert d.approved == all(c.outcome == "pass" for c in d.checks)
    if not d.approved:
        assert (d.quantity, d.entry, d.value, d.fees) == (0, None, 0, 0)
        assert d.failures
        return

    p = PROPOSAL.model_copy(update=s["proposal"])
    m = MARKET.model_copy(update=s["market"])
    pf = PORTFOLIO.model_copy(update=s["portfolio"])
    c = PAPER.model_copy(update=s["controls"])
    cfg = SLEEVES[p.market]
    budget, rate = cfg.capital.agent_budget_eur, m.eur_rate
    e, st_, t = s["levels"]["ema20"], s["levels"]["swing_low"], s["levels"]["resistance"]
    q = d.quantity
    assert (d.entry, d.stop, d.target) == (e, st_, t)

    # 1 global
    assert c.trading == "active"
    if c.app_mode == "live":
        assert c.gateway_mode == "live"
        assert c.live_confirmed
    else:
        assert c.gateway_mode != "live"
    assert OPEN + timedelta(minutes=15) <= s["now"] <= CLOSE - timedelta(minutes=10)
    # 2 instrument
    assert p.market in (cfg.markets.live if c.app_mode == "live" else cfg.markets.paper)
    assert m.mid >= 5
    assert m.avg_daily_value >= 20_000_000
    assert m.sessions_to_earnings is None or m.sessions_to_earnings > 3
    # 3 levels
    assert st_ < e < t
    assert (t - e) / (e - st_) >= 2
    assert 1 <= (e - st_) / m.atr <= 4
    assert e <= m.mid * D("1.01")
    assert m.mid > st_
    # 4 sizing
    assert q > 0
    assert q * (e - st_) <= D("0.015") * budget * rate
    assert q * e <= D("0.30") * budget * rate
    assert q * e <= pf.settled_cash_eur * rate - D("0.10") * budget * rate
    assert q * e >= 200 * rate
    assert (d.risk, d.value) == (q * (e - st_), q * e)
    # 5 fees
    assert d.fees == round_trip_fees(FEES, p.market, q, e, t) <= D("0.10") * d.risk
    # 6 portfolio
    held = pf.holdings
    assert len(held) < 4
    new_eur = q * e / rate
    assert (
        new_eur + sum((h.value_eur for h in held if h.sector == p.sector), D(0))
        <= D("0.60") * budget
    )
    corr = m.correlations
    assert (
        new_eur + sum((h.value_eur for h in held if corr.get(h.instrument_id, 1.0) > 0.7), D(0))
        <= D("0.60") * budget
    )
    # 7 loss limits
    assert pf.pnl_today_eur > D("-0.03") * budget
    assert pf.pnl_week_eur > D("-0.06") * budget
    assert pf.drawdown_eur < D("0.15") * budget
    assert d.trip is None
    # 8 rate limits
    assert pf.orders_today < 6
    assert all(h.instrument_id != p.instrument_id for h in held)
