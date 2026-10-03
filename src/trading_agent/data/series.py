"""ECB Data Portal (FX reference rates, policy rates) and FRED (US macro) clients."""

import csv
import io
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from pydantic import SecretStr

from trading_agent.domain.market import Observation

ECB_URL = "https://data-api.ecb.europa.eu/service/data/"
FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
TIMEOUT_S = 30.0


def _period(value: str) -> date:
    return date.fromisoformat(value if len(value) == 10 else f"{value[:7]}-01")  # monthly: YYYY-MM


def _value(raw: str) -> Decimal | None:
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def parse_ecb_csv(text: str) -> list[Observation]:
    observations: list[Observation] = []
    for row in csv.DictReader(io.StringIO(text)):
        value = _value(row.get("OBS_VALUE") or "")
        if value is not None and row.get("TIME_PERIOD"):
            observations.append(Observation(date=_period(row["TIME_PERIOD"]), value=value))
    return observations


def parse_fred_json(payload: dict[str, Any]) -> list[Observation]:
    observations: list[Observation] = []
    for item in payload.get("observations", []):
        value = _value(item.get("value", "."))  # "." marks a missing value
        if value is not None:
            observations.append(Observation(date=date.fromisoformat(item["date"]), value=value))
    return observations


class EcbProvider:
    source = "ecb"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def series(self, key: str, start: date) -> list[Observation]:
        response = await self._client.get(
            ECB_URL + key,
            params={"format": "csvdata", "startPeriod": start.isoformat()},
            timeout=TIMEOUT_S,
        )
        if response.status_code == 404:  # no observations in range
            return []
        response.raise_for_status()
        return parse_ecb_csv(response.text)


class FredProvider:
    source = "fred"

    def __init__(self, client: httpx.AsyncClient, api_key: SecretStr) -> None:
        self._client = client
        self._api_key = api_key

    async def series(self, key: str, start: date) -> list[Observation]:
        response = await self._client.get(
            FRED_URL,
            params={
                "series_id": key,
                "api_key": self._api_key.get_secret_value(),
                "file_type": "json",
                "observation_start": start.isoformat(),
            },
            timeout=TIMEOUT_S,
        )
        response.raise_for_status()
        return parse_fred_json(response.json())
