from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class RawJob(BaseModel):
    """Raw job data as scraped from source — stored verbatim in raw_jobs table."""

    title: str
    url: str
    company: str = ""
    location: str = ""
    description: str = ""
    salary: str = ""
    source: str = ""
    external_id: str = ""
    posted_at: Optional[datetime] = None
    # Full source payload; must NOT be '{}' — guards enforced in test_raw_data_preserved
    raw_data: dict = Field(default_factory=dict)
    content_hash: str = ""
    status: str = "new"  # CHECK IN ('new','refined','rejected','duplicate')


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
