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

# Single-flight guard: refine has no atomic row-claim (fetch_new is a plain
# SELECT status='new'). If an n8n cron fires /refine while a prior pass is still
# draining the same batch, both would process the same rows — the second pass
# would mark a just-refined job 'duplicate' against the first pass's freshly
# added MinHash bands, and double the enrichment spend. This module-level flag
# serialises passes WITHIN one process: there is no `await` between the check and
# the set, so it is atomic w.r.t. the asyncio event loop.
# CAVEAT: only covers a single uvicorn process. With multiple workers, promote to
# a Postgres advisory lock (pg_try_advisory_lock) or an atomic SELECT ... FOR
# UPDATE SKIP LOCKED claim on raw_jobs.
_refine_running = False


async def _run_refine(limit: int) -> None:
    """Background worker: fresh Supabase client, one refine pass (single-flight)."""
    global _refine_running
    if _refine_running:
        logger.info("refine_skip_already_running", extra={"limit": limit})
        return
    _refine_running = True

    from app.dependencies import get_supabase as _gs

    supabase = _gs()
    try:
        summary = await RefinePipeline(supabase).run(limit=limit)
        logger.info("refine_run_done", extra=summary)
    except Exception as exc:  # noqa: BLE001
        logger.error("refine_run_failed", extra={"error": str(exc)})
    finally:
        _refine_running = False


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
