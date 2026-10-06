"""Sizing: worked examples plus property tests that no result can break a limit."""

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from trading_agent.calc.sizing import Sizing, SizingLimits, conviction, position_size
from trading_agent.domain.numbers import QUANTITY_STEP

D = Decimal
LIMITS = SizingLimits(
    budget_eur=D(1000),
    min_risk_pct=D("0.75"),
    max_risk_pct=D("1.5"),
    min_position_pct=D(10),
    max_position_pct=D(25),
    min_confidence=D("0.3"),
    max_confidence=D("0.6"),
    min_position_eur=D(100),
    cash_reserve_pct=D(10),
)


def size(
    entry: str, stop: str, cash: str = "1000", rate: str = "1", confidence: str | None = None
) -> Sizing:
    return position_size(
        entry=D(entry),
        stop=D(stop),
        settled_cash=D(cash),
        eur_rate=D(rate),
        limits=LIMITS,
        confidence=None if confidence is None else D(confidence),
    )


def test_full_size_without_a_confidence_risk_binds() -> None:
    # risk cap 15 / 5 = 3, position cap 250 / 50 = 5, cash cap (1000 - 100) / 50 = 18
    s = size("50", "45")
    assert (s.quantity, s.risk, s.value, s.binding) == (3, D(15), D(150), "risk")
    assert s.conviction == 1


def test_usd_trade_position_cap_binds_with_a_fraction() -> None:
    # B = 1100 USD; risk cap 16.5 / 2.3 = 7.17; position cap 275 / 52.10 = 5.2783
    s = size("52.10", "49.80", cash="1100", rate="1.10")
    assert (s.quantity, s.binding) == (D("5.2783"), "position")
    assert s.value == D("5.2783") * D("52.10")


def test_one_expensive_share_is_split() -> None:
    # risk cap 15 / 25 = 0.6, position cap 250 / 500 = 0.5
    s = size("500", "475")
    assert (s.quantity, s.value, s.binding) == (D("0.5"), D(250), "position")


def test_size_grows_with_confidence() -> None:
    # stop 5 % below: min conviction risk 7.5 / 2.5 = 3, position 100 / 50 = 2
    low = size("50", "47.5", confidence="0.3")
    assert (low.quantity, low.conviction) == (2, 0)
    # half way: risk 11.25 / 2.5 = 4.5, position 175 / 50 = 3.5
    mid = size("50", "47.5", confidence="0.45")
    assert (mid.quantity, mid.conviction) == (D("3.5"), D("0.5"))
    # full: position 250 / 50 = 5
    high = size("50", "47.5", confidence="0.6")
    assert (high.quantity, high.conviction) == (5, 1)


def test_confidence_outside_the_range_is_clamped() -> None:
    assert (
        size("50", "47.5", confidence="0.05").quantity
        == size("50", "47.5", confidence="0.3").quantity
    )
    assert (
        size("50", "47.5", confidence="0.95").quantity
        == size("50", "47.5", confidence="0.6").quantity
    )


def test_conviction_curve() -> None:
    assert [conviction(D(c), LIMITS) for c in ("0", "0.3", "0.45", "0.6", "1")] == [
        0,
        0,
        D("0.5"),
        1,
        1,
    ]
    assert conviction(None, LIMITS) == 1


def test_cash_cap_binds() -> None:
    # (250 - 100) / 50 = 3 shares = EUR 150, below the risk cap 6 and the position cap 5
    s = size("50", "47.5", cash="250")
    assert (s.quantity, s.binding) == (3, "cash")


def test_rejections() -> None:
    assert size("50", "50").reason == "stop must be below entry for a long trade"
    assert size("50", "0").quantity == 0
    assert "minimum" in str(size("500", "400").reason)  # risk cap 0.15 shares = EUR 75 < 100
    assert "minimum" in str(size("50", "47.5", cash="140").reason)  # 0.8 shares = EUR 40
    assert "no capacity" in str(size("50", "47.5", cash="100").reason)  # nothing above the reserve


prices = st.decimals(min_value=D("0.5"), max_value=D(5000), places=2)
fractions = st.decimals(min_value=D("0.001"), max_value=D("0.5"), places=3)
confidences = st.one_of(st.none(), st.decimals(min_value=D(0), max_value=D(1), places=2))


@given(
    entry=prices,
    stop_fraction=fractions,
    cash=st.decimals(min_value=D(0), max_value=D(20000), places=2),
    rate=st.decimals(min_value=D("0.5"), max_value=D(2), places=4),
    confidence=confidences,
)
def test_no_sizing_breaks_a_limit(
    entry: Decimal, stop_fraction: Decimal, cash: Decimal, rate: Decimal, confidence: Decimal | None
) -> None:
    stop = (entry * (1 - stop_fraction)).quantize(D("0.01"))
    s = position_size(
        entry=entry,
        stop=stop,
        settled_cash=cash,
        eur_rate=rate,
        limits=LIMITS,
        confidence=confidence,
    )
    c = conviction(confidence, LIMITS)
    budget = LIMITS.budget_eur * rate
    risk_cap = (
        (LIMITS.min_risk_pct + c * (LIMITS.max_risk_pct - LIMITS.min_risk_pct)) / 100 * budget
    )
    position_cap = (
        (LIMITS.min_position_pct + c * (LIMITS.max_position_pct - LIMITS.min_position_pct))
        / 100
        * budget
    )
    spendable = cash - LIMITS.cash_reserve_pct / 100 * budget

    def fits(q: Decimal) -> bool:
        return (
            q * (entry - stop) <= risk_cap and q * entry <= position_cap and q * entry <= spendable
        )

    assert s.quantity >= 0
    if s.quantity == 0:
        assert s.reason is not None
        assert (s.risk, s.value) == (0, 0)
        return
    assert stop < entry
    assert s.quantity % QUANTITY_STEP == 0
    assert fits(s.quantity)
    assert not fits(s.quantity + QUANTITY_STEP)  # the largest quantity that fits
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


@given(
    entry=prices,
    stop_fraction=fractions,
    low=st.decimals(D(0), D(1), places=2),
    high=st.decimals(D(0), D(1), places=2),
)
def test_more_confidence_never_means_a_smaller_position(
    entry: Decimal, stop_fraction: Decimal, low: Decimal, high: Decimal
) -> None:
    stop = (entry * (1 - stop_fraction)).quantize(D("0.01"))
    if stop <= 0:
        return
    low, high = min(low, high), max(low, high)
    kwargs = {"entry": entry, "stop": stop, "settled_cash": D(20000), "eur_rate": D(1)}
    a = position_size(**kwargs, limits=LIMITS, confidence=low)
    b = position_size(**kwargs, limits=LIMITS, confidence=high)
    assert b.quantity >= a.quantity
