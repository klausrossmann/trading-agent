"""One LLM analysis: cache, budget, pacing, validation with one corrective retry, recording."""

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal, Protocol

import structlog
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.exceptions import AgentRunError, ModelHTTPError
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model
from pydantic_ai.usage import RunUsage

from trading_agent.domain.analysis import (
    AnalysisRecord,
    AnalysisStatus,
    LlmCall,
    Role,
    StoredAnalysis,
    ValidationIssue,
)
from trading_agent.llm.budget import BudgetMode, budget_mode, month_start
from trading_agent.llm.models import ModelsConfig, ModelUnavailableError
from trading_agent.llm.prompts import Prompt

log = structlog.get_logger(__name__)

RETRYABLE_HTTP = {429, 500, 502, 503, 504}


class AnalysisModule[I: BaseModel, O: BaseModel](Protocol):
    """An analysis module: calculator facts in, one validated LLM judgement out."""

    name: str
    prompt: Prompt

    def output_type(self, inp: I) -> type[O]: ...

    def validate(self, inp: I, out: O) -> list[ValidationIssue]: ...

    def finalize(self, inp: I, out: O, issues: Sequence[ValidationIssue]) -> O:
        """Applies caps (e.g. confidence after possible fabrication) before the output is stored."""
        ...


class AnalysisStore(Protocol):
    async def cached(self, input_hash: str) -> StoredAnalysis | None: ...

    async def save(self, analysis: AnalysisRecord | None, calls: Sequence[LlmCall]) -> int | None:
        """Stores the analysis (if any) and its calls; returns the analysis id."""
        ...

    async def spent_usd(self, since: datetime) -> float: ...


@dataclass(frozen=True)
class Outcome[O: BaseModel]:
    module: str
    status: AnalysisStatus | Literal["skipped", "failed"]
    model: str
    output: O | None = None
    issues: tuple[ValidationIssue, ...] = ()
    analysis_id: int | None = None
    cost_usd: float = 0.0
    cached: bool = False
    reason: str | None = None


def input_hash(module: str, prompt_version: int, model: str, as_of: date, text: str) -> str:
    key = "\n".join((module, str(prompt_version), model, as_of.isoformat(), text))
    return hashlib.sha256(key.encode()).hexdigest()


def user_prompt(text: str) -> str:
    return f"Input (JSON):\n{text}"


def feedback(issues: Sequence[ValidationIssue]) -> str:
    lines = "\n".join(f"- {i.message}" for i in issues if i.severity == "error")
    return (
        f"Your answer failed these checks:\n{lines}\n"
        "Answer again in the same format and fix these problems. Use only level names and "
        "numbers from the input."
    )


def _errors(issues: Sequence[ValidationIssue]) -> bool:
    return any(i.severity == "error" for i in issues)


class LlmRunner:
    def __init__(
        self,
        cfg: ModelsConfig,
        store: AnalysisStore,
        model_factory: Callable[[str], Model],
        *,
        dev_overrides: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        retry_delays_s: Sequence[float] = (20.0, 60.0),
    ) -> None:
        self.cfg = cfg
        self.store = store
        self._model_factory = model_factory
        self._dev = dev_overrides
        self._clock = clock
        self._sleep = sleep
        self._retry_delays = tuple(retry_delays_s)
        self._last_call: dict[str, float] = {}

    async def mode(self) -> BudgetMode:
        spent = await self.store.spent_usd(month_start(self._clock()))
        return budget_mode(spent, self.cfg.budget)

    async def run[I: BaseModel, O: BaseModel](
        self, module: AnalysisModule[I, O], inp: I, *, instrument_id: int | None, as_of: date
    ) -> Outcome[O]:
        role = module.prompt.role
        model_name = self.cfg.model_for(role, dev=self._dev)
        out_type = module.output_type(inp)
        text = inp.model_dump_json()
        key = input_hash(module.name, module.prompt.version, model_name, as_of, text)

        if (hit := await self.store.cached(key)) is not None:
            return Outcome(
                module.name,
                hit.status,
                model_name,
                out_type.model_validate(hit.output),
                hit.issues,
                hit.id,
                cached=True,
            )

        def skipped(reason: str) -> Outcome[O]:
            log.warning("llm.skipped", module=module.name, model=model_name, reason=reason)
            return Outcome(module.name, "skipped", model_name, reason=reason)

        if self.cfg.price(model_name, self._clock().date()) is None:
            return skipped(f"no price for {model_name} in config/models.yaml")
        if await self.mode() == "stopped":
            return skipped("monthly LLM budget used up")
        try:
            model = self._model_factory(model_name)
        except ModelUnavailableError as exc:
            return skipped(str(exc))

        agent = Agent(model, output_type=out_type, instructions=module.prompt.text, retries=2)
        calls: list[LlmCall] = []

        async def call(prompt: str, history: list[ModelMessage] | None) -> Any:
            return await self._call(agent, prompt, history, calls, model_name, module.name, role)

        try:
            result = await call(user_prompt(text), None)
            out: O = result.output
            issues = module.validate(inp, out)
            if _errors(issues):
                result = await call(feedback(issues), result.all_messages())
                out = result.output
                issues = module.validate(inp, out)
        except AgentRunError as exc:
            await self.store.save(None, calls)
            reason = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("llm.failed", module=module.name, model=model_name, error=reason)
            cost = sum(c.cost_usd for c in calls)
            return Outcome(module.name, "failed", model_name, cost_usd=cost, reason=reason)

        out = module.finalize(inp, out, issues)
        status: AnalysisStatus = "rejected" if _errors(issues) else "ok"
        cost = sum(c.cost_usd for c in calls)
        record = AnalysisRecord(
            module=module.name,
            prompt_version=module.prompt.version,
            model=model_name,
            instrument_id=instrument_id,
            as_of=as_of,
            input_hash=key,
            input=inp.model_dump(mode="json"),
            output=out.model_dump(mode="json"),
            issues=tuple(issues),
            status=status,
            cost_usd=cost,
        )
        analysis_id = await self.store.save(record, calls)
        log.info(
            "llm.analysis",
            module=module.name,
            model=model_name,
            status=status,
            calls=len(calls),
            tokens_in=sum(c.tokens_in for c in calls),
            tokens_out=sum(c.tokens_out for c in calls),
            cost_usd=round(cost, 6),
            issues=[i.code for i in issues],
        )
        return Outcome(module.name, status, model_name, out, tuple(issues), analysis_id, cost)

    async def _pace(self, model_name: str) -> None:
        interval = self.cfg.min_interval_s(model_name)
        last = self._last_call.get(model_name)
        if last is not None and (wait := last + interval - time.monotonic()) > 0:
            await self._sleep(wait)
        self._last_call[model_name] = time.monotonic()

    async def _call(
        self,
        agent: Agent[None, Any],
        prompt: str,
        history: list[ModelMessage] | None,
        calls: list[LlmCall],
        model_name: str,
        module: str,
        role: Role,
    ) -> Any:
        day = self._clock().date()
        attempt = 0
        while True:
            await self._pace(model_name)
            usage = RunUsage()
            started = time.monotonic()
            error: str | None = None
            try:
                return await agent.run(prompt, message_history=history, usage=usage)
            except ModelHTTPError as exc:
                error = f"HTTP {exc.status_code}"
                if exc.status_code not in RETRYABLE_HTTP or attempt >= len(self._retry_delays):
                    raise
            except AgentRunError as exc:
                error = type(exc).__name__
                raise
            finally:
                calls.append(
                    LlmCall(
                        role=role,
                        model=model_name,
                        module=module,
                        tokens_in=usage.input_tokens,
                        tokens_out=usage.output_tokens,
                        requests=usage.requests,
                        cost_usd=self.cfg.cost_usd(
                            model_name, day, usage.input_tokens, usage.output_tokens
                        ),
                        latency_ms=round((time.monotonic() - started) * 1000),
                        error=error,
                    )
                )
            await self._sleep(self._retry_delays[attempt])
            attempt += 1
