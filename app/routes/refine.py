"""Refine trigger endpoint (Spec 11, A3).

POST /refine drains raw_jobs(status='new') through the RefinePipeline state
machine (parse -> dedup -> dq -> location -> score/gate/enrich -> upsert). Like
the ATS /scan endpoints it runs as a FastAPI BackgroundTask and returns 202
immediately. Idempotent: the pipeline only touches status='new' rows, so calling
this repeatedly (e.g. on an n8n cron after each scrape) is safe — a no-op when
there is nothing new.

This is a SEPARATE endpoint from the ATS-scanner /scan/{stage} cron; it does not
touch that router.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Query

from app.dependencies import get_supabase, require_scope
from app.services.refine_pipeline import RefinePipeline

logger = logging.getLogger(__name__)

refine_router = APIRouter(tags=["refine"])


async def _run_refine(limit: int) -> None:
    """Background worker: fresh Supabase client, one refine pass."""
    from app.dependencies import get_supabase as _gs

    supabase = _gs()
    try:
        summary = await RefinePipeline(supabase).run(limit=limit)
        logger.info("refine_run_done", extra=summary)
    except Exception as exc:  # noqa: BLE001
        logger.error("refine_run_failed", extra={"error": str(exc)})


@refine_router.post(
    "/refine",
    status_code=202,
    dependencies=[Depends(require_scope("scrape:trigger"))],
)
async def trigger_refine(
    background_tasks: BackgroundTasks,
    limit: int = Query(100, ge=1, le=1000, description="max raw_jobs to drain this pass"),
    supabase=Depends(get_supabase),
):
    """Kick off one refine pass over raw_jobs(status='new'). Returns 202 + run info."""
    background_tasks.add_task(_run_refine, limit)
    logger.info("refine_run_started", extra={"limit": limit})
    return {"status": "running", "limit": limit}
