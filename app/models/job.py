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
    archetype: str = ""
    company_domain: str = ""
    profile_id: str = ""
    # Bundle-B additive columns (migration bundle-b-additive.sql):
    location_normalized: Optional[str] = None
    location_lat: Optional[float] = None
    location_lon: Optional[float] = None
    is_remote: bool = False
    is_hybrid: bool = False
    dq_flags: dict = Field(default_factory=dict)


class ScorerResult(BaseModel):
    """Result from a single scorer."""

    scorer_id: str
    stage: int
    score: float
    details: dict = Field(default_factory=dict)
