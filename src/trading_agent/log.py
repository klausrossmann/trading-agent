import logging
import sys

import structlog
from opentelemetry import trace
from structlog.typing import EventDict, Processor, WrappedLogger

# These log full request URLs at INFO/DEBUG; some URLs (heartbeat, Telegram) contain credentials.
_QUIET_LOGGERS = ("httpx", "httpcore")


def add_trace_ids(_: WrappedLogger, __: str, event: EventDict) -> EventDict:
    """Inside a span: its trace and span id, so log lines match the trace (OBSERVABILITY.md 7)."""
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid:
        event.setdefault("trace_id", format(ctx.trace_id, "032x"))
        event.setdefault("span_id", format(ctx.span_id, "016x"))
    return event


def configure_logging(level: str, *, json: bool | None = None) -> None:
    """JSON lines when not attached to a terminal (Docker), readable output otherwise.

    Library logs (stdlib `logging`) go through the same renderer as structlog events.
    """
    if json is None:
        json = not sys.stderr.isatty()
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        add_trace_ids,
    ]
    renderer: list[Processor] = (
        [structlog.processors.dict_tracebacks, structlog.processors.JSONRenderer()]
        if json
        else [structlog.dev.ConsoleRenderer()]
    )
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, *renderer],
        )
    )
    logging.basicConfig(level=level, handlers=[handler], force=True)
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
    )
