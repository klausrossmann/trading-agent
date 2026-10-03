import json
import logging
from collections.abc import Iterator

import pytest
import structlog

from trading_agent.log import configure_logging


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    structlog.reset_defaults()


def test_events_render_as_json_lines(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", json=True)
    structlog.get_logger("t").info("agent.started", mode="paper")
    logging.getLogger("lib").warning("from stdlib")
    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert lines[0] | {"timestamp": None} == {
        "event": "agent.started",
        "mode": "paper",
        "level": "info",
        "logger": "t",
        "timestamp": None,
    }
    assert lines[1]["event"] == "from stdlib"


def test_http_client_urls_are_not_logged(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("DEBUG", json=True)
    logging.getLogger("httpx").info("HTTP Request: GET https://hc.example/ping/secret-uuid")
    logging.getLogger("httpcore.http11").debug("request url=secret-uuid")
    assert "secret-uuid" not in capsys.readouterr().err
