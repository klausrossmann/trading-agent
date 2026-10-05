"""Company news (M9): headlines for held positions, triaged by a cheap model."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

Relevance = Literal["none", "low", "high"]


class NewsItem(BaseModel):
    """Headline and summary are untrusted text from outside sources."""

    model_config = ConfigDict(frozen=True)

    id: str  # "<provider>:<provider id>"
    instrument_id: int
    ts: datetime
    headline: str
    summary: str
    source: str
    url: str
    relevance: Relevance | None = None  # None until triaged
    note: str | None = None  # the triage model's reason
