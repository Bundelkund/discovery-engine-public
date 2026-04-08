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
    duration_ms: int = 0
