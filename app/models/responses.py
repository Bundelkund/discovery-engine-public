from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class ScrapeResponse(BaseModel):
    source: str
    profile_id: str
    jobs_found: int = 0
    jobs_new: int = 0
    jobs_duplicate: int = 0
    jobs_below_threshold: int = 0
    jobs_stored: int = 0
    jobs_enriched: int = 0
    duration_ms: int = 0
    errors: list[str] = Field(default_factory=list)


class ScoreResponse(BaseModel):
    scored: int = 0
    stage1_passed: int = 0
    stage2_triggered: int = 0
    stage3_triggered: int = 0
    duration_ms: int = 0


# --- WA Provider API Response Models ---


class JobListItem(BaseModel):
    id: str
    title: str
    company: Optional[str] = ""
    location: Optional[str] = ""
    remote: Optional[bool] = None
    description: Optional[str] = ""
    url: Optional[str] = ""
    source: Optional[str] = ""
    salary: Optional[str] = ""
    keywords: list[str] = Field(default_factory=list)
    posted_at: Optional[datetime] = None
    scraped_at: Optional[datetime] = None
    company_domain: Optional[str] = None
    final_score: float = 0.0
    score_stage_1: int = 0
    score_stage_2: Optional[float] = None
    score_stage_3: Optional[float] = None
    archetype: Optional[str] = ""
    match_reasoning: Optional[str] = None
    match_highlights: list[str] = Field(default_factory=list)
    match_pitch: Optional[str] = None


class JobListResponse(BaseModel):
    jobs: list[JobListItem] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
    total_pages: int = 0


class JobDetailResponse(BaseModel):
    id: str
    title: str
    company: Optional[str] = ""
    location: Optional[str] = ""
    remote: Optional[bool] = None
    description: Optional[str] = ""
    url: Optional[str] = ""
    source: Optional[str] = ""
    external_id: Optional[str] = ""
    salary: Optional[str] = ""
    keywords: list[str] = Field(default_factory=list)
    job_type: Optional[str] = None
    posted_at: Optional[datetime] = None
    scraped_at: Optional[datetime] = None
    content_hash: Optional[str] = ""
    company_domain: Optional[str] = ""
    metadata: Optional[dict] = Field(default_factory=dict)
    final_score: float = 0.0
    score_stage_1: int = 0
    score_stage_2: Optional[float] = None
    score_stage_3: Optional[float] = None
    archetype: Optional[str] = ""
    match_reasoning: Optional[str] = None
    match_highlights: list[str] = Field(default_factory=list)
    match_pitch: Optional[str] = None


class CompanySignals(BaseModel):
    transformation_signal_score: float = 0.0
    signal_type: Optional[str] = None
    signal_evidence: Optional[str] = None
    kununu_score: Optional[float] = None
    kununu_sentiment: Optional[str] = None


class CompanyDetailResponse(BaseModel):
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
    signals: Optional[CompanySignals] = None


class ProfileSyncRequest(BaseModel):
    user_id: str
    name: str = ""
    cv_text: str = ""
    keywords_positive: list[str] = Field(default_factory=list)
    keywords_negative: list[str] = Field(default_factory=list)
    target_roles: list[str] = Field(default_factory=list)
    target_roles_primary: list[str] = Field(default_factory=list)
    target_roles_secondary: list[str] = Field(default_factory=list)
    target_locations: list[str] = Field(default_factory=list)
    negative_domains: list[str] = Field(default_factory=list)


class ProfileSyncResponse(BaseModel):
    profile_id: str
    status: str = "created"
    scoring_ready: bool = True
