"""Records of LLM analyses and calls (IMPLEMENTATION.md 7)."""

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

Role = Literal["triage", "analysis", "proposer", "critic"]
AnalysisStatus = Literal["ok", "rejected"]


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


class StoredAnalysis(_Frozen):
    id: int
    output: dict[str, Any]
    issues: tuple[ValidationIssue, ...]
    status: AnalysisStatus
