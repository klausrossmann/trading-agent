"""Company news: Finnhub for US stocks (its free tier covers North America only), Yahoo
Finance for EU stocks."""

import asyncio
from collections.abc import Sequence
from datetime import UTC, date, datetime, time
from typing import Any, Protocol

import httpx
import yfinance as yf
from pydantic import SecretStr

from trading_agent.domain.market import Instrument
from trading_agent.domain.news import NewsItem

FINNHUB_URL = "https://finnhub.io/api/v1/company-news"
TIMEOUT_S = 20.0
MAX_ITEMS = 20  # newest first; a busy day has more than a triage needs


class NewsSource(Protocol):
    source: str
    pause_s: float  # between two requests, to stay under the provider's rate limit

    def covers(self, inst: Instrument) -> bool: ...

    async def company_news(self, inst: Instrument, start: date, end: date) -> list[NewsItem]: ...


def parse_finnhub(payload: Sequence[dict[str, Any]], instrument_id: int) -> list[NewsItem]:
    items: list[NewsItem] = []
    for raw in payload:
        if not raw.get("id") or not raw.get("headline") or not raw.get("datetime"):
            continue
        items.append(
            NewsItem(
                id=f"finnhub:{raw['id']}",
                instrument_id=instrument_id,
                ts=datetime.fromtimestamp(int(raw["datetime"]), UTC),
                headline=str(raw["headline"]),
                summary=str(raw.get("summary") or ""),
                source=str(raw.get("source") or ""),
                url=str(raw.get("url") or ""),
            )
        )
    return sorted(items, key=lambda i: i.ts, reverse=True)[:MAX_ITEMS]


class FinnhubNews:
    source = "finnhub"
    pause_s = 1.1  # free tier: 60 requests a minute

    def __init__(self, client: httpx.AsyncClient, api_key: SecretStr) -> None:
        self._client = client
        self._key = api_key

    def covers(self, inst: Instrument) -> bool:
        return inst.market == "US"

    async def company_news(self, inst: Instrument, start: date, end: date) -> list[NewsItem]:
        if inst.id is None:
            raise ValueError(f"{inst.yahoo_symbol}: no database id")
        response = await self._client.get(
            FINNHUB_URL,
            params={
                "symbol": inst.symbol,
                "from": start.isoformat(),
                "to": end.isoformat(),
                "token": self._key.get_secret_value(),
            },
            timeout=TIMEOUT_S,
        )
        response.raise_for_status()
        payload = response.json()
        return parse_finnhub(payload if isinstance(payload, list) else [], inst.id)


def _yahoo_ts(raw: dict[str, Any]) -> datetime | None:
    if isinstance(raw.get("providerPublishTime"), int):
        return datetime.fromtimestamp(raw["providerPublishTime"], UTC)
    try:
        return datetime.fromisoformat(str(raw["pubDate"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None


def parse_yahoo(payload: Sequence[dict[str, Any]], instrument_id: int) -> list[NewsItem]:
    """yfinance's `get_news()`: items nested under `content` (yfinance >= 0.2.50) or flat."""
    items: list[NewsItem] = []
    for outer in payload:
        raw: dict[str, Any] = outer.get("content") or outer
        news_id = outer.get("id") or raw.get("id") or raw.get("uuid")
        title = raw.get("title")
        ts = _yahoo_ts(raw)
        if not news_id or not title or ts is None:
            continue
        provider = raw.get("provider") or {}
        url = (raw.get("canonicalUrl") or {}).get("url") or raw.get("link") or ""
        items.append(
            NewsItem(
                id=f"yahoo:{news_id}",
                instrument_id=instrument_id,
                ts=ts if ts.tzinfo else ts.replace(tzinfo=UTC),
                headline=str(title),
                summary=str(raw.get("summary") or ""),
                source=str(provider.get("displayName") or raw.get("publisher") or ""),
                url=str(url),
            )
        )
    return sorted(items, key=lambda i: i.ts, reverse=True)[:MAX_ITEMS]


class YahooNews:
    """Unofficial (yfinance): only the latest headlines, no date filter on Yahoo's side."""

    source = "yahoo"
    pause_s = 0.5

    def covers(self, inst: Instrument) -> bool:
        return inst.market == "EU"

    async def company_news(self, inst: Instrument, start: date, end: date) -> list[NewsItem]:
        if inst.id is None:
            raise ValueError(f"{inst.yahoo_symbol}: no database id")
        ticker = yf.Ticker(inst.yahoo_symbol)
        payload = await asyncio.to_thread(ticker.get_news, count=MAX_ITEMS)
        since = datetime.combine(start, time(), UTC)
        until = datetime.combine(end, time.max, UTC)
        return [n for n in parse_yahoo(payload or [], inst.id) if since <= n.ts <= until]
