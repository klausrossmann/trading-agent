import asyncio
from collections import defaultdict
from typing import Literal

import typer

from trading_agent.data.quality import QualityIssue
from trading_agent.settings import Settings

app = typer.Typer(no_args_is_help=True, add_completion=False)

MarketOption = typer.Option(None, help="US or EU; default both.")


@app.callback()
def main() -> None:
    """Trading agent command line."""


def _settings() -> Settings:
    from trading_agent.log import configure_logging

    settings = Settings()  # pyright: ignore[reportCallIssue]  # required fields come from env/secrets
    configure_logging(settings.log_level)
    return settings


@app.command()
def run() -> None:
    """Start the agent: scheduler and jobs, until SIGTERM/SIGINT."""
    from trading_agent.data.universe import load_universe
    from trading_agent.scheduler import serve
    from trading_agent.settings import load_data_config, load_schedule

    settings = _settings()
    cfg_dir = settings.config_dir
    asyncio.run(
        serve(settings, load_schedule(cfg_dir), load_data_config(cfg_dir), load_universe(cfg_dir))
    )


@app.command()
def backfill(market: Literal["US", "EU"] | None = MarketOption) -> None:
    """Load the universe and all market data. Idempotent and incremental: safe to re-run."""
    import httpx

    from trading_agent import jobs
    from trading_agent.data.universe import load_universe
    from trading_agent.db.session import create_engine, session_factory
    from trading_agent.settings import load_data_config

    settings = _settings()

    async def _run() -> list[jobs.IngestResult]:
        engine = create_engine(settings.database_url)
        try:
            async with httpx.AsyncClient() as http:
                cfg = load_data_config(settings.config_dir)
                ctx = jobs.DataContext.build(settings, cfg, session_factory(engine), http)
                return await jobs.backfill(ctx, load_universe(settings.config_dir), market)
        finally:
            await engine.dispose()

    results = asyncio.run(_run())
    for r in results:
        typer.echo(
            f"{r.job:<12} inserted={r.inserted:<7} updated={r.updated:<5} removed={r.removed:<4} "
            f"up_to_date={r.up_to_date:<4} restated={len(r.restated):<3} failed={len(r.failed)}"
        )
        for item, error in r.failed.items():
            typer.echo(f"  failed {item}: {error}")
        if r.skipped:
            typer.echo(f"  skipped (not configured): {', '.join(r.skipped)}")
    if any(r.failed for r in results):
        raise typer.Exit(1)


@app.command()
def quality(
    market: Literal["US", "EU"] | None = MarketOption,
    show_all: bool = typer.Option(False, "--all", help="Also list old, non-blocking issues."),
) -> None:
    """Data quality report for stored bars. Exit code 1 if any symbol is blocked from scans."""
    from datetime import UTC, datetime

    from trading_agent.data.ingest import run_quality
    from trading_agent.db.session import create_engine, session_factory
    from trading_agent.settings import load_data_config

    settings = _settings()
    cfg = load_data_config(settings.config_dir).quality

    async def _run() -> list[QualityIssue]:
        engine = create_engine(settings.database_url)
        try:
            return await run_quality(
                session_factory(engine), cfg, now=datetime.now(UTC), market=market
            )
        finally:
            await engine.dispose()

    issues = asyncio.run(_run())
    by_symbol: dict[str, list[str]] = defaultdict(list)
    for i in issues:
        if i.blocking or show_all:
            flag = "BLOCK" if i.blocking else "     "
            by_symbol[i.yahoo_symbol].append(
                f"  {flag} {i.check:<16} {i.date or ''!s:<10} {i.detail}"
            )
    for symbol in sorted(by_symbol):
        typer.echo(symbol)
        typer.echo("\n".join(by_symbol[symbol]))
    blocked = sorted({i.yahoo_symbol for i in issues if i.blocking})
    typer.echo(f"{len(issues)} issues, {len(blocked)} blocked symbols: {', '.join(blocked) or '-'}")
    if blocked:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
