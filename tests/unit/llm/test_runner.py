from collections.abc import Sequence
from datetime import UTC, date, datetime

import pytest
from pydantic import BaseModel
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel

from trading_agent.domain.analysis import ValidationIssue
from trading_agent.llm.models import ModelsConfig, ModelUnavailableError
from trading_agent.llm.prompts import Prompt
from trading_agent.llm.runner import LlmRunner, input_hash
from trading_agent.llm.store import MemoryStore

AS_OF = date(2026, 10, 2)
NOW = datetime(2026, 10, 4, 12, tzinfo=UTC)


class Inp(BaseModel):
    x: float


class Out(BaseModel):
    value: float
    note: str


class FakeModule:
    name = "fake"
    prompt = Prompt("fake", 1, "analysis", "Out", "Return a positive value.")

    def output_type(self, inp: Inp) -> type[Out]:
        return Out

    def validate(self, inp: Inp, out: Out) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if out.value <= 0:
            issues.append(
                ValidationIssue(code="neg", message="value must be > 0", severity="error")
            )
        if out.value > 100:
            issues.append(ValidationIssue(code="big", message="very big", severity="warning"))
        return issues

    def finalize(self, inp: Inp, out: Out, issues: Sequence[ValidationIssue]) -> Out:
        if any(i.code == "big" for i in issues):
            return out.model_copy(update={"note": "capped"})
        return out


def _cfg(model: str = "google:m", rpm: int | None = None) -> ModelsConfig:
    return ModelsConfig.model_validate(
        {
            "roles": {"analysis": model},
            "prices": {"google:m": [{"since": "2026-01-01", "input": 1.0, "output": 2.0}]},
            "budget": {"monthly_usd": 1.0, "lean_mode_at": 0.8, "hard_stop_at": 1.0},
            "rate_limits_rpm": {"google:m": rpm} if rpm else {},
            "scan": {"candidates": 10, "candidates_lean": 5},
        }
    )


class Script:
    """A FunctionModel that answers with the given values (or raises), in order."""

    def __init__(self, *answers: float | Exception) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        assert isinstance(last, ModelRequest)
        self.prompts += [str(p.content) for p in last.parts if isinstance(p, UserPromptPart)]
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        tool = info.output_tools[0].name
        return ModelResponse(parts=[ToolCallPart(tool, {"value": answer, "note": "n"})])

    def model(self, _: str) -> Model:
        return FunctionModel(self)


def _runner(
    script: Script, store: MemoryStore | None = None, cfg: ModelsConfig | None = None
) -> tuple[LlmRunner, MemoryStore, list[float]]:
    store = store or MemoryStore()
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    runner = LlmRunner(
        cfg or _cfg(), store, script.model, clock=lambda: NOW, sleep=sleep, retry_delays_s=(5, 9)
    )
    return runner, store, sleeps


async def test_ok_run_is_recorded_and_then_cached() -> None:
    script = Script(3.0)
    runner, store, _ = _runner(script)
    out = await runner.run(FakeModule(), Inp(x=1.5), instrument_id=7, as_of=AS_OF)
    assert out.status == "ok"
    assert not out.cached
    assert out.output == Out(value=3.0, note="n")
    assert out.analysis_id == 1
    assert out.cost_usd > 0
    assert script.prompts == ['Input (JSON):\n{"x":1.5}']
    (call,) = store.calls
    assert (call.role, call.module, call.model, call.error) == (
        "analysis",
        "fake",
        "google:m",
        None,
    )
    assert call.cost_usd == pytest.approx((call.tokens_in * 1.0 + call.tokens_out * 2.0) / 1e6)
    record = store.analyses[1]
    assert (record.instrument_id, record.as_of, record.input) == (7, AS_OF, {"x": 1.5})

    again = await runner.run(FakeModule(), Inp(x=1.5), instrument_id=7, as_of=AS_OF)
    assert again.cached
    assert again.status == "ok"
    assert again.output == out.output
    assert again.cost_usd == 0
    assert len(store.calls) == 1


async def test_validation_error_gets_one_retry_with_feedback() -> None:
    script = Script(-1.0, 2.0)
    runner, store, _ = _runner(script)
    out = await runner.run(FakeModule(), Inp(x=1), instrument_id=None, as_of=AS_OF)
    assert out.status == "ok"
    assert out.output is not None
    assert out.output.value == 2.0
    assert len(store.calls) == 2
    assert "value must be > 0" in script.prompts[1]
    assert out.cost_usd == pytest.approx(sum(c.cost_usd for c in store.calls))


async def test_second_failure_is_stored_as_rejected() -> None:
    runner, store, _ = _runner(Script(-1.0, -2.0))
    out = await runner.run(FakeModule(), Inp(x=1), instrument_id=None, as_of=AS_OF)
    assert out.status == "rejected"
    assert [i.code for i in out.issues] == ["neg"]
    assert store.analyses[1].status == "rejected"
    cached = await runner.run(FakeModule(), Inp(x=1), instrument_id=None, as_of=AS_OF)
    assert cached.cached
    assert cached.status == "rejected"


async def test_warnings_are_kept_and_finalize_applies_caps() -> None:
    runner, store, _ = _runner(Script(500.0))
    out = await runner.run(FakeModule(), Inp(x=1), instrument_id=None, as_of=AS_OF)
    assert out.status == "ok"
    assert out.output is not None
    assert out.output.note == "capped"
    assert len(store.calls) == 1
    assert store.analyses[1].output["note"] == "capped"


async def test_budget_modes_and_hard_stop() -> None:
    runner, _, _ = _runner(Script(), MemoryStore(spent_usd=0.85))
    assert await runner.mode() == "lean"
    runner, store, _ = _runner(Script(1.0), MemoryStore(spent_usd=1.0))
    out = await runner.run(FakeModule(), Inp(x=1), instrument_id=None, as_of=AS_OF)
    assert out.status == "skipped"
    assert out.reason == "monthly LLM budget used up"
    assert store.calls == []


async def test_unpriced_or_unavailable_models_are_never_called() -> None:
    runner, store, _ = _runner(Script(1.0), cfg=_cfg(model="google:unpriced"))
    out = await runner.run(FakeModule(), Inp(x=1), instrument_id=None, as_of=AS_OF)
    assert out.status == "skipped"
    assert "no price" in (out.reason or "")

    def unavailable(name: str) -> Model:
        raise ModelUnavailableError(f"{name} needs GEMINI_API_KEY")

    runner = LlmRunner(_cfg(), store, unavailable, clock=lambda: NOW)
    out = await runner.run(FakeModule(), Inp(x=1), instrument_id=None, as_of=AS_OF)
    assert out.status == "skipped"
    assert "GEMINI_API_KEY" in (out.reason or "")
    assert store.calls == []


async def test_rate_limit_errors_are_retried_after_a_pause() -> None:
    script = Script(ModelHTTPError(429, "m"), ModelHTTPError(503, "m"), 4.0)
    runner, store, sleeps = _runner(script)
    out = await runner.run(FakeModule(), Inp(x=1), instrument_id=None, as_of=AS_OF)
    assert out.status == "ok"
    assert sleeps == [5, 9]
    assert [c.error for c in store.calls] == ["HTTP 429", "HTTP 503", None]


async def test_other_errors_fail_the_run_and_keep_the_calls() -> None:
    runner, store, _ = _runner(Script(ModelHTTPError(400, "m")))
    out = await runner.run(FakeModule(), Inp(x=1), instrument_id=None, as_of=AS_OF)
    assert out.status == "failed"
    assert "400" in (out.reason or "")
    assert [c.error for c in store.calls] == ["HTTP 400"]
    assert store.analyses == {}


async def test_schema_errors_exhaust_output_retries_and_fail() -> None:
    def bad(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"value": "x"})])

    store = MemoryStore()
    runner = LlmRunner(_cfg(), store, lambda _: FunctionModel(bad), clock=lambda: NOW)
    out = await runner.run(FakeModule(), Inp(x=1), instrument_id=None, as_of=AS_OF)
    assert out.status == "failed"
    (call,) = store.calls
    assert call.requests == 3
    assert call.tokens_in > 0
    assert call.error == "UnexpectedModelBehavior"


async def test_calls_are_paced_per_model() -> None:
    runner, _, sleeps = _runner(Script(1.0, 2.0), cfg=_cfg(rpm=60))
    await runner.run(FakeModule(), Inp(x=1), instrument_id=None, as_of=AS_OF)
    await runner.run(FakeModule(), Inp(x=2), instrument_id=None, as_of=AS_OF)
    assert len(sleeps) == 1
    assert 0.9 < sleeps[0] <= 1.0


def test_input_hash_covers_module_version_model_and_date() -> None:
    h = input_hash("m", 1, "google:m", AS_OF, "{}")
    assert input_hash("m", 1, "google:m", AS_OF, "{}") == h
    variants = [
        input_hash("n", 1, "google:m", AS_OF, "{}"),
        input_hash("m", 2, "google:m", AS_OF, "{}"),
        input_hash("m", 1, "google:n", AS_OF, "{}"),
        input_hash("m", 1, "google:m", date(2026, 10, 5), "{}"),
        input_hash("m", 1, "google:m", AS_OF, '{"a":1}'),
    ]
    assert h not in variants
    assert len(set(variants)) == len(variants)
