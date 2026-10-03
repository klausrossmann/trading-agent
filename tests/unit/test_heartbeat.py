import httpx
import pytest
from pydantic import SecretStr

from trading_agent.notify.heartbeat import ping

URL = SecretStr("https://hc.example/ping/secret-uuid")


def client_returning(status: int, calls: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_pings_url() -> None:
    calls: list[httpx.Request] = []
    async with client_returning(200, calls) as client:
        assert await ping(client, URL) is True
    assert [str(r.url) for r in calls] == [URL.get_secret_value()]


@pytest.mark.parametrize("url", [None, SecretStr("")])
async def test_skips_without_url(url: SecretStr | None) -> None:
    calls: list[httpx.Request] = []
    async with client_returning(200, calls) as client:
        assert await ping(client, url) is False
    assert calls == []


async def test_http_error_does_not_raise() -> None:
    async with client_returning(503, []) as client:
        assert await ping(client, URL) is False


async def test_network_error_does_not_raise_or_log_url(capsys: pytest.CaptureFixture[str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await ping(client, URL) is False
    captured = capsys.readouterr()
    assert "secret-uuid" not in captured.out + captured.err
