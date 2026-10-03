from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from trading_agent.data.ingest import describe_error
from trading_agent.data.series import EcbProvider, FredProvider, parse_ecb_csv, parse_fred_json

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_parse_recorded_ecb_fx() -> None:
    obs = parse_ecb_csv((FIXTURES / "ecb_exr_usd.csv").read_text())
    assert len(obs) == 7
    assert obs[0].date == date(2026, 9, 24)
    assert all(Decimal("0.5") < o.value < Decimal("2") for o in obs)


def test_parse_ecb_monthly_and_missing_values() -> None:
    text = "TIME_PERIOD,OBS_VALUE\n2026-08,2.0\n2026-09,\n2026-10,NaN\n"
    assert [(o.date, o.value) for o in parse_ecb_csv(text)] == [(date(2026, 8, 1), Decimal("2.0"))]


def test_parse_fred_skips_missing() -> None:
    payload = {
        "observations": [
            {"date": "2026-09-30", "value": "4.12"},
            {"date": "2026-10-01", "value": "."},
        ]
    }
    assert [(o.date, o.value) for o in parse_fred_json(payload)] == [
        (date(2026, 9, 30), Decimal("4.12"))
    ]


async def test_ecb_404_means_no_data() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await EcbProvider(client).series("EXR/D.USD.EUR.SP00.A", date(2026, 10, 1)) == []
    assert seen[0].url.path.endswith("/EXR/D.USD.EUR.SP00.A")
    assert seen[0].url.params["startPeriod"] == "2026-10-01"


async def test_fred_errors_never_expose_the_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["api_key"] == "k3y-secret"
        return httpx.Response(400, json={"error_message": "bad"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fred = FredProvider(client, SecretStr("k3y-secret"))
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await fred.series("DGS10", date(2026, 1, 1))
    assert "k3y-secret" in str(exc_info.value)  # httpx includes the URL ...
    assert describe_error(exc_info.value) == "HTTP 400"  # ... so we never log str(exc)
