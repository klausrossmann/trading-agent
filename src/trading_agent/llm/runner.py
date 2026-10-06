"""One LLM analysis: cache, budget, pacing, validation with one corrective retry, recording."""

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal, Protocol

import structlog
from opentelemetry import trace
from opentelemetry.trace import Span, TracerProvider
from pydantic import BaseModel
from pydantic_ai import Agent, capture_run_messages
from pydantic_ai.capabilities import Instrumentation
from pydantic_ai.exceptions import AgentRunError, ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter, ModelResponse
from pydantic_ai.models import Model
from pydantic_ai.models.instrumented import InstrumentationSettings
from pydantic_ai.usage import RunUsage

from trading_agent.domain.analysis import (
    AnalysisRecord,
    AnalysisStatus,
    CallKind,
    LlmCall,
    Role,
    StoredAnalysis,
    ValidationIssue,
)
from trading_agent.llm import tracing
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


def _transcript(messages: list[ModelMessage]) -> dict[str, Any]:
    last = next((m for m in reversed(messages) if isinstance(m, ModelResponse)), None)
    return {
        "messages": ModelMessagesTypeAdapter.dump_python(messages, mode="json"),
        "finish_reason": last.finish_reason if last else None,
        "provider_response_id": last.provider_response_id if last else None,
    }


def _level(outcome: Outcome[Any]) -> tracing.Level | None:
    if outcome.status == "failed":
        return "ERROR"
    return "WARNING" if outcome.status in ("skipped", "rejected") else None


@dataclass(frozen=True)
class _CallContext:
    model: str
    module: str
    role: Role
    kind: CallKind
    trace_id: str | None
    span_id: str | None


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
        tracer_provider: TracerProvider | None = None,
        trace_content: bool = True,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self._model_factory = model_factory
        self._dev = dev_overrides
        self._clock = clock
        self._sleep = sleep
        self._retry_delays = tuple(retry_delays_s)
        self._last_call: dict[str, float] = {}
        self._tracer = trace.get_tracer(__name__, tracer_provider=tracer_provider)
        self._content = trace_content
        self._instrumentation = (
            [
                Instrumentation(
                    settings=InstrumentationSettings(
                        tracer_provider=tracer_provider,
                        include_content=trace_content,
                        include_binary_content=False,
                    )
                )
            ]
            if tracer_provider is not None
            else []
        )

    async def mode(self) -> BudgetMode:
        spent = await self.store.spent_usd(month_start(self._clock()))
        return budget_mode(spent, self.cfg.budget)

    async def run[I: BaseModel, O: BaseModel](
        self,
        module: AnalysisModule[I, O],
        inp: I,
        *,
        instrument_id: int | None,
        as_of: date,
        subject: str | None = None,
    ) -> Outcome[O]:
        """`subject` (e.g. the symbol) names the trace span next to the module."""
        name = f"{module.name} {subject}" if subject else module.name
        with self._tracer.start_as_current_span(name) as span:
            outcome = await self._run(module, inp, instrument_id, as_of, span)
            out = outcome.output
            tracing.observe(
                span,
                output=out.model_dump_json() if out is not None and self._content else None,
                metadata={
                    "status": outcome.status,
                    "cached": outcome.cached,
                    "cost_usd": round(outcome.cost_usd, 6),
                    "analysis_id": outcome.analysis_id,
                    "issues": [i.code for i in outcome.issues] or None,
                },
                level=_level(outcome),
                message=outcome.reason,
            )
            return outcome

    async def _run[I: BaseModel, O: BaseModel](
        self,
        module: AnalysisModule[I, O],
        inp: I,
        instrument_id: int | None,
        as_of: date,
        span: Span,
    ) -> Outcome[O]:
        role = module.prompt.role
        model_name = self.cfg.model_for(role, dev=self._dev)
        out_type = module.output_type(inp)
        text = inp.model_dump_json()
        key = input_hash(module.name, module.prompt.version, model_name, as_of, text)
        tracing.observe(
            span,
            kind="chain",
            input=text if self._content else None,
            metadata={
                "module": module.name,
                "prompt_version": module.prompt.version,
                "model": model_name,
                "role": role,
                "instrument_id": instrument_id,
                "as_of": as_of.isoformat(),
                "input_hash": key,
            },
        )

        if (hit := await self.store.cached(key)) is not None:
            tracing.observe(span, metadata={"cached_from_trace": hit.trace_id})
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
        mode = await self.mode()
        tracing.observe(span, metadata={"budget_mode": mode})
        if mode == "stopped":
            return skipped("monthly LLM budget used up")
        try:
            model = self._model_factory(model_name)
        except ModelUnavailableError as exc:
            return skipped(str(exc))

        agent = Agent(
            model,
            output_type=out_type,
            instructions=module.prompt.text,
            name=module.name,
            retries=2,
            capabilities=self._instrumentation,
        )
        calls: list[LlmCall] = []
        trace_id, span_id = tracing.ids(span)

        async def call(prompt: str, history: list[ModelMessage] | None, kind: CallKind) -> Any:
            call_ctx = _CallContext(model_name, module.name, role, kind, trace_id, span_id)
            return await self._call(agent, prompt, history, calls, call_ctx)

        try:
            result = await call(user_prompt(text), None, "initial")
            out: O = result.output
            issues = module.validate(inp, out)
            if _errors(issues):
                result = await call(feedback(issues), result.all_messages(), "corrective")
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
            trace_id=trace_id,
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
        ctx: _CallContext,
    ) -> Any:
        day = self._clock().date()
        retry = 0
        while True:
            await self._pace(ctx.model)
            usage = RunUsage()
            started = time.monotonic()
            error: str | None = None
            kind: CallKind = ctx.kind if retry == 0 else "http_retry"
            attempt = len(calls) + 1
            # Captures the messages even when the run raises.
            with capture_run_messages() as messages:
                try:
                    return await agent.run(
                        prompt,
                        message_history=history,
                        usage=usage,
                        metadata={"attempt": attempt, "kind": kind},
                    )
                except ModelHTTPError as exc:
                    error = f"HTTP {exc.status_code}"
                    if exc.status_code not in RETRYABLE_HTTP or retry >= len(self._retry_delays):
                        raise
                except AgentRunError as exc:
                    error = type(exc).__name__
                    raise
                finally:
                    calls.append(
                        LlmCall(
                            role=ctx.role,
                            model=ctx.model,
                            module=ctx.module,
                            tokens_in=usage.input_tokens,
                            tokens_out=usage.output_tokens,
                            requests=usage.requests,
                            cost_usd=self.cfg.cost_usd(
                                ctx.model, day, usage.input_tokens, usage.output_tokens
                            ),
                            latency_ms=round((time.monotonic() - started) * 1000),
                            error=error,
                            attempt=attempt,
                            kind=kind,
                            trace_id=ctx.trace_id,
                            span_id=ctx.span_id,
                            **_transcript(messages),
                        )
                    )
            await self._sleep(self._retry_delays[retry])
            retry += 1
