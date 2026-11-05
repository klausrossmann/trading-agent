"""Morning briefing and the Telegram answers to /status and /budget."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import structlog

from trading_agent.core.db import Sessions
from trading_agent.llm.budget import month_start
from trading_agent.llm.runner import LlmRunner
from trading_agent.llm.store import DbAnalysisStore
from trading_agent.marketdata import calendars
from trading_agent.marketdata import store as market_repo
from trading_agent.marketdata.indicators import bars_to_frame
from trading_agent.marketdata.trend import trend_states
from trading_agent.marketdata.types import Market
from trading_agent.screen import screen
from trading_agent.screen.rules import PullbackParams
from trading_agent.telegram import messages
from trading_agent.telegram.bot import Notifier

log = structlog.get_logger(__name__)


@dataclass
class RuntimeState:
    """What /status reports; updated by the running jobs."""

    mode: str
    started_at: datetime
    heartbeat_at: datetime | None = None
    heartbeat_ok: bool | None = None
    blocked: dict[str, list[str]] = field(default_factory=dict[str, list[str]])  # market -> symbols
    scheduler: Any = None  # AsyncIOScheduler, set once built
    gateway: Callable[[], str] | None = None  # IB Gateway status, when enabled
    kill_switch: Callable[[], Awaitable[str]] | None = None
    interlock: Callable[[], str | None] | None = None
    tracing: str = "off"
    trace_ui_url: str = ""
    dashboard_ui_url: str = ""


@dataclass(frozen=True)
class BriefingSetup:
    sessions: Sessions
    benchmarks: Mapping[Market, str]
    rules: PullbackParams  # the screen's rules, for today's setups


def is_trading_day(day: date) -> bool:
    return any(calendars.session_time(c, day, "open") is not None for c in ("XETR", "XNYS"))


async def _last_bars_by_market(sessions: Sessions) -> dict[str, date | None]:
    async with sessions() as s:
        instruments = await market_repo.active_instruments(s)
        last = await market_repo.last_bar_dates(s)
    out: dict[str, date | None] = {"US": None, "EU": None}
    for inst_id, d in last.items():
        inst = instruments.get(inst_id)
        if inst is not None:
            current = out[inst.market]
            out[inst.market] = d if current is None else max(current, d)
    return out


async def build_briefing(
    setup: BriefingSetup, blocked: Mapping[str, list[str]], today: date
) -> messages.Briefing:
    sessions = setup.sessions
    async with sessions() as s:
        instruments = await market_repo.active_instruments(s)
        by_symbol = {i.yahoo_symbol: k for k, i in instruments.items()}
        markets: list[messages.MarketLine] = []
        for symbol in setup.benchmarks.values():
            inst_id = by_symbol.get(symbol)
            bars = (
                await market_repo.bars(s, inst_id, today - timedelta(days=450)) if inst_id else []
            )
            if len(bars) >= 2:
                close = bars_to_frame(bars)["close"]
                markets.append(
                    messages.MarketLine(
                        name=symbol,
                        last_date=bars[-1].date,
                        close=float(bars[-1].close),
                        change_pct=float(bars[-1].close / bars[-2].close - 1) * 100,
                        trend=trend_states(close),
                    )
                )
        fx = await market_repo.fx_rates(s, "USD")
        usd_per_eur = float(fx[-1].value) if fx else None
        horizon = max(calendars.session_offset(c, today, 3) for c in ("XNYS", "XETR"))
        upcoming = await market_repo.upcoming_earnings(s, today, horizon)

    earnings_lines = [
        messages.EarningsLine(instruments[i].yahoo_symbol, d, timing)
        for i, d, timing in upcoming
        if i in instruments
    ]
    data = await screen.load_market_data(sessions, setup.benchmarks, setup.rules)
    setups = [r.symbol for r in screen.check(data, setup.rules) if r.passed]
    held_back = sorted(s for symbols in blocked.values() for s in symbols) if blocked else None
    return messages.Briefing(
        day=today,
        markets=markets,
        eur_usd=usd_per_eur,
        earnings=earnings_lines,
        setups=setups[:8],
        last_bars=await _last_bars_by_market(sessions),
        blocked=held_back,
    )


async def briefing_text(setup: BriefingSetup, blocked: Mapping[str, list[str]], today: date) -> str:
    return messages.render_briefing(await build_briefing(setup, blocked, today))


async def morning_briefing(
    setup: BriefingSetup,
    blocked: Mapping[str, list[str]],
    notifier: Notifier,
    today: date | None = None,
) -> str | None:
    """Scheduled on weekdays; sends only if Xetra or NYSE has a session today."""
    today = today or datetime.now(UTC).date()
    if not is_trading_day(today):
        log.info("briefing.skipped", reason="no session today")
        return None
    text = await briefing_text(setup, blocked, today)
    await notifier.send(text)
    return text


async def status_text(state: RuntimeState, sessions: Sessions, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    hidden = {"heartbeat", "plan_session_jobs"}
    next_jobs: list[tuple[str, datetime]] = []
    if state.scheduler is not None:
        next_jobs = sorted(
            (
                (j.id, j.next_run_time)
                for j in state.scheduler.get_jobs()
                if j.next_run_time and j.id not in hidden
            ),
            key=lambda x: x[1],
        )[:5]
    return messages.render_status(
        messages.StatusSnapshot(
            mode=state.mode,
            started_at=state.started_at,
            now=now,
            heartbeat_at=state.heartbeat_at,
            heartbeat_ok=state.heartbeat_ok,
            last_bars=await _last_bars_by_market(sessions),
            blocked=state.blocked,
            next_jobs=next_jobs,
            **({"gateway": state.gateway()} if state.gateway else {}),
            **({"kill_switch": await state.kill_switch()} if state.kill_switch else {}),
            interlock=state.interlock() if state.interlock else None,
            tracing=state.tracing,
            trace_ui_url=state.trace_ui_url,
            dashboard_ui_url=state.dashboard_ui_url,
        )
    )


async def budget_text(
    sessions: Sessions, runner: LlmRunner, monthly_usd: float, now: datetime | None = None
) -> str:
    since = month_start(now or datetime.now(UTC))
    store = DbAnalysisStore(sessions)
    spent = await store.spent_usd(since)
    return messages.render_budget(
        messages.BudgetSnapshot(
            month=since.date(),
            spent_usd=spent,
            monthly_usd=monthly_usd,
            mode=await runner.mode(),
            by_model=await store.spend_by_model(since),
        )
    )
