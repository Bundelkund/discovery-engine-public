from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class RawJob(BaseModel):
    """Raw job data as scraped from source."""

    title: str
    url: str
    company: str = ""
    location: str = ""
    description: str = ""
    salary: str = ""
    source: str = ""
    external_id: str = ""
    posted_at: Optional[datetime] = None
    raw_data: dict = Field(default_factory=dict)


class NormalizedJob(BaseModel):
    """Job after normalization - ready for scoring."""

    title: str
    url: str
    company: str = ""
    location: str = ""
    description: str = ""
    salary: str = ""
    source: str
    external_id: str = ""
    posted_at: Optional[datetime] = None
    content_hash: str = ""


class ScoredJob(BaseModel):
    """Job after scoring - ready for storage."""

    title: str
    url: str
    company: str = ""
    location: str = ""
    description: str = ""
    salary: str = ""
    source: str
    external_id: str = ""
    posted_at: Optional[datetime] = None
    content_hash: str = ""
    score_stage_1: int = 0
    score_stage_2: Optional[float] = None
    archetype: str = ""
    company_domain: str = ""
    profile_id: str = ""
    score_stage_3: Optional[float] = None
    match_reasoning: Optional[str] = None
    match_highlights: Optional[list[str]] = None
    match_pitch: Optional[str] = None


class ScorerResult(BaseModel):
    """Result from a single scorer."""

    scorer_id: str
    stage: int
    score: float
    details: dict = Field(default_factory=dict)
