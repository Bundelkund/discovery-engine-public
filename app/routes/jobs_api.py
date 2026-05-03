import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.dependencies import ConsumerIdentity, get_supabase, require_scope
from app.models.responses import (
    JobDetailResponse,
    JobListItem,
    JobQueryResponse,
)
from app.repositories.jobs import JobRepository, _geocode_city, _haversine_km

logger = logging.getLogger(__name__)

jobs_api_router = APIRouter(prefix="/jobs", tags=["jobs-api"])

MAX_DESCRIPTION_LIST = 500


def _row_to_list_item(row: dict) -> JobListItem:
    raw_desc = row.get("description")
    if raw_desc is None:
        logger.warning("row_missing_description", extra={"job_id": row.get("id", "")})
        desc = ""
    else:
        desc = raw_desc
    if len(desc) > MAX_DESCRIPTION_LIST:
        desc = desc[:MAX_DESCRIPTION_LIST] + "..."

    keywords = row.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.split(",") if k.strip()]

    score_stage_1 = row.get("score_stage_1") or 0
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
        final_score=float(score_stage_1),
        score_stage_1=score_stage_1,
        archetype=row.get("archetype", ""),
    )


def _row_to_detail(row: dict) -> JobDetailResponse:
    keywords = row.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.split(",") if k.strip()]

    score_stage_1 = row.get("score_stage_1") or 0
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
        final_score=float(score_stage_1),
        score_stage_1=score_stage_1,
        archetype=row.get("archetype", ""),
    )


# ---------------------------------------------------------------------------
# Consumer-Agnostic Query API — Phase 3 (AC-001, AC-015-AC-018)
# ---------------------------------------------------------------------------


@jobs_api_router.get("")
async def list_jobs(
    consumer: ConsumerIdentity = Depends(require_scope("jobs:read")),
    *,
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
    limit: int = Query(50, ge=1, le=500, description="Max rows returned (default 50, max 500)"),
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

    rows, total = await repo.query(
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

    # Haversine post-filter: repo.query() applies SQL bounding-box prefilter;
    # here we refine to the exact circle. Rows missing location_lat/lon are
    # kept (legacy pre-migration data graceful fallback).
    if max_distance_km is not None and location is not None:
        coords = _geocode_city(location)
        if coords is None:
            logger.warning(
                "max_distance_km_skipped",
                extra={"reason": "location_not_geocodable", "location": location},
            )
        else:
            lat0, lon0 = coords
            refined: list[dict] = []
            for row in rows:
                row_lat = row.get("location_lat")
                row_lon = row.get("location_lon")
                if row_lat is None or row_lon is None:
                    refined.append(row)
                    continue
                if _haversine_km(lat0, lon0, float(row_lat), float(row_lon)) <= max_distance_km:
                    refined.append(row)
            total = len(refined)
            rows = refined

    logger.info(
        "jobs_query_served",
        extra={
            "consumer_id": consumer.id,
            "result_count": len(rows),
            "total": total,
            "filters": {
                "keywords_positive": keywords_positive or None,
                "keywords_negative": keywords_negative or None,
                "location": location,
                "max_age_days": max_age_days,
                "source": source or None,
                "company_domain": company_domain or None,
                "seniority": seniority,
                "min_salary": min_salary,
                "max_salary": max_salary,
                "max_distance_km": max_distance_km,
                "sort": sort,
                "limit": limit,
                "offset": offset,
            },
        },
    )

    return JobQueryResponse(
        jobs=[_row_to_list_item(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@jobs_api_router.get(
    "/{job_id}", dependencies=[Depends(require_scope("jobs:read"))]
)
async def get_job(
    job_id: str,
    supabase=Depends(get_supabase),
) -> JobDetailResponse:
    repo = JobRepository(supabase)
    row = await repo.get_by_id(job_id)
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    return _row_to_detail(row)
