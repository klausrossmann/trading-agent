"""The News agent: sorts new headlines by importance and decides, per stock, whether they call
for an action. For a stock the agent holds: `exit` (thesis broken) or `none`. For any other
stock: `buy` (a catalyst worth a look by the scan) or `none`. Code acts on the answer: an exit
above the configured confidence sells at the market, a buy signal joins the next scan."""

from collections.abc import Sequence
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from trading_agent.llm.prompts import Prompt
from trading_agent.llm.sanitize import untrusted
from trading_agent.llm.types import ValidationIssue
from trading_agent.llm.validators import check_grounded
from trading_agent.marketdata.types import Market
from trading_agent.news.types import NewsItem, Relevance

NAME = "news"
PROMPT_VERSION = 1
TEXT_CHARS = 400

Action = Literal["none", "buy", "exit"]


class PositionFacts(BaseModel):
    """Computed by code from the bracket and daily bars up to `as_of`."""

    model_config = ConfigDict(frozen=True)

    as_of: date
    entry_date: date
    sessions_held: int
    entry_price: float
    initial_stop: float
    stop: float
    target: float
    last_close: float
    r_now: float  # (last close - entry) / (entry - initial stop)
    closes_last_10: list[float]
    sma20: float | None
    sma50: float | None
    atr14: float | None
    next_report: date | None
    sessions_to_report: int | None
    thesis: str
    invalidation: str


class Headline(BaseModel):
    model_config = ConfigDict(frozen=True)

    ref: str
    published: datetime
    text: str  # headline and summary inside an <untrusted> block


class EarlierNews(BaseModel):
    model_config = ConfigDict(frozen=True)

    published: datetime
    relevance: Relevance | None
    note: str | None
    text: str


class StockNews(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    name: str
    market: Market
    sector: str | None
    position: PositionFacts | None  # None: not held
    headlines: list[Headline]  # new, to be sorted
    earlier: list[EarlierNews] = []  # sorted before, for context (held stocks)


class NewsInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    as_of: date
    stocks: list[StockNews]


class HeadlineVerdict(BaseModel):
    ref: str = Field(description="The headline's ref from the input")
    relevance: Relevance
    note: str = Field(description="Why, in one sentence")


class StockVerdict(BaseModel):
    symbol: str
    items: list[HeadlineVerdict] = Field(description="Every headline of the stock exactly once")
    action: Action
    confidence: float = Field(ge=0, le=1, description="How sure you are that the action is right")
    reason: str = Field(description="1-3 sentences citing the news and facts")


class NewsOutput(BaseModel):
    stocks: list[StockVerdict] = Field(description="Every input stock exactly once")


def _text(n: NewsItem) -> str:
    return untrusted(n.source or "news", f"{n.headline}. {n.summary}", TEXT_CHARS)


def stock_news(
    symbol: str,
    name: str,
    market: Market,
    sector: str | None,
    news: Sequence[NewsItem],
    position: PositionFacts | None = None,
    earlier: Sequence[NewsItem] = (),
) -> StockNews:
    """Headline refs are `<symbol>:<n>`, unique across a batch."""
    return StockNews(
        symbol=symbol,
        name=name,
        market=market,
        sector=sector,
        position=position,
        headlines=[
            Headline(ref=f"{symbol}:{i}", published=n.ts, text=_text(n))
            for i, n in enumerate(news, start=1)
        ],
        earlier=[
            EarlierNews(published=n.ts, relevance=n.relevance, note=n.note, text=_text(n))
            for n in earlier
        ],
    )


def _issue(code: str, message: str) -> ValidationIssue:
    return ValidationIssue(code=code, message=message, severity="error")


class NewsAgent:
    name = NAME

    def __init__(self, prompt: Prompt) -> None:
        self.prompt = prompt

    def output_type(self, inp: NewsInput) -> type[NewsOutput]:
        return NewsOutput

    def validate(self, inp: NewsInput, out: NewsOutput) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        stocks = {s.symbol: s for s in inp.stocks}
        answered = [v.symbol for v in out.stocks]
        if sorted(answered) != sorted(stocks):
            issues.append(_issue("stocks", f"answer every stock exactly once: {', '.join(stocks)}"))
        texts: dict[str, str] = {}
        for i, v in enumerate(out.stocks):
            s = stocks.get(v.symbol)
            if s is None:
                continue
            refs = [h.ref for h in s.headlines]
            if sorted(t.ref for t in v.items) != sorted(refs):
                issues.append(
                    _issue("refs", f"{v.symbol}: answer every headline exactly once: {refs}")
                )
            if v.action == "exit" and s.position is None:
                issues.append(_issue("action", f"{v.symbol}: exit only for a held stock"))
            if v.action == "buy" and s.position is not None:
                issues.append(_issue("action", f"{v.symbol}: buy only for a stock not held"))
            if v.action == "buy" and not any(t.relevance == "high" for t in v.items):
                issues.append(_issue("action", f"{v.symbol}: buy needs a high-relevance headline"))
            texts[f"stocks[{i}].reason"] = v.reason
            texts |= {f"stocks[{i}].items[{k}].note": t.note for k, t in enumerate(v.items)}
        issues += check_grounded(texts, inp.model_dump_json())
        return issues

    def finalize(
        self, inp: NewsInput, out: NewsOutput, issues: Sequence[ValidationIssue]
    ) -> NewsOutput:
        return out
