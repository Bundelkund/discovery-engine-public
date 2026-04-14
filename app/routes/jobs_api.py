import math
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.dependencies import get_supabase, require_api_key
from app.models.responses import (
    JobDetailResponse,
    JobListItem,
    JobListResponse,
)
from app.repositories.jobs import JobRepository

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


@jobs_api_router.get("", dependencies=[Depends(require_api_key)])
async def list_jobs(
    profile_id: str = Query(..., description="Profile ID for scored jobs"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    sort: str = Query("final_score", pattern="^(final_score|scraped_at|company)$"),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
    search: Optional[str] = Query(None, min_length=2),
    source: Optional[str] = None,
    score_min: Optional[float] = Query(None, ge=0),
    archetype: Optional[str] = None,
    supabase=Depends(get_supabase),
) -> JobListResponse:
    repo = JobRepository(supabase)

    total = await repo.count_jobs(
        profile_id=profile_id,
        search=search,
        source=source,
        score_min=score_min,
        archetype=archetype,
    )

    rows = await repo.list_jobs(
        profile_id=profile_id,
        page=page,
        page_size=page_size,
        sort=sort,
        sort_dir=sort_dir,
        search=search,
        source=source,
        score_min=score_min,
        archetype=archetype,
    )

    return JobListResponse(
        jobs=[_row_to_list_item(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size) if total > 0 else 0,
    )


@jobs_api_router.get("/{job_id}", dependencies=[Depends(require_api_key)])
async def get_job(
    job_id: str,
    supabase=Depends(get_supabase),
) -> JobDetailResponse:
    repo = JobRepository(supabase)
    row = await repo.get_by_id(job_id)
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    return _row_to_detail(row)
