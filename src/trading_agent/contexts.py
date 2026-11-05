"""The shared setup objects every stage works with: settings, providers, rules and agents,
built once at start (scheduler.serve) or per CLI command."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

import httpx
from pydantic_ai.models import Model

from trading_agent import telemetry
from trading_agent.agents import critic, proposer
from trading_agent.agents.critic import CriticAgent
from trading_agent.agents.proposer import ProposerAgent
from trading_agent.calc.fees import FeeSchedule
from trading_agent.data.ingest import Sessions
from trading_agent.data.series import EcbProvider, FredProvider
from trading_agent.data.universe import Universe
from trading_agent.data.yahoo import YahooProvider
from trading_agent.db.analyses import DbAnalysisStore
from trading_agent.domain.market import Market
from trading_agent.domain.risk import Mode
from trading_agent.domain.trading import Book, book_for_mode
from trading_agent.ibkr.market import IbkrPriceProvider
from trading_agent.llm.fake import fake_model
from trading_agent.llm.models import ModelsConfig, build_model, load_models_config
from trading_agent.llm.prompts import load_prompt
from trading_agent.llm.runner import LlmRunner
from trading_agent.modules import earnings as earnings_module
from trading_agent.modules import technical as technical_module
from trading_agent.modules.earnings import EarningsModule
from trading_agent.modules.technical import PlanRules, TechnicalModule
from trading_agent.notify.telegram import LogNotifier, Notifier
from trading_agent.risk.config import RiskConfig, load_risk_config
from trading_agent.settings import DataConfig, QualityConfig, Settings, load_data_config, load_fees
from trading_agent.strategies.news import NewsParams, load_news_params
from trading_agent.strategies.pullback import PullbackParams, load_params


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


@dataclass
class DataContext:
    """For the data jobs (jobs.py): the market-data providers."""

    sessions: Sessions
    cfg: DataConfig
    yahoo: YahooProvider
    ecb: EcbProvider
    fred: FredProvider | None
    notifier: Notifier = field(default_factory=LogNotifier)
    state: RuntimeState | None = None
    ibkr: IbkrPriceProvider | None = None

    @classmethod
    def build(
        cls,
        settings: Settings,
        cfg: DataConfig,
        sessions: Sessions,
        http: httpx.AsyncClient,
        notifier: Notifier | None = None,
        state: RuntimeState | None = None,
    ) -> "DataContext":
        key = settings.fred_api_key
        fred = FredProvider(http, key) if key is not None and key.get_secret_value() else None
        return cls(
            sessions,
            cfg,
            YahooProvider(),
            EcbProvider(http),
            fred,
            notifier or LogNotifier(),
            state,
        )


@dataclass
class BookContext:
    """For the briefing, the digest and reports: the rules and the book's settings."""

    sessions: Sessions
    pullback: PullbackParams
    risk: RiskConfig
    fees: FeeSchedule
    benchmarks: dict[Market, str]
    mode: Mode = "paper"
    news: NewsParams = field(default_factory=NewsParams)

    @classmethod
    def load(
        cls, config_dir: Path, sessions: Sessions, universe: Universe, mode: Mode = "paper"
    ) -> "BookContext":
        return cls(
            sessions,
            load_params(config_dir),
            load_risk_config(config_dir),
            load_fees(config_dir),
            dict(universe.benchmarks),
            mode,
            load_news_params(config_dir),
        )


@dataclass
class AnalysisContext:
    """For the scan and the news stages: the AI runner, the agents and the screen's rules."""

    sessions: Sessions
    book: Book  # the agent's own book: its open positions are not proposed again
    models: ModelsConfig
    runner: LlmRunner
    technical: TechnicalModule
    earnings: EarningsModule
    rules: PlanRules
    holding_sessions: int  # entry validity + time stop
    pullback: PullbackParams
    benchmarks: dict[Market, str]
    proposer: ProposerAgent
    critic: CriticAgent
    quality: QualityConfig
    blacklist: frozenset[str]
    news: NewsParams = field(default_factory=NewsParams)

    @classmethod
    def build(cls, settings: Settings, sessions: Sessions, universe: Universe) -> "AnalysisContext":
        cfg_dir = settings.config_dir
        prompts = settings.prompts_dir
        models = load_models_config(cfg_dir)
        factory: Callable[[str], Model] = (
            (lambda _: fake_model())
            if settings.llm_fake
            else partial(build_model, gemini_api_key=settings.gemini_api_key)
        )
        runner = LlmRunner(
            models,
            DbAnalysisStore(sessions),
            factory,
            dev_overrides=settings.llm_dev_overrides,
            tracer_provider=telemetry.provider(),
            trace_content=settings.trace_content,
        )
        p = load_params(cfg_dir)
        risk = load_risk_config(cfg_dir)
        return cls(
            sessions=sessions,
            book=book_for_mode(settings.app_mode),
            models=models,
            runner=runner,
            technical=TechnicalModule(
                load_prompt(prompts, technical_module.NAME, technical_module.PROMPT_VERSION)
            ),
            earnings=EarningsModule(
                load_prompt(prompts, earnings_module.NAME, earnings_module.PROMPT_VERSION)
            ),
            rules=PlanRules(
                min_risk_reward=float(risk.per_trade.min_risk_reward),
                stop_atr_min=float(risk.per_trade.stop_atr_min),
                stop_atr_max=float(risk.per_trade.stop_atr_max),
            ),
            holding_sessions=p.entry_valid_sessions + p.time_stop_sessions,
            pullback=p,
            benchmarks=dict(universe.benchmarks),
            proposer=ProposerAgent(load_prompt(prompts, proposer.NAME, proposer.PROMPT_VERSION)),
            critic=CriticAgent(load_prompt(prompts, critic.NAME, critic.PROMPT_VERSION)),
            quality=load_data_config(cfg_dir).quality,
            blacklist=frozenset(risk.instruments.blacklist),
            news=load_news_params(cfg_dir),
        )
