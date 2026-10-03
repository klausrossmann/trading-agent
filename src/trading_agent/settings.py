"""Runtime settings from environment / .env / Docker secrets, plus typed config/*.yaml files."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

_SECRETS_DIR = Path("/run/secrets")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        # Docker secrets are files named like the field, e.g. /run/secrets/db_password.
        secrets_dir=_SECRETS_DIR if _SECRETS_DIR.is_dir() else None,
    )

    app_mode: Literal["paper", "live"] = "paper"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    config_dir: Path = Path("config")
    heartbeat_url: SecretStr | None = None
    fred_api_key: SecretStr | None = None

    db_host: str = "db"
    db_port: int = 5432
    db_name: str = "trading"
    db_user: str = "agent"
    db_password: SecretStr

    @property
    def database_url(self) -> URL:
        return URL.create(
            "postgresql+asyncpg",
            username=self.db_user,
            password=self.db_password.get_secret_value(),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )


class HeartbeatJob(BaseModel):
    interval_minutes: int = Field(ge=1, le=60)


class CronJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cron: dict[
        str, str | int
    ]  # APScheduler CronTrigger fields, e.g. {hour: 7, day_of_week: mon-fri}
    misfire_grace_minutes: int = Field(default=30, ge=0)


class SessionJob(BaseModel):
    """Runs once per exchange session, relative to its open or close (holidays, half-days, DST)."""

    model_config = ConfigDict(extra="forbid")

    calendar: Literal["XNYS", "XETR"]
    anchor: Literal["open", "close"]
    offset_minutes: int
    misfire_grace_minutes: int = Field(default=30, ge=0)


class ScheduleConfig(BaseModel):
    timezone: str = "Europe/Berlin"
    heartbeat: HeartbeatJob
    jobs: dict[str, CronJob | SessionJob] = Field(default_factory=dict)


def load_schedule(config_dir: Path) -> ScheduleConfig:
    raw = yaml.safe_load((config_dir / "schedule.yaml").read_text(encoding="utf-8"))
    return ScheduleConfig.model_validate(raw)


class PricesConfig(BaseModel):
    backfill_years: int = Field(ge=1, le=20)
    overlap_sessions: int = Field(ge=1, le=30)
    restatement_tolerance_pct: float = Field(gt=0)


class MacroConfig(BaseModel):
    fred: list[str] = Field(default_factory=list)
    ecb: dict[str, str] = Field(default_factory=dict)


class EarningsConfig(BaseModel):
    history_limit: int = Field(ge=1)
    daily_limit: int = Field(ge=1)


class QualityConfig(BaseModel):
    max_jump_pct: float = Field(gt=0)
    stale_sessions: int = Field(ge=0)
    block_window_sessions: int = Field(ge=1)


class DataConfig(BaseModel):
    prices: PricesConfig
    fx: dict[str, str]
    macro: MacroConfig
    earnings: EarningsConfig
    quality: QualityConfig


def load_data_config(config_dir: Path) -> DataConfig:
    raw = yaml.safe_load((config_dir / "data.yaml").read_text(encoding="utf-8"))
    return DataConfig.model_validate(raw)
