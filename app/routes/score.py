import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import load_scoring_config
from app.dependencies import get_supabase, require_api_key
from app.models.job import NormalizedJob
from app.models.profile import UserProfile
from app.models.responses import ScoreResponse
from app.repositories.jobs import JobRepository
from app.repositories.profiles import ProfileRepository
from app.scoring.pipeline import ScoringPipeline

score_router = APIRouter(tags=["scoring"])


class ScoreBatchRequest(BaseModel):
    profile_id: str
    filters: Optional[dict] = None


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

    profile = UserProfile(
        **{k: v for k, v in profile_data.items() if k in UserProfile.model_fields}
    )

    filters = request.filters or {}
    unscored = await job_repo.get_unscored(
        profile_id=request.profile_id,
        source=filters.get("source"),
        limit=filters.get("limit", 200),
    )

    if not unscored:
        return ScoreResponse(duration_ms=int((time.time() - start) * 1000))

    scoring_config = load_scoring_config().get("scoring", {})
    pipeline = ScoringPipeline(scoring_config)

    jobs = [
        NormalizedJob(
            **{k: v for k, v in j.items() if k in NormalizedJob.model_fields}
        )
        for j in unscored
    ]
    scored = await pipeline.run_stage1(jobs, profile)
    kept, _ = pipeline.filter_by_threshold(scored)

    stage2 = await pipeline.run_stage2(kept, profile)
    stage2_count = sum(1 for j in stage2 if j.score_stage_2 is not None)

    for job in stage2:
        if job.score_stage_2 is not None:
            await job_repo.update_scores(job.url, job.score_stage_2)

    return ScoreResponse(
        scored=len(scored),
        stage1_passed=len(kept),
        stage2_triggered=stage2_count,
        duration_ms=int((time.time() - start) * 1000),
    )
