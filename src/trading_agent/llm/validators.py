"""Deterministic checks after every LLM call (IMPLEMENTATION.md 7.3)."""

import re
from collections.abc import Iterable, Mapping

from trading_agent.domain.analysis import ValidationIssue

_NUMBER = re.compile(r"(?<![A-Za-z_\d.])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
# Small counts and standard indicator periods ("RSI 14", "200-day") aren't claims about the data.
_ALWAYS_OK = {float(n) for n in (*range(11), 12, 14, 20, 26, 50, 52, 100, 200)}


def _error(code: str, message: str) -> ValidationIssue:
    return ValidationIssue(code=code, message=message, severity="error")


def numbers_in(text: str) -> list[float]:
    return [float(m.replace(",", "")) for m in _NUMBER.findall(text)]


def check_long_plan(
    entry: float,
    stop: float,
    target: float,
    atr: float | None,
    *,
    min_rr: float,
    stop_atr: tuple[float, float],
) -> list[ValidationIssue]:
    if not stop < entry < target:
        return [
            _error(
                "level_order",
                f"a long needs stop < entry < target; got stop {stop}, entry {entry}, "
                f"target {target}",
            )
        ]
    issues: list[ValidationIssue] = []
    rr = (target - entry) / (entry - stop)
    if rr < min_rr * 0.99:  # menu levels are rounded to cents
        issues.append(_error("risk_reward", f"reward:risk is {rr:.2f}, the minimum is {min_rr:g}"))
    if atr is not None and atr > 0:
        lo, hi = stop_atr
        distance = (entry - stop) / atr
        if not lo <= distance <= hi:
            issues.append(
                _error(
                    "stop_distance",
                    f"the stop is {distance:.2f} x ATR14 below the entry; allowed is "
                    f"{lo:g} to {hi:g} x ATR14",
                )
            )
    return issues


def check_data_gaps(unknown: Iterable[str], data_gaps: Iterable[str]) -> list[ValidationIssue]:
    missing = sorted(unknown)
    if missing and not any(g.strip() for g in data_gaps):
        return [
            _error(
                "missing_data_gaps",
                f"the input marks these as unknown, so data_gaps must not be empty: "
                f"{', '.join(missing)}",
            )
        ]
    return []


def check_grounded(
    texts: Mapping[str, str], source: str, tolerance: float = 0.005
) -> list[ValidationIssue]:
    """Every number in the free text must appear in the input (±0.5 %), else it may be invented."""
    known = [abs(v) for v in numbers_in(source)]
    issues: list[ValidationIssue] = []
    for field, text in texts.items():
        stray: list[str] = []
        for value in numbers_in(_ISO_DATE.sub(" ", text)):
            v = abs(value)
            if v in _ALWAYS_OK or any(abs(v - k) <= tolerance * max(k, 1e-9) for k in known):
                continue
            stray.append(f"{value:g}")
        if stray:
            issues.append(
                ValidationIssue(
                    code="ungrounded_number",
                    message=f"{field}: numbers not found in the input: {', '.join(stray)}",
                    severity="warning",
                )
            )
    return issues
