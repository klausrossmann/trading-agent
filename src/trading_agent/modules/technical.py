"""Technical module: indicators, trend and level menu in; a rating and a level-based plan out."""

import math
from collections.abc import Sequence
from datetime import date
from functools import cache
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, create_model

from trading_agent.calc.indicators import atr, bollinger, macd, rsi, sma
from trading_agent.calc.levels import level_menu
from trading_agent.calc.trend import relative_strength, trend_states
from trading_agent.domain.analysis import ValidationIssue
from trading_agent.domain.levels import LevelKind
from trading_agent.domain.market import Currency, Instrument, Market
from trading_agent.llm.prompts import Prompt
from trading_agent.llm.validators import check_data_gaps, check_grounded, check_long_plan

NAME = "technical"
PROMPT_VERSION = 1
FABRICATION_CAP = 0.5  # setup_quality cap when the text quotes numbers not in the input
TEXT_FIELDS = ("trend_summary", "momentum_summary", "volume_summary", "reasoning", "invalidation")


def _num(value: float, places: int = 2) -> float | None:
    return round(float(value), places) if math.isfinite(value) else None


class PlanRules(BaseModel):
    model_config = ConfigDict(frozen=True)

    min_risk_reward: float
    stop_atr_min: float
    stop_atr_max: float


class Level(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    price: float
    kind: LevelKind
    touches: int | None = None


class TechnicalInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    name: str
    market: Market
    currency: Currency
    sector: str | None
    as_of: date
    close: float
    change_pct: dict[str, float | None]
    trend: dict[str, str]
    indicators: dict[str, float | None]
    volume: dict[str, float | None]
    levels: list[Level]
    rules: PlanRules
    unknown: list[str]


def compute(
    instrument: Instrument,
    frame: pd.DataFrame,
    benchmark_close: pd.Series | None,
    rules: PlanRules,
) -> TechnicalInput:
    close = frame["close"]
    last = float(close.iloc[-1])

    def change(sessions: int) -> float | None:
        if len(close) <= sessions:
            return None
        return _num((last / float(close.iloc[-1 - sessions]) - 1) * 100)

    def pct_vs(series: pd.Series) -> float | None:
        ref = float(series.iloc[-1])
        return _num((last / ref - 1) * 100) if math.isfinite(ref) and ref > 0 else None

    atr_now = float(atr(frame).iloc[-1])
    bands = bollinger(close).iloc[-1]
    width = float(bands["upper"] - bands["lower"])
    m = macd(close).iloc[-1]
    rs = (
        float(relative_strength(close, benchmark_close).iloc[-1])
        if benchmark_close is not None
        else math.nan
    )
    indicators: dict[str, float | None] = {
        "rsi14": _num(float(rsi(close).iloc[-1]), 1),
        "macd": _num(float(m["macd"]), 3),
        "macd_signal": _num(float(m["signal"]), 3),
        "macd_hist": _num(float(m["hist"]), 3),
        "atr14": _num(atr_now),
        "atr_pct": _num(atr_now / last * 100),
        "bb_pct_b": _num((last - float(bands["lower"])) / width, 2) if width > 0 else None,
        "sma100": _num(float(sma(close, 100).iloc[-1])),
        "close_vs_ema20_pct": None,
        "close_vs_sma50_pct": pct_vs(sma(close, 50)),
        "close_vs_sma200_pct": pct_vs(sma(close, 200)),
        "rs_vs_benchmark_63d_pct": _num(rs * 100),
    }
    levels = level_menu(frame)
    ema20 = next((lv for lv in levels if lv.name == "ema20"), None)
    if ema20 is not None:
        indicators["close_vs_ema20_pct"] = _num((last / float(ema20.price) - 1) * 100)

    vol = frame["volume"]
    recent = frame.iloc[-20:]
    up = recent["close"] > recent["close"].shift(1).fillna(recent["open"])
    up_vol, down_vol = float(recent["volume"][up].sum()), float(recent["volume"][~up].sum())
    avg20, avg50 = float(vol.iloc[-20:].mean()), float(vol.iloc[-50:].mean())
    volume: dict[str, float | None] = {
        "avg20": _num(avg20, 0) if len(vol) >= 20 else None,
        "avg50": _num(avg50, 0) if len(vol) >= 50 else None,
        "avg20_vs_avg50": _num(avg20 / avg50) if len(vol) >= 50 and avg50 > 0 else None,
        "last_vs_avg20": _num(float(vol.iloc[-1]) / avg20)
        if len(vol) >= 20 and avg20 > 0
        else None,
        "up_vs_down_volume_20d": _num(up_vol / down_vol) if down_vol > 0 else None,
    }
    trend = trend_states(close)
    change_pct = {f"{n}d": change(n) for n in (1, 5, 20, 63)}
    unknown = sorted(
        [f"change_pct.{k}" for k, v in change_pct.items() if v is None]
        + [f"trend.{k}" for k, v in trend.items() if v == "unknown"]
        + [f"indicators.{k}" for k, v in indicators.items() if v is None]
        + [f"volume.{k}" for k, v in volume.items() if v is None]
    )
    return TechnicalInput(
        symbol=instrument.yahoo_symbol,
        name=instrument.name,
        market=instrument.market,
        currency=instrument.currency,
        sector=instrument.sector,
        as_of=pd.Timestamp(frame.index[-1]).date(),
        close=round(last, 2),
        change_pct=change_pct,
        trend=dict(trend),
        indicators=indicators,
        volume=volume,
        levels=[
            Level(name=lv.name, price=float(lv.price), kind=lv.kind, touches=lv.touches)
            for lv in levels
        ],
        rules=rules,
        unknown=unknown,
    )


Rating = Literal["strong_buy", "buy", "neutral", "avoid"]


class TechnicalAssessment(BaseModel):
    trend_summary: str = Field(description="Daily and weekly trend, 1-3 sentences")
    momentum_summary: str = Field(description="RSI, MACD and Bollinger position in plain English")
    volume_summary: str = Field(description="Buying vs selling pressure from the volume facts")
    rating: Rating
    setup_quality: float = Field(ge=0, le=1, description="0 = no setup, 1 = textbook setup")
    entry_ref: str | None = Field(description="Level name for the buy limit; null unless buy")
    stop_ref: str | None = Field(description="Level name for the stop; null unless buy")
    target_ref: str | None = Field(description="Level name for the target; null unless buy")
    reasoning: str = Field(description="Why this rating and plan, 2-4 sentences")
    invalidation: str = Field(description="Price action that would prove the thesis wrong")
    data_gaps: list[str] = Field(description="Inputs listed in `unknown`; else empty")


@cache
def technical_output_type(names: tuple[str, ...]) -> type[TechnicalAssessment]:
    """The assessment with level refs restricted to this menu, so the model can't invent prices."""
    names_type = Literal.__getitem__(names)  # pyright: ignore[reportAttributeAccessIssue]
    ref = (names_type | None, Field(description="A level name from `levels`, or null"))
    return create_model(
        "TechnicalAssessment",
        __base__=TechnicalAssessment,
        entry_ref=ref,
        stop_ref=ref,
        target_ref=ref,
    )


class TechnicalModule:
    name = NAME

    def __init__(self, prompt: Prompt) -> None:
        self.prompt = prompt

    def output_type(self, inp: TechnicalInput) -> type[TechnicalAssessment]:
        return technical_output_type(tuple(lv.name for lv in inp.levels))

    def validate(self, inp: TechnicalInput, out: TechnicalAssessment) -> list[ValidationIssue]:
        issues = check_data_gaps(inp.unknown, out.data_gaps)
        if out.rating in ("strong_buy", "buy"):
            refs = (out.entry_ref, out.stop_ref, out.target_ref)
            if any(r is None for r in refs):
                issues.append(
                    ValidationIssue(
                        code="missing_plan",
                        message="a buy rating needs entry_ref, stop_ref and target_ref",
                        severity="error",
                    )
                )
            else:
                price = {lv.name: lv.price for lv in inp.levels}
                issues += check_long_plan(
                    price[str(out.entry_ref)],
                    price[str(out.stop_ref)],
                    price[str(out.target_ref)],
                    inp.indicators.get("atr14"),
                    min_rr=inp.rules.min_risk_reward,
                    stop_atr=(inp.rules.stop_atr_min, inp.rules.stop_atr_max),
                )
        texts = {f: str(getattr(out, f)) for f in TEXT_FIELDS}
        issues += check_grounded(texts, inp.model_dump_json())
        return issues

    def finalize(
        self, inp: TechnicalInput, out: TechnicalAssessment, issues: Sequence[ValidationIssue]
    ) -> TechnicalAssessment:
        if any(i.code == "ungrounded_number" for i in issues):
            return out.model_copy(update={"setup_quality": min(out.setup_quality, FABRICATION_CAP)})
        return out
