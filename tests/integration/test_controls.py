"""Kill switch and live interlock end to end: Telegram commands, the CLI reset code, the DB."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from trading_agent.controls import CANCEL_BUTTON, STOP_BUTTON, ControlCenter
from trading_agent.data.ingest import Sessions
from trading_agent.db.models import AuditLog
from trading_agent.domain.risk import Mode
from trading_agent.notify import messages
from trading_agent.notify.telegram import Reply

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 7, 14, 0, tzinfo=UTC)


class Inbox:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
async def clean(sessions: Sessions) -> Sessions:
    async with sessions.begin() as s:
        await s.execute(text("TRUNCATE kill_switch, audit_log"))
    return sessions


def center(
    sessions: Sessions, mode: Mode = "paper", gateway: Mode | None = None
) -> tuple[ControlCenter, Inbox, Clock]:
    inbox, clock = Inbox(), Clock()
    return ControlCenter(sessions, mode, lambda: gateway, inbox, clock), inbox, clock


async def run(c: ControlCenter, command: str, *args: str) -> str:
    reply = await c.commands()[command].run(list(args))
    return reply.text if isinstance(reply, Reply) else reply


async def test_pause_resume_and_stop(clean: Sessions) -> None:
    c, inbox, _ = center(clean)
    assert (await c.controls()).trading == "active"
    assert await run(c, "resume") == "Already active."
    assert await run(c, "pause", "earnings", "season") == (
        "⏸ New entries paused (earnings season) until /resume"
    )
    assert (await c.controls()).trading == "paused"
    assert await run(c, "resume") == "▶️ New entries allowed again (resumed)"

    stop = await c.commands()["stop"].run([])
    assert isinstance(stop, Reply)
    assert [data for row in stop.buttons for _, data in row] == [STOP_BUTTON, CANCEL_BUTTON]
    assert (await c.on_button(CANCEL_BUTTON)).text == "Cancelled, nothing changed."
    assert (await c.kill_switch()).state == "active"
    assert (await c.on_button(STOP_BUTTON)).text.startswith("⛔ Halted: /stop.")
    assert (await c.controls()).trading == "halted"
    assert (await run(c, "pause")).startswith("Halted, /pause changes nothing.")
    assert (await run(c, "resume")).startswith("Halted, /resume doesn't apply.")
    assert inbox.sent == []  # command replies go to the chat, not as extra alerts

    async with clean() as s:
        events = (await s.execute(select(AuditLog).order_by(AuditLog.id))).scalars().all()
    assert [(e.actor, e.payload["state"]) for e in events] == [
        ("telegram", "paused"),
        ("telegram", "active"),
        ("telegram", "halted"),
    ]


async def test_reset_needs_the_code_from_the_host(clean: Sessions) -> None:
    c, _, clock = center(clean)
    assert await c.issue_reset_code() is None  # not halted
    assert (await run(c, "reset", "123456")).startswith("No reset code issued.")
    await c.halt("reconciliation mismatch")

    code = await c.issue_reset_code()
    assert code is not None
    assert len(code) == 6
    assert await run(c, "reset") == f"Usage: /reset CODE. {messages.RESET_HINT}"
    wrong = "000000" if code != "000000" else "111111"
    assert await run(c, "reset", wrong) == "Wrong reset code."
    clock.now += timedelta(minutes=16)
    assert (await run(c, "reset", code)).startswith("The reset code has expired.")

    code = await c.issue_reset_code()
    assert code is not None
    assert await run(c, "reset", code) == "▶️ New entries allowed again (reset)"
    assert (await c.kill_switch()).state == "active"
    assert (await run(c, "reset", code)).startswith("No reset code issued.")  # single use


async def test_automatic_changes_are_announced(clean: Sessions) -> None:
    c, inbox, clock = center(clean)
    await c.apply_trip("pause_day")
    assert inbox.sent == ["⏸ New entries paused (daily loss limit) until Thu 08 Oct 00:00"]
    await c.apply_trip("pause_day")
    assert len(inbox.sent) == 1

    clock.now = datetime(2026, 10, 7, 22, 0, tzinfo=UTC)  # midnight in Berlin
    assert (await c.kill_switch()).state == "active"
    assert inbox.sent[-1] == "▶️ New entries allowed again (pause ended)"
    assert (await c.kill_switch()).state == "active"
    assert len(inbox.sent) == 2

    await c.apply_trip("halt")
    assert inbox.sent[-1].startswith("⛔ Halted: drawdown limit.")
    assert (await c.halt("reconciliation mismatch")).reason == "drawdown limit"
    assert len(inbox.sent) == 3


async def test_live_interlock(clean: Sessions) -> None:
    paper, _, _ = center(clean)
    assert not paper.start_live_interlock()
    assert paper.interlock_line() is None
    assert await run(paper, "confirm_live", "123456") == "Paper mode, nothing to confirm."

    live, _, _ = center(clean, "live", "live")
    assert live.start_live_interlock()
    assert live.live_code is not None
    assert live.interlock_line() == "waiting for /confirm_live CODE"
    assert (await live.controls()).live_confirmed is False
    assert (await run(live, "confirm_live")).startswith("Wrong code.")
    assert (await run(live, "confirm_live", live.live_code)).startswith(
        "🔐 Live trading confirmed. Interlock: confirmed"
    )
    controls = await live.controls()
    assert (controls.app_mode, controls.gateway_mode, controls.live_confirmed) == (
        "live",
        "live",
        True,
    )
    live.start_live_interlock()  # a restart needs a new confirmation
    assert not live.live_confirmed

    on_paper_gateway, _, _ = center(clean, "live", "paper")
    assert on_paper_gateway.interlock_line() == "gateway is not live, no orders"
