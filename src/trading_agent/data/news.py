"""Finnhub company news (free tier: North American companies only)."""

from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

import httpx
from pydantic import SecretStr

from trading_agent.domain.market import Instrument
from trading_agent.domain.news import NewsItem

FINNHUB_URL = "https://finnhub.io/api/v1/company-news"
TIMEOUT_S = 20.0
MAX_ITEMS = 20  # newest first; a busy day has more than a triage needs


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
