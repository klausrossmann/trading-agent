"""LLM tracing setup (OBSERVABILITY.md): OpenTelemetry SDK, OTLP/HTTP export to Langfuse.

On as soon as secrets/trace_headers holds the Langfuse keys (TRACE_ENDPOINT defaults to Langfuse
EU). Export runs in the background with a bounded queue and a short timeout: a slow or
unreachable backend drops spans, never delays a job.
"""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import urlparse

import structlog
from opentelemetry import context, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span as ApiSpan
from pydantic import SecretStr

from trading_agent.domain.market import BERLIN

log = structlog.get_logger(__name__)

EXPORT_TIMEOUT_S = 5
MAX_QUEUE = 2048

_provider: TracerProvider | None = None
_state = "off"


def parse_headers(raw: str) -> dict[str, str]:
    """`key=value,key2=value2` (the OTEL_EXPORTER_OTLP_HEADERS format); values may contain `=`."""
    pairs = (item.partition("=") for item in raw.split(",") if item.strip())
    return {k.strip(): v.strip() for k, _, v in pairs if k.strip()}


class TraceAttributes(SpanProcessor):
    """Environment and session (the Berlin trading day) on every span: Langfuse filters per span."""

    def __init__(self, environment: str, clock: Callable[[], datetime] | None = None) -> None:
        self._environment = environment
        self._clock = clock or (lambda: datetime.now(BERLIN))

    def on_start(self, span: Span, parent_context: context.Context | None = None) -> None:
        span.set_attribute("langfuse.environment", self._environment)
        span.set_attribute("langfuse.session.id", self._clock().date().isoformat())


def configure(
    endpoint: str, *, headers: SecretStr | None, environment: str
) -> TracerProvider | None:
    """Sets the global tracer provider once; returns it, or None while the endpoint or the
    headers (the backend's keys) are empty."""
    global _provider, _state
    if _provider is not None:
        return _provider
    raw_headers = headers.get_secret_value().strip() if headers else ""
    if not endpoint:
        _state = "off (TRACE_ENDPOINT empty)"
        return None
    if not raw_headers:
        _state = "off (no keys in secrets/trace_headers)"
        return None
    provider = TracerProvider(
        resource=Resource.create(
            {SERVICE_NAME: "trading-agent", "deployment.environment.name": environment}
        )
    )
    provider.add_span_processor(TraceAttributes(environment))
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=endpoint, headers=parse_headers(raw_headers), timeout=EXPORT_TIMEOUT_S
            ),
            max_queue_size=MAX_QUEUE,
            export_timeout_millis=EXPORT_TIMEOUT_S * 1000,
        )
    )
    trace.set_tracer_provider(provider)
    _provider = provider
    _state = f"on ({urlparse(endpoint).hostname}, {environment})"
    log.info("tracing.on", endpoint=endpoint, environment=environment)
    return provider


def provider() -> TracerProvider | None:
    return _provider


def state() -> str:
    """For /status and the start log, e.g. `on (cloud.langfuse.com, paper)`."""
    return _state


def shutdown() -> None:
    """Flushes queued spans (bounded by the export timeout); call before the process exits."""
    if _provider is not None:
        _provider.shutdown()


@contextmanager
def root_span(
    name: str, *, tags: tuple[str, ...] = (), tracer_provider: TracerProvider | None = None
) -> Iterator[ApiSpan]:
    """A new trace named `name`, e.g. one job run or one CLI command."""
    tracer = trace.get_tracer(__name__, tracer_provider=tracer_provider)
    with tracer.start_as_current_span(name, context=context.Context()) as span:
        span.set_attribute("langfuse.trace.name", name)
        if tags:
            span.set_attribute("langfuse.trace.tags", list(tags))
        yield span


def traced[T](name: str, fn: Callable[[], Awaitable[T]]) -> Callable[[], Awaitable[T]]:
    """A scheduler job that runs in its own trace."""

    async def run() -> T:
        with root_span(name, tags=("job",)):
            return await fn()

    return run
