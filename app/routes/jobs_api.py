import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.dependencies import get_consumer, get_supabase
from app.models.responses import (
    JobDetailResponse,
    JobListItem,
    JobQueryResponse,
)
from app.repositories.jobs import JobRepository

logger = logging.getLogger(__name__)

jobs_api_router = APIRouter(prefix="/jobs", tags=["jobs-api"])

MAX_DESCRIPTION_LIST = 500


def _compute_final_score(row: dict) -> float:
    """COALESCE(stage_3, stage_2, stage_1, 0)."""
    for field in ("score_stage_3", "score_stage_2", "score_stage_1"):
        val = row.get(field)
        if val is not None:
            return float(val)
    return 0.0


def _row_to_list_item(row: dict) -> JobListItem:
    desc = row.get("description") or ""
    if len(desc) > MAX_DESCRIPTION_LIST:
        desc = desc[:MAX_DESCRIPTION_LIST] + "..."

    keywords = row.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.split(",") if k.strip()]

    highlights = row.get("match_highlights") or []

    return JobListItem(
        id=row.get("id", ""),
        title=row.get("title", ""),
        company=row.get("company", ""),
        location=row.get("location", ""),
        remote=row.get("remote"),
        description=desc,
        url=row.get("url", ""),
        source=row.get("source", ""),
        salary=row.get("salary", ""),
        keywords=keywords,
        posted_at=row.get("posted_at") or row.get("scraped_at"),
        scraped_at=row.get("scraped_at"),
        company_domain=row.get("company_domain", ""),
        final_score=_compute_final_score(row),
        score_stage_1=row.get("score_stage_1") or 0,
        score_stage_2=row.get("score_stage_2"),
        score_stage_3=row.get("score_stage_3"),
        archetype=row.get("archetype", ""),
        match_reasoning=row.get("match_reasoning"),
        match_highlights=highlights,
        match_pitch=row.get("match_pitch"),
    )


def _row_to_detail(row: dict) -> JobDetailResponse:
    keywords = row.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.split(",") if k.strip()]

    highlights = row.get("match_highlights") or []

    return JobDetailResponse(
        id=row.get("id", ""),
        title=row.get("title", ""),
        company=row.get("company", ""),
        location=row.get("location", ""),
        remote=row.get("remote"),
        description=row.get("description", ""),
        url=row.get("url", ""),
        source=row.get("source", ""),
        external_id=row.get("external_id", ""),
        salary=row.get("salary", ""),
        keywords=keywords,
        job_type=row.get("job_type"),
        posted_at=row.get("posted_at") or row.get("scraped_at"),
        scraped_at=row.get("scraped_at"),
        content_hash=row.get("content_hash", ""),
        company_domain=row.get("company_domain", ""),
        metadata=row.get("metadata") or {},
        final_score=_compute_final_score(row),
        score_stage_1=row.get("score_stage_1") or 0,
        score_stage_2=row.get("score_stage_2"),
        score_stage_3=row.get("score_stage_3"),
        archetype=row.get("archetype", ""),
        match_reasoning=row.get("match_reasoning"),
        match_highlights=highlights,
        match_pitch=row.get("match_pitch"),
    )


# ---------------------------------------------------------------------------
# Consumer-Agnostic Query API — Phase 3 (AC-001, AC-015-AC-018)
# ---------------------------------------------------------------------------


@jobs_api_router.get("", dependencies=[Depends(get_consumer)])
async def list_jobs(
    # --- MUST-filter params (AC-001) ---
    keywords_positive: list[str] = Query(
        default=[], description="Keep rows where ANY keyword matches title OR description (ILIKE)"
    ),
    keywords_negative: list[str] = Query(
        default=[], description="Exclude rows where ANY keyword matches title OR description (ILIKE)"
    ),
    location: Optional[str] = Query(
        None, description="ILIKE match on location column (location_normalized post-Phase-4)"
    ),
    max_age_days: Optional[int] = Query(
        None, ge=1, description="Keep jobs scraped within last N days"
    ),
    exclude_domain: list[str] = Query(
        default=[], description="Exclude jobs from these company domains"
    ),
    sort: str = Query(
        "recency",
        pattern="^(recency|score_keyword)$",
        description="Sort order: recency (scraped_at DESC) or score_keyword (score_stage_1 DESC, NULL-last)",
    ),
    limit: int = Query(50, ge=1, le=100, description="Max rows returned (default 50, max 100)"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    # --- SHOULD-filter params (AC-015-AC-018) ---
    source: list[str] = Query(default=[], description="Exact-match whitelist on source column"),
    company_domain: list[str] = Query(
        default=[], description="Whitelist on company_domain (contrast to exclude_domain)"
    ),
    seniority: Optional[str] = Query(
        None,
        pattern="^(senior|junior|lead|mid)$",
        description="Filter by seniority via title ILIKE heuristic",
    ),
    min_salary: Optional[int] = Query(None, ge=0, description="Minimum salary_min value"),
    max_salary: Optional[int] = Query(None, ge=0, description="Maximum salary_max value"),
    max_distance_km: Optional[int] = Query(
        None,
        ge=1,
        description=(
            "Haversine distance filter in km. Requires `location` param to be set. "
            "Uses Python-Haversine post-query filter (PostGIS not installed). "
            "Pre-Phase-4 migration: silently skipped if location_lat/lon columns absent."
        ),
    ),
    supabase=Depends(get_supabase),
) -> JobQueryResponse:
    """Consumer-agnostic paginated job query — no profile_id required."""
    if not supabase:
        raise HTTPException(status_code=503, detail="Database client not initialised")

    if max_distance_km is not None and not location:
        raise HTTPException(
            status_code=400,
            detail="max_distance_km requires `location` to also be set",
        )

    repo = JobRepository(supabase)

    rows, total = repo.query(
        keywords_positive=keywords_positive or None,
        keywords_negative=keywords_negative or None,
        location=location,
        max_age_days=max_age_days,
        exclude_domain=exclude_domain or None,
        sort=sort,
        limit=limit,
        offset=offset,
        source=source or None,
        company_domain=company_domain or None,
        seniority=seniority,
        min_salary=min_salary,
        max_salary=max_salary,
        max_distance_km=max_distance_km,
    )

    return JobQueryResponse(
        jobs=[_row_to_list_item(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@jobs_api_router.get("/{job_id}", dependencies=[Depends(get_consumer)])
async def get_job(
    job_id: str,
    supabase=Depends(get_supabase),
) -> JobDetailResponse:
    repo = JobRepository(supabase)
    row = await repo.get_by_id(job_id)
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    return _row_to_detail(row)
