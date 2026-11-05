"""The wiring: builds each component's setup from settings and config files, once at start
(scheduler.serve) or per CLI command. Components never import this module; they get only
the small setup they need (e.g. `ScreenSetup`, `AnalystSetup`, `BriefingSetup`)."""

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from pydantic_ai.models import Model

from trading_agent.analysts import earnings as earnings_module
from trading_agent.analysts import technical as technical_module
from trading_agent.analysts.earnings import EarningsModule
from trading_agent.analysts.stage import AnalystSetup
from trading_agent.analysts.technical import PlanRules, TechnicalModule
from trading_agent.core import telemetry
from trading_agent.core.db import Sessions
from trading_agent.core.settings import QualityConfig, Settings, load_data_config, load_fees
from trading_agent.critic import agent as critic
from trading_agent.critic.agent import CriticAgent
from trading_agent.llm.fake import fake_model
from trading_agent.llm.models import ModelsConfig, build_model, load_models_config
from trading_agent.llm.prompts import load_prompt
from trading_agent.llm.runner import LlmRunner
from trading_agent.llm.store import DbAnalysisStore
from trading_agent.marketdata.types import Market
from trading_agent.marketdata.universe import Universe
from trading_agent.news.rules import NewsParams, load_news_params
from trading_agent.portfolio.fees import FeeSchedule
from trading_agent.portfolio.types import Book, book_for_mode
from trading_agent.proposer import agent as proposer
from trading_agent.proposer.agent import ProposerAgent
from trading_agent.risk.config import RiskConfig, load_risk_config
from trading_agent.risk.types import Mode
from trading_agent.screen.rules import PullbackParams, load_params
from trading_agent.screen.stage import ScreenSetup
from trading_agent.telegram.briefing import BriefingSetup


@dataclass
class BookContext:
    """The book's rules and settings: for trading, the briefing, the digest and reports."""

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

    @property
    def book(self) -> Book:
        return book_for_mode(self.mode)

    @property
    def budget_eur(self) -> float:
        return float(self.risk.for_mode(self.mode).capital.agent_budget_eur)

    @property
    def briefing(self) -> BriefingSetup:
        return BriefingSetup(self.sessions, self.benchmarks, self.pullback)


@dataclass
class AnalysisContext:
    """For the scan: the AI runner, the agents and the screen's rules."""

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

    @property
    def screen(self) -> ScreenSetup:
        return ScreenSetup(
            self.sessions,
            self.pullback,
            self.benchmarks,
            self.quality,
            self.blacklist,
            self.news.max_candidates,
        )

    @property
    def analysts(self) -> AnalystSetup:
        return AnalystSetup(
            self.sessions,
            self.runner,
            self.technical,
            self.earnings,
            self.rules,
            self.holding_sessions,
            self.benchmarks,
        )

    async def scan_size(self) -> int:
        """Candidates per market and scan; fewer once the AI budget is in lean mode."""
        scan = self.models.scan
        return scan.candidates_lean if await self.runner.mode() == "lean" else scan.candidates

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
