"""Records of LLM analyses and calls (IMPLEMENTATION.md 7)."""

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

Role = Literal["triage", "analysis", "proposer", "critic"]
AnalysisStatus = Literal["ok", "rejected"]
CallKind = Literal["initial", "corrective", "http_retry"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class ValidationIssue(_Frozen):
    code: str  # e.g. "level_order", "risk_reward", "ungrounded_number"
    message: str
    severity: Literal["error", "warning"]  # errors trigger one retry, then the output is rejected


class LlmCall(_Frozen):
    role: Role
    model: str
    module: str
    tokens_in: int
    tokens_out: int
    requests: int
    cost_usd: float
    latency_ms: int
    error: str | None = None
    attempt: int = 1  # 1, 2, ... within the analysis
    kind: CallKind = "initial"
    trace_id: str | None = None  # None when tracing is off
    span_id: str | None = None  # the analysis span
    messages: list[dict[str, Any]] | None = None  # the run's PydanticAI messages
    finish_reason: str | None = None
    provider_response_id: str | None = None


class AnalysisRecord(_Frozen):
    module: str
    prompt_version: int
    model: str
    instrument_id: int | None
    as_of: date
    input_hash: str
    input: dict[str, Any]
    output: dict[str, Any]
    issues: tuple[ValidationIssue, ...]
    status: AnalysisStatus
    cost_usd: float
    trace_id: str | None = None


class StoredAnalysis(_Frozen):
    id: int
    output: dict[str, Any]
    issues: tuple[ValidationIssue, ...]
    status: AnalysisStatus
    trace_id: str | None = None


def trace_url(ui_url: str, trace_id: str | None) -> str | None:
    """Link to a trace in the tracing UI (Langfuse: https://cloud.langfuse.com/project/<id>)."""
    if not ui_url or not trace_id:
        return None
    return f"{ui_url.rstrip('/')}/traces/{trace_id}"
