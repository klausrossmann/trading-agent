"""Monthly LLM budget guard (IMPLEMENTATION.md 7.5)."""

from datetime import UTC, datetime
from typing import Literal

from trading_agent.llm.models import BudgetConfig

BudgetMode = Literal["normal", "lean", "stopped"]


def month_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def budget_mode(spent_usd: float, cfg: BudgetConfig) -> BudgetMode:
    used = spent_usd / cfg.monthly_usd
    if used >= cfg.hard_stop_at:
        return "stopped"
    if used >= cfg.lean_mode_at:
        return "lean"
    return "normal"
