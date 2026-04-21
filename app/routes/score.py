import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import load_scoring_config
from app.dependencies import get_supabase, require_api_key
from app.utils.profile_mapper import map_profile_data
from app.models.job import NormalizedJob, ScoredJob
from app.models.profile import UserProfile
from app.models.responses import ScoreResponse
from app.repositories.jobs import JobRepository
from app.repositories.profiles import ProfileRepository
from app.scoring.pipeline import ScoringPipeline

score_router = APIRouter(tags=["scoring"])


class ScoreBatchRequest(BaseModel):
    profile_id: str
    filters: Optional[dict] = None
    rescore: bool = False


def _build_normalized_jobs(rows: list[dict]) -> list[NormalizedJob]:
    return [
        NormalizedJob(
            **{k: (v if v is not None else "")
               for k, v in row.items()
               if k in NormalizedJob.model_fields}
        )
        for row in rows
    ]


_STR_FIELDS = {
    "title", "url", "company", "location", "description", "salary",
    "source", "external_id", "content_hash", "archetype",
    "company_domain", "profile_id",
}


def _build_scored_jobs(rows: list[dict], profile_id: str) -> list[ScoredJob]:
    """Lift already-scored DB rows back into ScoredJob objects for stage 2/3."""
    jobs: list[ScoredJob] = []
    for row in rows:
        payload: dict = {}
        for k, v in row.items():
            if k not in ScoredJob.model_fields:
                continue
            if v is None and k in _STR_FIELDS:
                payload[k] = ""
            elif v == "" and k not in _STR_FIELDS:
                continue
            else:
                payload[k] = v
        payload["score_stage_1"] = row.get("score_stage_1") or 0
        payload["profile_id"] = profile_id
        jobs.append(ScoredJob(**payload))
    return jobs


@score_router.post("/score/batch", dependencies=[Depends(require_api_key)])
async def score_batch(
    request: ScoreBatchRequest, supabase=Depends(get_supabase)
):
    start = time.time()

    profile_repo = ProfileRepository(supabase)
    job_repo = JobRepository(supabase)

    profile_data = await profile_repo.get(request.profile_id)
    if not profile_data:
        raise HTTPException(status_code=404, detail="Profile not found")

    map_profile_data(profile_data)
    profile = UserProfile(
        **{k: v for k, v in profile_data.items() if k in UserProfile.model_fields}
    )

    scoring_config = load_scoring_config().get("scoring", {})
    pipeline = ScoringPipeline(scoring_config)

    filters = request.filters or {}
    limit = filters.get("limit", 200)
    source = filters.get("source")

    if request.rescore:
        stage2_cfg = next(
            (s for s in scoring_config.get("stages", []) if s.get("stage") == 2),
            {},
        )
        stage1_min = stage2_cfg.get("gate_threshold", 50)
        rows = await job_repo.get_needs_rescore(
            profile_id=request.profile_id,
            stage1_min=stage1_min,
            source=source,
            limit=limit,
        )
        if not rows:
            return ScoreResponse(duration_ms=int((time.time() - start) * 1000))

        kept = _build_scored_jobs(rows, request.profile_id)
        stage1_passed = len(kept)
        scored_total = len(kept)
    else:
        unscored = await job_repo.get_unscored(
            profile_id=request.profile_id,
            source=source,
            limit=limit,
        )
        if not unscored:
            return ScoreResponse(duration_ms=int((time.time() - start) * 1000))

        normalized = _build_normalized_jobs(unscored)
        scored = await pipeline.run_stage1(normalized, profile)

        for job in scored:
            if job.score_stage_1 is not None:
                await job_repo.update_stage1_score(
                    job.url, job.score_stage_1, job.archetype,
                    profile_id=request.profile_id,
                )

        kept, _ = pipeline.filter_by_threshold(scored)
        stage1_passed = len(kept)
        scored_total = len(scored)

    stage2 = await pipeline.run_stage2(kept, profile)
    stage2_count = sum(1 for j in stage2 if j.score_stage_2 is not None)

    for job in stage2:
        if job.score_stage_2 is not None:
            await job_repo.update_scores(job.url, job.score_stage_2)

    stage3 = await pipeline.run_stage3(stage2, profile)
    stage3_count = sum(1 for j in stage3 if j.score_stage_3 is not None)

    for job in stage3:
        if job.score_stage_3 is not None:
            await job_repo.update_stage3_score(
                job.url, job.score_stage_3,
                job.match_reasoning, job.match_highlights,
                job.match_pitch,
            )

    return ScoreResponse(
        scored=scored_total,
        stage1_passed=stage1_passed,
        stage2_triggered=stage2_count,
        stage3_triggered=stage3_count,
        duration_ms=int((time.time() - start) * 1000),
    )
