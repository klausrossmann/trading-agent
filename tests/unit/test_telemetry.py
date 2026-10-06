from datetime import datetime

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr

from trading_agent import telemetry
from trading_agent.domain.analysis import trace_url
from trading_agent.domain.market import BERLIN
from trading_agent.log import add_trace_ids


@pytest.fixture
def provider() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(
        telemetry.TraceAttributes(
            "paper", clock=lambda: datetime(2026, 10, 6, 0, 30, tzinfo=BERLIN)
        )
    )
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    return tp, exporter


def test_headers_keep_base64_padding() -> None:
    raw = "Authorization=Basic cGstbGY6c2stbGY=, x-langfuse-ingestion-version=4,"
    assert telemetry.parse_headers(raw) == {
        "Authorization": "Basic cGstbGY6c2stbGY=",
        "x-langfuse-ingestion-version": "4",
    }


def test_root_span_starts_a_trace_with_environment_and_trading_day(
    provider: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    tp, exporter = provider
    tracer = tp.get_tracer("t")
    with tracer.start_as_current_span("outer"):  # noqa: SIM117
        with telemetry.root_span("scan_us", tags=("job",), tracer_provider=tp):
            with tracer.start_as_current_span("technical AAA"):
                pass
    spans = {s.name: s for s in exporter.get_finished_spans()}
    root, child, outer = spans["scan_us"], spans["technical AAA"], spans["outer"]
    root_ctx, outer_ctx = root.get_span_context(), outer.get_span_context()
    assert root_ctx is not None
    assert outer_ctx is not None
    assert root.parent is None
    assert root_ctx.trace_id != outer_ctx.trace_id
    assert child.parent is not None
    assert child.parent.span_id == root_ctx.span_id
    attrs = root.attributes or {}
    assert attrs["langfuse.trace.name"] == "scan_us"
    assert tuple(attrs["langfuse.trace.tags"]) == ("job",)  # pyright: ignore[reportArgumentType]
    for span in (root, child):
        assert (span.attributes or {})["langfuse.environment"] == "paper"
        assert (span.attributes or {})["langfuse.session.id"] == "2026-10-06"


def test_tracing_stays_off_without_endpoint_or_keys() -> None:
    assert telemetry.configure("", headers=SecretStr("a=b"), environment="paper") is None
    assert telemetry.state() == "off (TRACE_ENDPOINT empty)"
    endpoint = "https://cloud.langfuse.com/api/public/otel/v1/traces"
    assert telemetry.configure(endpoint, headers=SecretStr(" \n"), environment="paper") is None
    assert telemetry.configure(endpoint, headers=None, environment="paper") is None
    assert telemetry.state() == "off (no keys in secrets/trace_headers)"
    assert telemetry.provider() is None
    telemetry.shutdown()


def test_log_lines_carry_the_trace_ids(
    provider: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    tp, _ = provider
    assert add_trace_ids(None, "info", {"event": "x"}) == {"event": "x"}
    with tp.get_tracer("t").start_as_current_span("s") as span:
        event = add_trace_ids(None, "info", {"event": "x"})
    ctx = span.get_span_context()
    assert event["trace_id"] == format(ctx.trace_id, "032x")
    assert event["span_id"] == format(ctx.span_id, "016x")


def test_trace_url() -> None:
    base = "https://cloud.langfuse.com/project/p1/"
    assert trace_url(base, "abc") == "https://cloud.langfuse.com/project/p1/traces/abc"
    assert trace_url("", "abc") is None
    assert trace_url(base, None) is None
