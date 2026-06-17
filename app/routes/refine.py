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

from app.dependencies import require_scope
from app.services.refine_runner import drain

logger = logging.getLogger(__name__)

refine_router = APIRouter(tags=["refine"])

# Single-flight, the drain loop, and the scheduler now live in
# app/services/refine_runner.py so the manual endpoint and the internal scheduler
# share ONE guard (two separate guards = no mutual exclusion between them). The
# engine drains autonomously via that scheduler; this endpoint stays as a manual
# kick / external-cron safety net and is fully idempotent.


async def _run_refine(limit: int, max_passes: int) -> None:
    """Background worker: one full drain cycle (single-flight via refine_runner)."""
    summary = await drain(limit=limit, max_passes=max_passes)
    logger.info("refine_run_done", extra=summary)


@refine_router.post(
    "/refine",
    status_code=202,
    dependencies=[Depends(require_scope("scrape:trigger"))],
)
async def trigger_refine(
    background_tasks: BackgroundTasks,
    limit: int = Query(100, ge=1, le=1000, description="max raw_jobs per pass"),
    max_passes: int = Query(
        1, ge=1, le=1000,
        description="passes this call: 1 = single pass (default), higher = drain-until-empty",
    ),
):
    """Kick off a refine drain over raw_jobs(status='new'). Returns 202 + run info.

    ``max_passes=1`` keeps the historical single-pass behaviour; pass a higher
    value (or rely on the internal scheduler) to drain a backlog in one call.
    """
    background_tasks.add_task(_run_refine, limit, max_passes)
    logger.info("refine_run_started", extra={"limit": limit, "max_passes": max_passes})
    return {"status": "running", "limit": limit, "max_passes": max_passes}
