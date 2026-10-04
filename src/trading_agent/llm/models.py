"""Model registry (`config/models.yaml`): role -> model, prices, budget, pacing."""

from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_ai.models import Model

from trading_agent.domain.analysis import Role


class Price(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    since: date
    input: float = Field(ge=0)  # USD per 1M tokens
    output: float = Field(ge=0)


class BudgetConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    monthly_usd: float = Field(gt=0)
    lean_mode_at: float = Field(gt=0, le=1)
    hard_stop_at: float = Field(gt=0)


class ScanConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidates: int = Field(ge=1)
    candidates_lean: int = Field(ge=1)


class ModelsConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    roles: dict[Role, str]
    dev_overrides: dict[Role, str] = Field(default_factory=dict[Role, str])
    prices: dict[str, list[Price]] = Field(default_factory=dict[str, list[Price]])
    budget: BudgetConfig
    rate_limits_rpm: dict[str, int] = Field(default_factory=dict[str, int])
    scan: ScanConfig

    @model_validator(mode="after")
    def _sorted_prices(self) -> "ModelsConfig":
        for name, prices in self.prices.items():
            if [p.since for p in prices] != sorted(p.since for p in prices):
                raise ValueError(f"prices for {name} must be in date order")
        return self

    def model_for(self, role: Role, *, dev: bool = False) -> str:
        if dev and role in self.dev_overrides:
            return self.dev_overrides[role]
        return self.roles[role]

    def price(self, model: str, day: date) -> Price | None:
        valid = [p for p in self.prices.get(model, []) if p.since <= day]
        return valid[-1] if valid else None

    def cost_usd(self, model: str, day: date, tokens_in: int, tokens_out: int) -> float:
        price = self.price(model, day)
        if price is None:
            raise ValueError(f"no price for {model} on {day}; add it to config/models.yaml")
        return (tokens_in * price.input + tokens_out * price.output) / 1_000_000

    def min_interval_s(self, model: str) -> float:
        rpm = self.rate_limits_rpm.get(model)
        return 60 / rpm if rpm else 0.0


def load_models_config(config_dir: Path) -> ModelsConfig:
    raw = yaml.safe_load((config_dir / "models.yaml").read_text(encoding="utf-8"))
    return ModelsConfig.model_validate(raw)


class ModelUnavailableError(RuntimeError):
    """The provider's API key is missing or the provider isn't supported yet."""


def build_model(name: str, *, gemini_api_key: SecretStr | None) -> Model:
    """A PydanticAI model with its key passed explicitly (keys never go into os.environ)."""
    provider, _, model_name = name.partition(":")
    if provider == "google":
        key = gemini_api_key.get_secret_value() if gemini_api_key else ""
        if not key:
            raise ModelUnavailableError(f"{name} needs GEMINI_API_KEY")
        from pydantic_ai.models.google import GoogleModel
        from pydantic_ai.providers.google import GoogleProvider

        return GoogleModel(model_name, provider=GoogleProvider(api_key=key))
    raise ModelUnavailableError(f"provider {provider!r} is not set up yet ({name})")
