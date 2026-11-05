"""News-triggered trades: the thresholds code applies to the News agent's answers."""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

NAME = "news"


class NewsParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    buy_confidence: float = Field(default=0.6, ge=0, le=1)
    exit_confidence: float = Field(default=0.7, ge=0, le=1)
    max_candidates: int = Field(default=3, ge=0)
    max_trades_per_day: int = Field(default=1, ge=0)
    signal_hours: int = Field(default=24, ge=1)
    lookback_days: int = Field(default=2, ge=1)
    max_headlines: int = Field(default=5, ge=1)
    batch: int = Field(default=8, ge=1)


def load_news_params(config_dir: Path) -> NewsParams:
    raw = yaml.safe_load((config_dir / "strategies.yaml").read_text(encoding="utf-8"))
    return NewsParams.model_validate(raw.get("news") or {})
