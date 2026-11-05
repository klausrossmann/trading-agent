"""Telegram bot (long polling) restricted to the owner's chat, plus a log-only fallback."""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import structlog
from pydantic import SecretStr
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

log = structlog.get_logger(__name__)

MAX_MESSAGE = 4096
RETRY_SECONDS = 60


class Notifier(Protocol):
    async def send(self, text: str) -> None: ...


class LogNotifier:
    """Used when Telegram isn't configured: messages only go to the log."""

    async def send(self, text: str) -> None:
        log.info("notify.message", text=text)


@dataclass(frozen=True)
class Reply:
    """A text with rows of inline buttons: (label, callback data of at most 64 bytes)."""

    text: str
    buttons: tuple[tuple[tuple[str, str], ...], ...] = ()

    def markup(self) -> InlineKeyboardMarkup | None:
        if not self.buttons:
            return None
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(label, callback_data=data) for label, data in row]
                for row in self.buttons
            ]
        )


def as_reply(answer: "str | Reply") -> Reply:
    return answer if isinstance(answer, Reply) else Reply(answer)


@dataclass(frozen=True)
class Command:
    description: str
    run: Callable[[list[str]], Awaitable["str | Reply"]]  # args -> reply


@dataclass(frozen=True)
class Interaction:
    """Handlers for button presses (callback data -> new message content) and plain text."""

    on_button: Callable[[str], Awaitable[Reply]]
    on_text: Callable[[str], Awaitable[str | None]]  # None: not expected, show the help


class TelegramBot:
    def __init__(
        self,
        token: SecretStr,
        owner_chat_id: int | None,
        commands: Mapping[str, Command],
        interaction: Interaction | None = None,
    ) -> None:
        self._owner = owner_chat_id
        self._commands = dict(commands)
        self._interaction = interaction
        self._ready = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.app = Application.builder().token(token.get_secret_value()).build()
        self.app.add_handler(TypeHandler(Update, self.gate), group=-1)
        for name, command in self._commands.items():
            self.app.add_handler(CommandHandler(name, self.handler(command)))
        self.app.add_handler(CommandHandler(["help", "start"], self.help))
        self.app.add_handler(CallbackQueryHandler(self.button))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text))
        self.app.add_handler(MessageHandler(filters.ALL, self.help))

    async def gate(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Runs before every handler: anything not from the owner's chat stops here."""
        chat = update.effective_chat
        if self._owner is None or chat is None or chat.id != self._owner:
            log.warning(
                "telegram.ignored",
                chat_id=chat.id if chat else None,
                hint="set TELEGRAM_OWNER_CHAT_ID to this chat id" if self._owner is None else None,
            )
            raise ApplicationHandlerStop

    def help_text(self) -> str:
        lines = [f"/{name} - {c.description}" for name, c in self._commands.items()]
        return "\n".join(["Commands:", *lines, "/help - this list"])

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_message:
            await update.effective_message.reply_text(self.help_text())

    async def button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        if self._interaction is None or not query.data:
            return
        try:
            reply = await self._interaction.on_button(query.data)
        except Exception as exc:
            log.exception("telegram.button_failed")
            reply = Reply(f"⚠️ Failed: {type(exc).__name__}")
        await query.edit_message_text(reply.text[:MAX_MESSAGE], reply_markup=reply.markup())

    async def text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is None:
            return
        answer = None
        if self._interaction is not None and message.text:
            answer = await self._interaction.on_text(message.text)
        await message.reply_text(answer[:MAX_MESSAGE] if answer else self.help_text())

    def handler(
        self, command: Command
    ) -> Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, None]]:
        async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            try:
                reply = as_reply(await command.run(list(context.args or [])))
            except Exception as exc:
                log.exception("telegram.command_failed")
                reply = Reply(f"⚠️ Command failed: {type(exc).__name__}")
            if update.effective_message:
                await update.effective_message.reply_text(
                    reply.text[:MAX_MESSAGE], reply_markup=reply.markup()
                )

        return handle

    async def _run(self) -> None:
        while True:
            try:
                await self.app.initialize()
                await self.app.start()
                updater = self.app.updater
                if updater is not None:
                    await updater.start_polling(drop_pending_updates=True)
                self._ready.set()
                log.info("telegram.started", owner_configured=self._owner is not None)
                return
            except TelegramError as exc:
                log.warning(
                    "telegram.start_failed", error=type(exc).__name__, retry_s=RETRY_SECONDS
                )
                await asyncio.sleep(RETRY_SECONDS)

    def start(self) -> None:
        """Connect in the background, retrying, so a Telegram outage never blocks the agent."""
        self._task = asyncio.create_task(self._run())

    async def wait_ready(self, within_s: float) -> bool:
        try:
            await asyncio.wait_for(self._ready.wait(), within_s)
        except TimeoutError:
            return False
        return True

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        if not self._ready.is_set():
            return
        updater = self.app.updater
        if updater is not None and updater.running:
            await updater.stop()
        await self.app.stop()
        await self.app.shutdown()

    async def send(self, text: str) -> None:
        """Message the owner. Never raises: a failed message must not break a job."""
        if self._owner is None or not self._ready.is_set():
            log.info("notify.unsent", text=text, reason="telegram not ready or no owner chat")
            return
        try:
            await self.app.bot.send_message(self._owner, text[:MAX_MESSAGE])
        except TelegramError as exc:
            log.warning("telegram.send_failed", error=type(exc).__name__)


def build_notifier(
    token: SecretStr | None,
    owner_chat_id: int | None,
    commands: Mapping[str, Command],
    interaction: Interaction | None = None,
) -> TelegramBot | LogNotifier:
    if token is None or not token.get_secret_value():
        return LogNotifier()
    return TelegramBot(token, owner_chat_id, commands, interaction)
