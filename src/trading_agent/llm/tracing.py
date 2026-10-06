"""Span helpers for LLM tracing (OBSERVABILITY.md).

OpenTelemetry API only: without a configured provider every call is a no-op. Attribute names
follow Langfuse's OTel mapping, so a backend switch only touches this module and `telemetry`.
"""

import json
from collections.abc import Mapping
from typing import Any, Literal

from opentelemetry.trace import Span, Status, StatusCode

Level = Literal["WARNING", "ERROR"]


def observe(
    span: Span,
    *,
    kind: str | None = None,
    input: str | None = None,
    output: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    level: Level | None = None,
    message: str | None = None,
) -> None:
    """Sets an observation's type, input, output, metadata and level; `None` values are skipped."""
    if not span.is_recording():
        return
    if kind is not None:
        span.set_attribute("langfuse.observation.type", kind)
    if input is not None:
        span.set_attribute("langfuse.observation.input", input)
    if output is not None:
        span.set_attribute("langfuse.observation.output", output)
    for key, value in (metadata or {}).items():
        if value is not None:
            text = value if isinstance(value, str) else json.dumps(value)
            span.set_attribute(f"langfuse.observation.metadata.{key}", text)
    if level is not None:
        span.set_attribute("langfuse.observation.level", level)
    if message is not None:
        span.set_attribute("langfuse.observation.status_message", message)
    if level == "ERROR":
        span.set_status(Status(StatusCode.ERROR, message))


def ids(span: Span) -> tuple[str | None, str | None]:
    """(trace id, span id) as lowercase hex, or (None, None) when tracing is off."""
    ctx = span.get_span_context()
    if not ctx.is_valid:
        return None, None
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
