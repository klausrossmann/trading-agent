"""Alerting from jobs and the scheduler, with a fake notifier."""

import asyncio
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from trading_agent import jobs
from trading_agent.data import ingest
from trading_agent.data.quality import QualityIssue
from trading_agent.data.series import EcbProvider
from trading_agent.data.yahoo import YahooProvider
from trading_agent.scheduler import alert_on_job_failures
from trading_agent.settings import load_data_config

ROOT = Path(__file__).resolve().parents[2]


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


def issue(symbol: str, check: Any = "stale", blocking: bool = True) -> QualityIssue:
    return QualityIssue(symbol, check, date(2026, 10, 2), "", blocking)


async def test_quality_alert_only_for_newly_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    rounds = [
        [issue("A.DE"), issue("B.DE", "jump"), issue("C.DE", blocking=False)],
        [issue("A.DE"), issue("B.DE", "jump")],
        [issue("A.DE"), issue("D.DE")],
    ]

    async def fake_quality(*_: Any, **__: Any) -> list[QualityIssue]:
        return rounds.pop(0)

    monkeypatch.setattr(ingest, "run_quality", fake_quality)
    notifier = FakeNotifier()
    state = jobs.RuntimeState(mode="paper", started_at=datetime.now(UTC))
    async with httpx.AsyncClient() as http:
        ctx = jobs.DataContext(
            sessions=None,  # pyright: ignore[reportArgumentType]
            cfg=load_data_config(ROOT / "config"),
            yahoo=YahooProvider(),
            ecb=EcbProvider(http),
            fred=None,
            notifier=notifier,
            state=state,
        )
        for _ in range(3):
            await jobs._check_quality(ctx, "EU", datetime.now(UTC))  # pyright: ignore[reportPrivateUsage]
    assert notifier.sent == [
        "⚠️ Data quality EU: excluded from today's scan: A.DE (stale), B.DE (jump)",
        "⚠️ Data quality EU: excluded from today's scan: D.DE (stale)",
    ]
    assert state.blocked == {"EU": ["A.DE", "D.DE"]}


async def test_failed_job_sends_alert() -> None:
    async def broken() -> None:
        raise ValueError("bad data")

    notifier = FakeNotifier()
    scheduler = AsyncIOScheduler()
    alert_on_job_failures(scheduler, notifier)
    scheduler.start()
    scheduler.add_job(broken, id="ingest_eod_us:2026-10-05", next_run_time=datetime.now(UTC))
    for _ in range(50):
        if notifier.sent:
            break
        await asyncio.sleep(0.02)
    scheduler.shutdown(wait=False)
    assert notifier.sent == ["⚠️ Job ingest_eod_us:2026-10-05 failed: ValueError: bad data"]


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 10, 5), True),
        (date(2026, 10, 3), False),  # Saturday
        (date(2026, 12, 25), False),  # both closed
        (date(2026, 11, 26), True),  # Thanksgiving: NYSE closed, Xetra open
    ],
)
def test_trading_day(day: date, expected: bool) -> None:
    assert jobs.is_trading_day(day) is expected
