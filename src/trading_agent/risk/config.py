"""Typed view of config/risk.yaml. Every key is validated; unknown keys are rejected."""

from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from trading_agent.calc.sizing import SizingLimits
from trading_agent.domain.market import Market


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Capital(_Strict):
    agent_budget_eur: Decimal = Field(gt=0)
    min_cash_reserve_pct: Decimal = Field(ge=0, lt=100)
    # paper only: a separate notional budget per market
    paper_budget_eur: dict[Market, Decimal] = Field(default_factory=dict[Market, Decimal])


class PerTrade(_Strict):
    max_risk_pct: Decimal = Field(gt=0, le=5)
    max_position_pct: Decimal = Field(gt=0, le=100)
    min_position_eur: Decimal = Field(ge=0)
    max_fee_to_risk_pct: Decimal = Field(gt=0)
    require_broker_side_stop: bool
    order_type: Literal["limit_only"]
    max_limit_deviation_pct: Decimal = Field(gt=0)
    min_risk_reward: Decimal = Field(gt=0)
    stop_atr_min: Decimal = Field(gt=0)
    stop_atr_max: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def _stop_atr_range(self) -> "PerTrade":
        if self.stop_atr_min >= self.stop_atr_max:
            raise ValueError("stop_atr_min must be below stop_atr_max")
        return self


class Portfolio(_Strict):
    max_open_positions: int = Field(ge=1)
    max_sector_pct: Decimal = Field(gt=0, le=100)
    max_correlated_cluster_pct: Decimal = Field(gt=0, le=100)


class LossLimits(_Strict):
    daily_loss_pct: Decimal = Field(gt=0)
    weekly_loss_pct: Decimal = Field(gt=0)
    max_drawdown_pct: Decimal = Field(gt=0)


class Markets(_Strict):
    paper: list[Market]
    live: list[Market]


class Instruments(_Strict):
    min_avg_daily_dollar_volume: Decimal = Field(ge=0)
    min_price: Decimal = Field(ge=0)
    blacklist: list[str] = Field(default_factory=list[str])  # Yahoo symbols, never traded


class Execution(_Strict):
    routing: Literal["smart"]
    max_orders_per_day: int = Field(ge=1)
    no_trading_first_minutes: int = Field(ge=0)
    no_trading_last_minutes: int = Field(ge=0)
    no_new_entries_before_earnings_days: int = Field(ge=0)


class RiskConfig(_Strict):
    capital: Capital
    per_trade: PerTrade
    portfolio: Portfolio
    loss_limits: LossLimits
    markets: Markets
    instruments: Instruments
    execution: Execution

    def sizing_limits(self) -> SizingLimits:
        return SizingLimits(
            budget_eur=self.capital.agent_budget_eur,
            max_risk_pct=self.per_trade.max_risk_pct,
            max_position_pct=self.per_trade.max_position_pct,
            min_position_eur=self.per_trade.min_position_eur,
            cash_reserve_pct=self.capital.min_cash_reserve_pct,
        )

    def budget_for(self, market: Market, mode: Literal["paper", "live"]) -> Decimal:
        if mode == "paper":
            return self.capital.paper_budget_eur.get(market, self.capital.agent_budget_eur)
        return self.capital.agent_budget_eur

    def sleeves(
        self, markets: list[Market], mode: Literal["paper", "live"]
    ) -> list[tuple[list[Market], "RiskConfig"]]:
        """Markets grouped by budget; each group is simulated as its own account."""
        groups: dict[Decimal, list[Market]] = {}
        for m in markets:
            groups.setdefault(self.budget_for(m, mode), []).append(m)
        return [
            (
                ms,
                self.model_copy(
                    update={"capital": self.capital.model_copy(update={"agent_budget_eur": b})}
                ),
            )
            for b, ms in groups.items()
        ]


def load_risk_config(config_dir: Path) -> RiskConfig:
    raw = yaml.safe_load((config_dir / "risk.yaml").read_text(encoding="utf-8"))
    return RiskConfig.model_validate(raw)
