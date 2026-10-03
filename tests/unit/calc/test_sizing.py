"""Sizing: worked examples plus property tests that no result can break a limit."""

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from trading_agent.calc.sizing import Sizing, SizingLimits, position_size

D = Decimal
LIMITS = SizingLimits(
    budget_eur=D(1000),
    max_risk_pct=D("1.5"),
    max_position_pct=D(30),
    min_position_eur=D(200),
    cash_reserve_pct=D(10),
)


def size(entry: str, stop: str, cash: str = "1000", rate: str = "1") -> Sizing:
    return position_size(
        entry=D(entry), stop=D(stop), settled_cash=D(cash), eur_rate=D(rate), limits=LIMITS
    )


def test_concept_example_eur() -> None:
    # CONCEPT 6.3: EUR 300 position, stop 5 % below -> EUR 15 at risk.
    # risk cap 15 / 2.5 = 6, position cap 300 / 50 = 6, cash cap (1000 - 100) / 50 = 18
    s = size("50", "47.5")
    assert (s.quantity, s.risk, s.value) == (6, D("15.0"), D("300.0"))
    assert s.binding == "risk"


def test_usd_trade_position_cap_binds() -> None:
    # B = 1100 USD; risk cap 16.5 / 2.3 = 7.17; position cap 330 / 52.10 = 6.33
    s = size("52.10", "49.80", cash="1100", rate="1.10")
    assert (s.quantity, s.binding) == (6, "position")
    assert s.value == D("312.60")


def test_cash_cap_binds() -> None:
    # (250 - 100) / 50 = 3 shares = EUR 150 < 200 minimum -> rejected
    s = size("50", "47.5", cash="250")
    assert s.quantity == 0
    assert s.reason is not None
    assert "minimum" in s.reason


def test_rejections() -> None:
    assert size("50", "50").reason == "stop must be below entry for a long trade"
    assert size("50", "0").quantity == 0
    assert size("500", "400").quantity == 0  # risk cap 15 / 100 < 1 share
    assert size("150", "140").quantity == 0  # 1 share = EUR 150 < 200


prices = st.decimals(min_value=D("0.5"), max_value=D(5000), places=2)
fractions = st.decimals(min_value=D("0.001"), max_value=D("0.5"), places=3)


@given(
    entry=prices,
    stop_fraction=fractions,
    cash=st.decimals(min_value=D(0), max_value=D(20000), places=2),
    rate=st.decimals(min_value=D("0.5"), max_value=D(2), places=4),
)
def test_no_sizing_breaks_a_limit(
    entry: Decimal, stop_fraction: Decimal, cash: Decimal, rate: Decimal
) -> None:
    stop = (entry * (1 - stop_fraction)).quantize(D("0.01"))
    s = position_size(entry=entry, stop=stop, settled_cash=cash, eur_rate=rate, limits=LIMITS)
    budget = LIMITS.budget_eur * rate
    risk_cap = LIMITS.max_risk_pct / 100 * budget
    position_cap = LIMITS.max_position_pct / 100 * budget
    spendable = cash - LIMITS.cash_reserve_pct / 100 * budget

    def fits(q: int) -> bool:
        return (
            q * (entry - stop) <= risk_cap and q * entry <= position_cap and q * entry <= spendable
        )

    assert s.quantity >= 0
    if s.quantity == 0:
        assert s.reason is not None
        assert (s.risk, s.value) == (0, 0)
        return
    assert stop < entry
    assert fits(s.quantity)
    assert not fits(s.quantity + 1)  # the largest quantity that fits
    assert s.value == s.quantity * entry >= LIMITS.min_position_eur * rate
    assert s.risk == s.quantity * (entry - stop)


@given(entry=prices, stop_fraction=fractions, cash=st.decimals(D(0), D(20000), places=2))
def test_more_cash_never_means_a_smaller_position(
    entry: Decimal, stop_fraction: Decimal, cash: Decimal
) -> None:
    stop = (entry * (1 - stop_fraction)).quantize(D("0.01"))
    if stop <= 0:
        return
    less = position_size(entry=entry, stop=stop, settled_cash=cash, eur_rate=D(1), limits=LIMITS)
    more = position_size(
        entry=entry, stop=stop, settled_cash=cash + 500, eur_rate=D(1), limits=LIMITS
    )
    assert more.quantity >= less.quantity
