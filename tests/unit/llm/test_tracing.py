"""LLM tracing: span tree, attributes, transcripts in the call log (OBSERVABILITY.md)."""

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime

import httpx2
import pytest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from pydantic import BaseModel
from pydantic_ai import models
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider

from trading_agent.domain.analysis import ValidationIssue
from trading_agent.llm.models import ModelsConfig
from trading_agent.llm.prompts import Prompt
from trading_agent.llm.runner import LlmRunner
from trading_agent.llm.store import MemoryStore

AS_OF = date(2026, 10, 2)
NOW = datetime(2026, 10, 4, 12, tzinfo=UTC)
META = "langfuse.observation.metadata."


class Inp(BaseModel):
    x: float


class Out(BaseModel):
    value: float


class Module:
    name = "fake"
    prompt = Prompt("fake", 1, "analysis", "Out", "Return a positive value.")

    def output_type(self, inp: Inp) -> type[Out]:
        return Out

    def validate(self, inp: Inp, out: Out) -> list[ValidationIssue]:
        if out.value > 0:
            return []
        return [ValidationIssue(code="neg", message="value must be > 0", severity="error")]

    def finalize(self, inp: Inp, out: Out, issues: object) -> Out:
        return out


def _cfg(model: str = "google:m") -> ModelsConfig:
    return ModelsConfig.model_validate(
        {
            "roles": {"analysis": model},
            "prices": {model: [{"since": "2026-01-01", "input": 1.0, "output": 2.0}]},
            "budget": {"monthly_usd": 1.0, "lean_mode_at": 0.8, "hard_stop_at": 1.0},
            "scan": {"candidates": 10, "candidates_lean": 5},
        }
    )


def _answers(*values: object) -> Model:
    queue = list(values)

    def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        value = queue.pop(0)
        if isinstance(value, Exception):
            raise value
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"value": value})])

    return FunctionModel(answer, model_name="m")


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    exp = InMemorySpanExporter()
    yield exp
    exp.clear()


def _runner(
    exporter: InMemorySpanExporter,
    model: Model,
    *,
    content: bool = True,
    store: MemoryStore | None = None,
    cfg: ModelsConfig | None = None,
) -> tuple[LlmRunner, MemoryStore]:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    store = store or MemoryStore()

    async def no_sleep(_: float) -> None:
        return None

    runner = LlmRunner(
        cfg or _cfg(),
        store,
        lambda _: model,
        clock=lambda: NOW,
        sleep=no_sleep,
        retry_delays_s=(1,),
        tracer_provider=provider,
        trace_content=content,
    )
    return runner, store


def _by_name(spans: tuple[ReadableSpan, ...]) -> dict[str, list[ReadableSpan]]:
    out: dict[str, list[ReadableSpan]] = {}
    for s in spans:
        out.setdefault(s.name, []).append(s)
    return out


def _hex(span: ReadableSpan) -> tuple[str, str]:
    ctx = span.get_span_context()
    assert ctx is not None
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")


async def test_corrective_retry_is_one_trace_with_two_agent_runs(
    exporter: InMemorySpanExporter,
) -> None:
    runner, store = _runner(exporter, _answers(-1.0, 2.0))
    out = await runner.run(Module(), Inp(x=1), instrument_id=7, as_of=AS_OF, subject="AAA")
    assert out.status == "ok"

    spans = _by_name(exporter.get_finished_spans())
    (analysis,) = spans["fake AAA"]
    runs = spans["invoke_agent fake"]
    chats = spans["chat m"]
    assert (len(runs), len(chats)) == (2, 2)
    assert analysis.parent is None
    analysis_ctx = analysis.get_span_context()
    assert analysis_ctx is not None
    assert all(r.parent is not None and r.parent.span_id == analysis_ctx.span_id for r in runs)
    assert {json.loads(str((r.attributes or {})["metadata"]))["kind"] for r in runs} == {
        "initial",
        "corrective",
    }

    attrs = analysis.attributes or {}
    assert attrs["langfuse.observation.type"] == "chain"
    assert attrs["langfuse.observation.input"] == '{"x":1.0}'
    assert attrs["langfuse.observation.output"] == '{"value":2.0}'
    assert attrs[META + "status"] == "ok"
    assert attrs[META + "module"] == "fake"
    assert attrs[META + "instrument_id"] == "7"
    assert attrs[META + "analysis_id"] == "1"
    assert attrs[META + "budget_mode"] == "normal"
    assert "langfuse.observation.level" not in attrs

    trace_id, span_id = _hex(analysis)
    first, second = store.calls
    assert [(c.attempt, c.kind) for c in store.calls] == [(1, "initial"), (2, "corrective")]
    assert all(c.trace_id == trace_id and c.span_id == span_id for c in store.calls)
    assert store.analyses[1].trace_id == trace_id
    assert first.messages is not None
    assert second.messages is not None
    assert '"value": -1.0' in json.dumps(first.messages)
    assert "value must be > 0" in json.dumps(second.messages)


async def test_cache_hit_points_to_the_trace_that_answered(exporter: InMemorySpanExporter) -> None:
    runner, _ = _runner(exporter, _answers(3.0))
    await runner.run(Module(), Inp(x=1), instrument_id=None, as_of=AS_OF)
    (first,) = _by_name(exporter.get_finished_spans())["fake"]
    exporter.clear()

    again = await runner.run(Module(), Inp(x=1), instrument_id=None, as_of=AS_OF)
    assert again.cached
    spans = _by_name(exporter.get_finished_spans())
    assert "invoke_agent fake" not in spans
    attrs = spans["fake"][0].attributes or {}
    assert attrs[META + "cached"] == "true"
    assert attrs[META + "cached_from_trace"] == _hex(first)[0]


async def test_http_retry_keeps_the_request_of_the_failed_attempt(
    exporter: InMemorySpanExporter,
) -> None:
    runner, store = _runner(exporter, _answers(ModelHTTPError(429, "m"), 4.0))
    out = await runner.run(Module(), Inp(x=1), instrument_id=None, as_of=AS_OF)
    assert out.status == "ok"
    failed, ok = store.calls
    assert [(c.attempt, c.kind, c.error) for c in store.calls] == [
        (1, "initial", "HTTP 429"),
        (2, "http_retry", None),
    ]
    assert failed.messages is not None
    assert [m["kind"] for m in failed.messages] == ["request"]
    assert failed.finish_reason is None
    assert ok.messages is not None
    assert any(m["kind"] == "response" for m in ok.messages)


async def test_failed_run_is_an_error_span_and_keeps_the_messages(
    exporter: InMemorySpanExporter,
) -> None:
    runner, store = _runner(exporter, _answers("x", "y", "z"))
    out = await runner.run(Module(), Inp(x=1), instrument_id=None, as_of=AS_OF)
    assert out.status == "failed"
    (analysis,) = _by_name(exporter.get_finished_spans())["fake"]
    assert analysis.status.status_code == StatusCode.ERROR
    attrs = analysis.attributes or {}
    assert attrs["langfuse.observation.level"] == "ERROR"
    assert "UnexpectedModelBehavior" in str(attrs["langfuse.observation.status_message"])
    (call,) = store.calls
    assert call.messages is not None
    assert sum(m["kind"] == "response" for m in call.messages) == 3


async def test_skipped_run_is_a_warning(exporter: InMemorySpanExporter) -> None:
    runner, _ = _runner(exporter, _answers(), store=MemoryStore(spent_usd=1.0))
    out = await runner.run(Module(), Inp(x=1), instrument_id=None, as_of=AS_OF)
    assert out.status == "skipped"
    attrs = _by_name(exporter.get_finished_spans())["fake"][0].attributes or {}
    assert attrs["langfuse.observation.level"] == "WARNING"
    assert attrs[META + "budget_mode"] == "stopped"


async def test_content_can_be_left_out_of_the_spans(exporter: InMemorySpanExporter) -> None:
    runner, store = _runner(exporter, _answers(2.0), content=False)
    await runner.run(Module(), Inp(x=1), instrument_id=None, as_of=AS_OF)
    spans = _by_name(exporter.get_finished_spans())
    analysis = spans["fake"][0].attributes or {}
    chat = spans["chat m"][0].attributes or {}
    assert "langfuse.observation.input" not in analysis
    assert "langfuse.observation.output" not in analysis
    assert "Input (JSON)" not in json.dumps(dict(chat))
    assert chat["gen_ai.usage.input_tokens"]
    assert store.calls[0].messages  # the database keeps the full record


async def test_without_tracing_the_call_log_still_has_the_messages() -> None:
    store = MemoryStore()
    runner = LlmRunner(_cfg(), store, lambda _: _answers(2.0), clock=lambda: NOW)
    await runner.run(Module(), Inp(x=1), instrument_id=None, as_of=AS_OF)
    (call,) = store.calls
    assert (call.trace_id, call.span_id) == (None, None)
    assert call.messages is not None
    assert isinstance(call.messages[0]["parts"][0], dict)


async def test_api_key_never_reaches_a_span(
    exporter: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "fake-gemini-key-" + "x" * 16
    seen: list[httpx2.Request] = []

    def gemini(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        body = json.loads(request.content)
        tool = body["tools"][0]["functionDeclarations"][0]["name"]
        call = {"functionCall": {"name": tool, "args": {"value": 2.0}}}
        return httpx2.Response(
            200,
            json={
                "candidates": [
                    {"content": {"role": "model", "parts": [call]}, "finishReason": "STOP"}
                ],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 3},
                "modelVersion": "gemini-test",
                "responseId": "resp-1",
            },
        )

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(gemini))
    model = GoogleModel("gemini-test", provider=GoogleProvider(api_key=key, http_client=client))
    runner, store = _runner(exporter, model, cfg=_cfg("google:gemini-test"))
    out = await runner.run(Module(), Inp(x=1), instrument_id=None, as_of=AS_OF)
    assert out.status == "ok", out.reason
    assert seen
    spans = exporter.get_finished_spans()
    assert {s.name for s in spans} >= {"fake", "invoke_agent fake", "chat gemini-test"}
    for span in spans:
        for value in (span.attributes or {}).values():
            assert key not in str(value), span.name
    (call,) = store.calls
    assert key not in json.dumps(call.model_dump())
    assert call.provider_response_id == "resp-1"
    assert call.finish_reason == "stop"
