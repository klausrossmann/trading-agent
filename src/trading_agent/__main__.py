import asyncio
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import typer

from trading_agent.data.quality import QualityIssue
from trading_agent.domain.market import Market
from trading_agent.settings import Settings

if TYPE_CHECKING:
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
    from trading_agent.db.session import create_engine, session_factory
    from trading_agent.evaluation import report
    from trading_agent.risk.config import load_risk_config
    from trading_agent.settings import load_fees

    settings = _settings()
    cfg_dir = settings.config_dir
    cfg, risk = bt.load_backtest_config(cfg_dir), load_risk_config(cfg_dir)
    fees, universe = load_fees(cfg_dir), load_universe(cfg_dir)

    async def _load() -> bt.MarketData:
        engine = create_engine(settings.database_url)
        try:
            return await bt.load_market_data(
                session_factory(engine), dict(universe.benchmarks), cfg.pullback
            )
        finally:
            await engine.dispose()

    data = asyncio.run(_load())
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
                    "Not modelled yet: correlation clusters and loss limits (risk engine, M8), "
                    "FX conversion costs, dividends.",
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
    from trading_agent import jobs
    from trading_agent.data.universe import load_universe
    from trading_agent.db.session import create_engine, session_factory

    settings = _settings()

    async def _run() -> jobs.ScanResult:
        engine = create_engine(settings.database_url)
        try:
            ctx = jobs.AnalysisContext.build(
                settings, session_factory(engine), load_universe(settings.config_dir)
            )
            return await jobs.analyse(ctx, symbols or None, market, top)
        finally:
            await engine.dispose()

    result = asyncio.run(_run())
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
    from trading_agent import jobs, pipeline
    from trading_agent.data.universe import load_universe
    from trading_agent.db.session import create_engine, session_factory

    settings = _settings()

    async def _run() -> pipeline.ProposalRun:
        engine = create_engine(settings.database_url)
        try:
            ctx = jobs.AnalysisContext.build(
                settings, session_factory(engine), load_universe(settings.config_dir)
            )
            return await pipeline.propose(ctx, market, symbols or None)
        finally:
            await engine.dispose()

    result = asyncio.run(_run())
    typer.echo(pipeline.summary(result, market))
    for symbol, reason in result.skipped.items():
        typer.echo(f"  skipped {symbol}: {reason}")


@app.command(name="ibkr-check")
def ibkr_check(symbols: list[str] | None = CheckSymbolsArgument) -> None:
    """Check the IB Gateway: account, positions, contract ids, bars vs. Yahoo, quote type."""
    from trading_agent import broker
    from trading_agent.db.session import create_engine, session_factory

    settings = _settings()

    async def _run() -> list[str]:
        engine = create_engine(settings.database_url)
        try:
            return await broker.check(
                settings, session_factory(engine), symbols or ["AAPL", "SAP.DE"]
            )
        finally:
            await engine.dispose()

    typer.echo("\n".join(asyncio.run(_run())))


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
