import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import typer

from trading_agent.data.quality import QualityIssue
from trading_agent.domain.market import Market
from trading_agent.settings import Settings

if TYPE_CHECKING:
    from trading_agent.data.ingest import Sessions
    from trading_agent.jobs import SymbolAnalysis

app = typer.Typer(no_args_is_help=True, add_completion=False)

MarketOption = typer.Option(None, help="US or EU; default both.")
YearsOption = typer.Option(5, help="Trading period in years, ending at the last stored bar.")
OutputOption = typer.Option(None, help="Also write the report to this file.")
SymbolsArgument = typer.Argument(None, help="Yahoo symbols, e.g. AAPL SAP.DE; default top setups.")
TopOption = typer.Option(None, help="Number of baseline setups; default from models.yaml.")
CheckSymbolsArgument = typer.Argument(None, help="Yahoo symbols; default AAPL SAP.DE.")


@app.callback()
def main() -> None:
    """Trading agent command line."""


def _settings() -> Settings:
    from trading_agent import telemetry
    from trading_agent.log import configure_logging

    settings = Settings()  # pyright: ignore[reportCallIssue]  # required fields come from env/secrets
    configure_logging(settings.log_level)
    telemetry.configure(
        settings.trace_endpoint,
        headers=settings.trace_headers,
        environment=settings.trace_environment or settings.app_mode,
    )
    return settings


def _with_db[T](settings: Settings, job: Callable[["Sessions"], Awaitable[T]]) -> T:
    """Runs `job` with a session factory on the configured database, then closes the pool
    and flushes the traces."""
    from trading_agent import telemetry
    from trading_agent.db.session import create_engine, session_factory

    async def _run() -> T:
        engine = create_engine(settings.database_url)
        try:
            return await job(session_factory(engine))
        finally:
            await engine.dispose()

    try:
        return asyncio.run(_run())
    finally:
        telemetry.shutdown()


@app.command()
def run() -> None:
    """Start the agent: scheduler and jobs, until SIGTERM/SIGINT."""
    from trading_agent import telemetry
    from trading_agent.data.universe import load_universe
    from trading_agent.scheduler import serve
    from trading_agent.settings import load_data_config, load_schedule

    settings = _settings()
    cfg_dir = settings.config_dir
    try:
        asyncio.run(
            serve(
                settings, load_schedule(cfg_dir), load_data_config(cfg_dir), load_universe(cfg_dir)
            )
        )
    finally:
        telemetry.shutdown()


@app.command()
def backfill(market: Literal["US", "EU"] | None = MarketOption) -> None:
    """Load the universe and all market data. Idempotent and incremental: safe to re-run."""
    import httpx

    from trading_agent import jobs
    from trading_agent.data.universe import load_universe
    from trading_agent.settings import load_data_config

    settings = _settings()

    async def _run(sessions: "Sessions") -> list[jobs.IngestResult]:
        async with httpx.AsyncClient() as http:
            cfg = load_data_config(settings.config_dir)
            ctx = jobs.DataContext.build(settings, cfg, sessions, http)
            return await jobs.backfill(ctx, load_universe(settings.config_dir), market)

    results = _with_db(settings, _run)
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
    from trading_agent.settings import load_data_config

    settings = _settings()
    cfg = load_data_config(settings.config_dir).quality

    issues: list[QualityIssue] = _with_db(
        settings, lambda s: run_quality(s, cfg, now=datetime.now(UTC), market=market)
    )
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


@app.command()
def backtest(
    years: int = YearsOption,
    market: Literal["US", "EU"] | None = MarketOption,
    output: Path | None = OutputOption,
) -> None:
    """Backtest the rule-based baseline on stored data and print the report."""
    from datetime import timedelta

    from trading_agent import backtest as bt
    from trading_agent.data.universe import load_universe
    from trading_agent.evaluation import report
    from trading_agent.risk.config import load_risk_config
    from trading_agent.settings import load_fees

    settings = _settings()
    cfg_dir = settings.config_dir
    cfg, risk = bt.load_backtest_config(cfg_dir), load_risk_config(cfg_dir)
    fees, universe = load_fees(cfg_dir), load_universe(cfg_dir)

    data = _with_db(
        settings, lambda s: bt.load_market_data(s, dict(universe.benchmarks), cfg.pullback)
    )
    if not data.instruments:
        raise typer.BadParameter("no bars stored; run `trading-agent backfill` first")
    end = max(max(x.rows) for x in data.instruments.values())
    start = end - timedelta(days=round(365.25 * years))
    markets: list[Market] = [market] if market else list(risk.markets.paper)
    sections: list[str] = []
    for sleeve_markets, sleeve_risk in risk.sleeves(markets, "paper"):
        result = bt.run(data, cfg, sleeve_risk, fees, start=start, end=end, markets=sleeve_markets)
        capital = float(sleeve_risk.capital.agent_budget_eur)
        benchmarks = {
            f"Buy and hold {universe.benchmarks[m]}": curve
            for m in sleeve_markets
            if m in universe.benchmarks
            and (curve := bt.benchmark_equity(data, m, start, end, capital)) is not None
        }
        sections.append(
            report.render(
                title=f"Backtest: {bt.pullback.NAME} ({', '.join(sleeve_markets)})",
                start=start,
                end=end,
                capital=capital,
                trades=result.trades,
                equity=result.equity,
                invested=result.invested,
                benchmarks=benchmarks,
                signals=result.signals,
                rejections=result.rejections,
                notes=[
                    "Survivorship bias: today's index members over the whole period, which "
                    "flatters the result.",
                    "Daily bars: if stop and target are both inside a bar the stop counts; on "
                    "the entry bar only the stop is checked.",
                    "Risk engine rules applied: sizing, positions, sector and correlation "
                    "cluster caps, fee-to-risk, orders per day, settled cash, loss limits "
                    "(measured at each close). Not modelled: FX conversion costs, dividends.",
                    "Prices from Yahoo Finance; third-party fees are estimates (config/fees.yaml).",
                ],
            )
        )
    text = "\n".join(sections)
    typer.echo(text)
    if output:
        output.write_text(text, encoding="utf-8")


def _plan_line(item: "SymbolAnalysis") -> str:
    out = item.technical.output
    if out is None:
        return f"technical {item.technical.status} ({item.technical.reason})"
    line = f"{out.rating} q={out.setup_quality:.2f}"
    if out.entry_ref and out.stop_ref and out.target_ref:
        price = {lv.name: lv.price for lv in item.technical_input.levels}
        entry, stop, target = price[out.entry_ref], price[out.stop_ref], price[out.target_ref]
        rr = (target - entry) / (entry - stop) if entry > stop else float("nan")
        line += (
            f"  entry {out.entry_ref} {entry:.2f} / stop {out.stop_ref} {stop:.2f} / "
            f"target {out.target_ref} {target:.2f} (R:R {rr:.1f})"
        )
    return line


@app.command()
def analyse(
    symbols: list[str] | None = SymbolsArgument,
    market: Literal["US", "EU"] | None = MarketOption,
    top: int | None = TopOption,
) -> None:
    """LLM technical and earnings analysis of today's top baseline setups (or given symbols)."""
    from trading_agent import jobs, telemetry
    from trading_agent.data.universe import load_universe

    settings = _settings()

    async def _run(sessions: "Sessions") -> jobs.ScanResult:
        ctx = jobs.AnalysisContext.build(settings, sessions, load_universe(settings.config_dir))
        with telemetry.root_span("cli analyse", tags=("cli",)):
            return await jobs.analyse(ctx, symbols or None, market, top)

    result = _with_db(settings, _run)
    for item in result.items:
        e = item.earnings.output
        earnings = (
            f"{e.stance} (risk {e.event_risk})"
            if e
            else f"{item.earnings.status} ({item.earnings.reason})"
        )
        flags = [
            f"{o.module} {o.status}"
            for o in item.outcomes
            if o.status != "ok" and o.output is not None
        ]
        typer.echo(f"{item.symbol:<9} {_plan_line(item)}")
        typer.echo(f"{'':<9} earnings: {earnings}" + (f"  [{', '.join(flags)}]" if flags else ""))
    outcomes = [o for i in result.items for o in i.outcomes]
    typer.echo(
        f"{len(result.items)} symbols, {len(outcomes)} analyses "
        f"({sum(1 for o in outcomes if o.cached)} cached), cost ${result.cost_usd:.4f}, "
        f"budget mode {result.mode}"
    )
    if result.missing:
        typer.echo(f"no data: {', '.join(result.missing)}")
    if any(o.status in ("failed", "skipped") for o in outcomes):
        raise typer.Exit(1)


@app.command()
def propose(
    symbols: list[str] | None = SymbolsArgument,
    market: Literal["US", "EU"] | None = MarketOption,
) -> None:
    """Agent pipeline on today's top setups (or given symbols): proposals, no orders."""
    from trading_agent import jobs, pipeline, telemetry
    from trading_agent.data.universe import load_universe

    settings = _settings()

    async def _run(sessions: "Sessions") -> pipeline.ProposalRun:
        ctx = jobs.AnalysisContext.build(settings, sessions, load_universe(settings.config_dir))
        with telemetry.root_span(f"cli propose {market or 'all'}", tags=("cli",)):
            return await pipeline.propose(ctx, market, symbols or None)

    result = _with_db(settings, _run)
    typer.echo(pipeline.summary(result, market))
    for symbol, reason in result.skipped.items():
        typer.echo(f"  skipped {symbol}: {reason}")


@app.command(name="ibkr-check")
def ibkr_check(
    symbols: list[str] | None = CheckSymbolsArgument,
    order_test: bool = typer.Option(
        False, "--order-test", help="Also place and cancel a far-from-market 1-share limit."
    ),
) -> None:
    """Check the IB Gateway: account, positions, contract ids, bars vs. Yahoo, quotes, ticks."""
    from trading_agent import broker

    settings = _settings()
    lines = _with_db(
        settings,
        lambda s: broker.check(settings, s, symbols or ["AAPL", "SAP.DE"], order_test),
    )
    typer.echo("\n".join(lines))


@app.command(name="weekly-report")
def weekly_report(
    day: str | None = typer.Option(None, "--date", help="Week end, YYYY-MM-DD; default today."),
    output: Path | None = OutputOption,
) -> None:
    """Weekly report now (KPIs, costs, calibration, go-live gate); stores it, sends the summary."""
    from datetime import date

    from trading_agent import jobs, reports
    from trading_agent.data.universe import load_universe
    from trading_agent.notify.telegram import LogNotifier

    settings = _settings()
    week_end = date.fromisoformat(day) if day else None

    async def _run(sessions: "Sessions") -> str:
        ctx = jobs.BookContext.load(
            settings.config_dir, sessions, load_universe(settings.config_dir)
        )
        return await reports.weekly_report(ctx, LogNotifier(), week_end)

    text = _with_db(settings, _run)
    typer.echo(text)
    if output:
        output.write_text(text, encoding="utf-8")


@app.command()
def place(market: Literal["US", "EU"] = typer.Argument(..., help="US or EU")) -> None:
    """Run today's placement for MARKET now. Safe to repeat: proposals with a bracket are
    rejected as already pending, and no orderRef is sent twice."""
    from trading_agent import broker, jobs, scheduler, trading
    from trading_agent.controls import ControlCenter
    from trading_agent.data.universe import load_universe
    from trading_agent.notify.telegram import LogNotifier

    settings = _settings()

    async def _run(sessions: "Sessions") -> trading.Placement:
        link: broker.BrokerLink | None = None
        try:
            book = jobs.BookContext.load(
                settings.config_dir, sessions, load_universe(settings.config_dir)
            )
            center = ControlCenter(sessions, settings.app_mode)
            if settings.ib_enabled:
                link = broker.BrokerLink(settings, LogNotifier())
                await link.connect()
                center.gateway_mode = link.mode
            ctx = await scheduler.trading_context(
                settings, sessions, book, center, LogNotifier(), link
            )
            return await trading.place(ctx, market)
        finally:
            if link is not None:
                link.ib.disconnect()

    result = _with_db(settings, _run)
    for p in result.placed:
        typer.echo(f"placed {p.symbol} {p.quantity} @ {p.entry:.2f} (stop {p.stop:.2f})")
    for symbol, reason in result.rejected:
        typer.echo(f"not placed {symbol}: {reason}")
    if not result.placed and not result.rejected:
        typer.echo("nothing to place")


@app.command(name="tax-report")
def tax_report(
    year: int = typer.Argument(..., help="Calendar year, e.g. 2027."),
    book: Literal["agent_live", "agent_paper"] = typer.Option(
        "agent_live", help="agent_live (taxable) or agent_paper (dry run)."
    ),
    output: Path | None = OutputOption,
) -> None:
    """Yearly tax helper: share sales in EUR at trade-day ECB rates (not tax advice)."""
    from trading_agent import jobs, reports
    from trading_agent.data.universe import load_universe

    settings = _settings()

    async def _run(sessions: "Sessions") -> str:
        ctx = jobs.BookContext.load(
            settings.config_dir, sessions, load_universe(settings.config_dir)
        )
        return await reports.tax_report(ctx, year, book)

    text = _with_db(settings, _run)
    typer.echo(text)
    if output:
        output.write_text(text, encoding="utf-8")


@app.command()
def reset() -> None:
    """End a halt (kill switch): prints a code to confirm with /reset CODE in Telegram."""
    from trading_agent.controls import RESET_CODE_TTL, ControlCenter

    settings = _settings()
    code = _with_db(settings, lambda s: ControlCenter(s, settings.app_mode).issue_reset_code())
    if code is None:
        typer.echo("The agent is not halted; nothing to reset.")
        return
    minutes = int(RESET_CODE_TTL.total_seconds() // 60)
    typer.echo(f"Send /reset {code} to the bot within {minutes} minutes.")


@app.command()
def dashboard(
    address: str = typer.Option("127.0.0.1", help="Listen address; 0.0.0.0 inside the container."),
    port: int = typer.Option(8501),
) -> None:
    """Serve the read-only dashboard (Streamlit)."""
    from streamlit.web import cli as streamlit_cli

    script = Path(__file__).parent / "dashboard" / "app.py"
    streamlit_cli.main(
        [
            "run",
            str(script),
            f"--server.address={address}",
            f"--server.port={port}",
            "--server.headless=true",
            "--server.fileWatcherType=none",
            "--browser.gatherUsageStats=false",
            "--client.toolbarMode=viewer",
            "--logger.hideWelcomeMessage=true",  # it looks up the host's public IP
        ],
        prog_name="streamlit",
    )


if __name__ == "__main__":
    app()
