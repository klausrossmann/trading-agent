"""Bot logic without network: fake updates instead of Telegram's servers."""

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr
from telegram.ext import ApplicationHandlerStop, ExtBot

from trading_agent.notify.telegram import Command, LogNotifier, TelegramBot, build_notifier

OWNER = 4242
TOKEN = SecretStr("123456:TEST-token")


async def _echo(args: list[str]) -> str:
    return f"args={args}"


async def _boom(args: list[str]) -> str:
    raise RuntimeError("secret detail")


def bot(owner: int | None = OWNER) -> TelegramBot:
    return TelegramBot(
        TOKEN, owner, {"status": Command("echo", _echo), "boom": Command("fails", _boom)}
    )


def update(chat_id: int | None) -> Any:
    chat = SimpleNamespace(id=chat_id) if chat_id is not None else None
    message = SimpleNamespace(reply_text=AsyncMock())
    return SimpleNamespace(effective_chat=chat, effective_message=message)


def context(*args: str) -> Any:
    return SimpleNamespace(args=list(args))


async def test_gate_lets_only_the_owner_through() -> None:
    b = bot()
    await b.gate(update(OWNER), context())  # no exception
    for stranger in (1, -100123, None):
        with pytest.raises(ApplicationHandlerStop):
            await b.gate(update(stranger), context())


async def test_without_owner_everyone_is_ignored() -> None:
    with pytest.raises(ApplicationHandlerStop):
        await bot(owner=None).gate(update(OWNER), context())


async def test_command_reply_and_failure() -> None:
    b = bot()
    u = update(OWNER)
    await b.handler(Command("echo", _echo))(u, context("a", "b"))
    u.effective_message.reply_text.assert_awaited_once_with("args=['a', 'b']")

    u = update(OWNER)
    await b.handler(Command("fails", _boom))(u, context())
    reply = u.effective_message.reply_text.await_args.args[0]
    assert reply == "⚠️ Command failed: RuntimeError"  # no exception text leaks


def test_help_lists_commands() -> None:
    assert bot().help_text().splitlines() == [
        "Commands:",
        "/status - echo",
        "/boom - fails",
        "/help - this list",
    ]


async def test_send_only_when_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    send = AsyncMock()
    monkeypatch.setattr(ExtBot, "send_message", send)
    b = bot()
    await b.send("hello")
    send.assert_not_awaited()  # not connected yet
    cast(Any, b)._ready.set()
    await b.send("x" * 5000)
    send.assert_awaited_once()
    assert send.await_args is not None
    assert send.await_args.args[0] == OWNER
    assert len(send.await_args.args[1]) == 4096


def test_notifier_choice() -> None:
    assert isinstance(build_notifier(None, OWNER, {}), LogNotifier)
    assert isinstance(build_notifier(SecretStr(""), OWNER, {}), LogNotifier)
    assert isinstance(build_notifier(TOKEN, OWNER, {}), TelegramBot)
