from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class CompanyProfile(BaseModel):
    """Company enrichment data."""

    domain: str
    name: str = ""
    size: str = ""
    industry: str = ""
    location: str = ""
    linkedin: str = ""
    hunter_data: dict = Field(default_factory=dict)
    cvf_scores: dict = Field(default_factory=dict)
    hiring_signal_count: int = 0
    enriched_at: Optional[datetime] = None


class EnrichmentContext(BaseModel):
    """Context passed through enrichment pipeline."""

    jobs: list[dict] = Field(default_factory=list)
    source: str = ""
