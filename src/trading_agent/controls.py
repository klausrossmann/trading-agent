"""Kill switch and live interlock at runtime (IMPLEMENTATION.md 9.3, 10.4).

Telegram commands, alerts, the CLI reset code, and the `Controls` the risk engine reads.
"""

import hashlib
import hmac
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import structlog

from trading_agent.data.ingest import Sessions
from trading_agent.db import controls as repo
from trading_agent.domain.risk import Controls, KillSwitch, Mode, Trip
from trading_agent.notify import messages
from trading_agent.notify.telegram import Command, LogNotifier, Notifier, Reply
from trading_agent.risk import kill_switch

log = structlog.get_logger(__name__)

TZ = ZoneInfo("Europe/Berlin")
RESET_CODE_TTL = timedelta(minutes=15)
STOP_BUTTON = "k:stop"
CANCEL_BUTTON = "k:cancel"


def new_code() -> str:
    return f"{secrets.randbelow(10**6):06d}"


def _sha256(code: str) -> str:
    return hashlib.sha256(code.strip().encode()).hexdigest()


class ControlCenter:
    def __init__(
        self,
        sessions: Sessions,
        app_mode: Mode,
        gateway_mode: Callable[[], Mode | None] = lambda: None,
        notifier: Notifier | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.sessions = sessions
        self.app_mode: Mode = app_mode
        self.gateway_mode = gateway_mode
        self.notifier: Notifier = notifier or LogNotifier()
        self.clock = clock
        self.live_code: str | None = None
        self.live_confirmed = False

    async def _change(
        self, actor: str, step: Callable[[KillSwitch, datetime], KillSwitch]
    ) -> tuple[KillSwitch, KillSwitch]:
        """(state before, state after); stores and audits only real changes."""
        now = self.clock()
        async with self.sessions.begin() as s:
            stored = await repo.kill_switch(s, lock=True)
            new = step(stored, now)
            if new != stored:
                await repo.save_kill_switch(s, new, actor)
        if new != stored:
            log.info("kill_switch.changed", actor=actor, state=new.state, reason=new.reason)
        return stored, new

    async def _announce(
        self, actor: str, step: Callable[[KillSwitch, datetime], KillSwitch]
    ) -> KillSwitch:
        old, new = await self._change(actor, step)
        if new != old:
            await self.notifier.send(messages.kill_switch_alert(new))
        return new

    async def kill_switch(self) -> KillSwitch:
        """The current state; an expired pause is stored as over and announced."""
        return await self._announce("schedule", kill_switch.current)

    async def halt(self, reason: str, actor: str = "agent") -> KillSwitch:
        return await self._announce(actor, lambda ks, now: kill_switch.halt(ks, reason, now))

    async def apply_trip(self, trip: Trip) -> KillSwitch:
        return await self._announce(
            "risk", lambda ks, now: kill_switch.apply_trip(ks, trip, now, TZ)
        )

    async def controls(self) -> Controls:
        return Controls(
            trading=(await self.kill_switch()).state,
            app_mode=self.app_mode,
            gateway_mode=self.gateway_mode(),
            live_confirmed=self.live_confirmed,
        )

    # --- live interlock (10.4) ---

    def start_live_interlock(self) -> bool:
        """In live mode: a new code in the host logs; orders wait for /confirm_live CODE."""
        self.live_confirmed = False
        if self.app_mode != "live":
            return False
        self.live_code = new_code()
        log.warning("live.confirm_code", code=self.live_code)
        return True

    def interlock_line(self) -> str | None:
        if self.app_mode != "live":
            return None
        if self.gateway_mode() != "live":
            return "gateway is not live, no orders"
        return "confirmed" if self.live_confirmed else "waiting for /confirm_live CODE"

    # --- reset of a halt: code from the CLI on the host, confirmed in Telegram ---

    async def issue_reset_code(self) -> str | None:
        """None if not halted. The code is stored only as a hash and expires."""
        code = new_code()
        async with self.sessions.begin() as s:
            ks = await repo.kill_switch(s, lock=True)
            if ks.state != "halted":
                return None
            await repo.set_reset_code(s, _sha256(code), self.clock() + RESET_CODE_TTL)
        return code

    async def _reset(self, code: str) -> str:
        async with self.sessions() as s:
            stored = await repo.reset_code(s)
        if stored is None:
            return f"No reset code issued. {messages.RESET_HINT}"
        sha256, expires = stored
        if self.clock() > expires:
            return f"The reset code has expired. {messages.RESET_HINT}"
        if not hmac.compare_digest(sha256, _sha256(code)):
            log.warning("kill_switch.reset_code_wrong")
            return "Wrong reset code."
        old, new = await self._change("telegram", kill_switch.reset)
        return messages.kill_switch_alert(new) if new != old else "Not halted."

    # --- Telegram ---

    def commands(self) -> dict[str, Command]:
        async def pause(args: list[str]) -> str:
            reason = " ".join(args) or "/pause"
            _, new = await self._change(
                "telegram", lambda ks, now: kill_switch.pause(ks, reason, now)
            )
            if new.state == "halted":
                return f"Halted, /pause changes nothing. {messages.RESET_HINT}"
            return messages.kill_switch_alert(new)

        async def resume(_: list[str]) -> str:
            old, new = await self._change("telegram", kill_switch.resume)
            if new.state == "halted":
                return f"Halted, /resume doesn't apply. {messages.RESET_HINT}"
            return messages.kill_switch_alert(new) if new != old else "Already active."

        async def stop(_: list[str]) -> Reply:
            return Reply(
                messages.STOP_CONFIRM,
                ((("⛔ Stop trading", STOP_BUTTON), ("Cancel", CANCEL_BUTTON)),),
            )

        async def reset(args: list[str]) -> str:
            if not args:
                return f"Usage: /reset CODE. {messages.RESET_HINT}"
            return await self._reset(args[0])

        async def confirm_live(args: list[str]) -> str:
            if self.app_mode != "live" or self.live_code is None:
                return "Paper mode, nothing to confirm."
            if not args or not hmac.compare_digest(args[0].strip(), self.live_code):
                log.warning("live.confirm_wrong")
                return "Wrong code. It is in the host logs (live.confirm_code)."
            self.live_confirmed = True
            log.info("live.confirmed")
            return f"🔐 Live trading confirmed. Interlock: {self.interlock_line()}"

        return {
            "pause": Command("block new entries until /resume", pause),
            "resume": Command("allow new entries again after /pause", resume),
            "stop": Command("kill switch: halt the agent (asks to confirm)", stop),
            "reset": Command("end a halt with the code from `trading-agent reset`", reset),
            "confirm_live": Command(
                "confirm live trading with the code from the host logs", confirm_live
            ),
        }

    async def on_button(self, data: str) -> Reply:
        if data == STOP_BUTTON:
            _, new = await self._change(
                "telegram", lambda ks, now: kill_switch.halt(ks, "/stop", now)
            )
            return Reply(messages.kill_switch_alert(new))
        return Reply("Cancelled, nothing changed.")
