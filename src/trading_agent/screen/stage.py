"""The screen stage: today's candidates for the analysts, and each stock's screen step."""

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime

from trading_agent.core.db import Sessions
from trading_agent.core.settings import QualityConfig
from trading_agent.core.steps import Step
from trading_agent.marketdata import ingest
from trading_agent.marketdata.types import Market
from trading_agent.screen import rules as pullback
from trading_agent.screen import screen


@dataclass(frozen=True)
class ScreenSetup:
    sessions: Sessions
    rules: pullback.PullbackParams
    benchmarks: Mapping[Market, str]
    quality: QualityConfig
    blacklist: frozenset[str]  # risk.yaml instruments.blacklist
    max_news: int  # news candidates per scan (strategies.yaml news.max_candidates)


async def screen_stocks(
    setup: ScreenSetup,
    market: Market | None,
    top: int,
    held: Collection[str] = (),
    now: datetime | None = None,
    signals: Mapping[int, float] | None = None,
) -> list[screen.ScreenResult]:
    """Every active stock with the screen's verdict; the passed ones (best relative strength
    first, at most `top`) are analysed. Blocked by the quality checks, blacklisted in risk.yaml
    or already held means stopped. Stocks with a news buy signal (`signals`: instrument id ->
    confidence) only need the basic rules; at most `max_news` of them."""
    now = now or datetime.now(UTC)
    data = await screen.load_market_data(setup.sessions, setup.benchmarks, setup.rules, now.date())
    issues = await ingest.run_quality(setup.sessions, setup.quality, now=now, market=market)
    excluded = {s: "blacklisted in risk.yaml" for s in setup.blacklist}
    for i in issues:
        if i.blocking:
            excluded[i.yahoo_symbol] = f"data quality: {i.check}"
    found = screen.select(screen.check(data, setup.rules, market), excluded, held, top)
    if not signals:
        return found
    from_news = screen.news_check(data, setup.rules, signals, market)
    return screen.merge(found, screen.select(from_news, excluded, held, setup.max_news))


def screen_step(day: date, r: screen.ScreenResult) -> Step:
    if not r.passed:
        summary = r.reason if r.source == pullback.NAME else f"news candidate: {r.reason}"
    elif r.source == pullback.NAME:
        summary = f"rank {r.rank} by relative strength"
    else:
        summary = f"news candidate {r.rank}: price, liquidity and report date ok"
    return Step(
        day=day,
        market=r.market,
        instrument_id=r.instrument_id,
        symbol=r.symbol,
        step="screen",
        status="passed" if r.passed else "stopped",
        summary=summary,
        input={"as_of": r.as_of.isoformat() if r.as_of else None, **r.values},
        output={"passed": r.passed, "rank": r.rank, "reason": r.reason, "source": r.source},
    )
