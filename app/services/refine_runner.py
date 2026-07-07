"""Autonomous refine runner — drives raw_jobs → jobs_v2 from INSIDE the engine.

Historically the refine step (raw_jobs(status='new') → RefinePipeline → jobs_v2)
was triggered only by an external n8n cron hitting ``POST /refine``. When that
cron drifted out of config the whole pipeline silently stalled: scrapes kept
filling raw_jobs while jobs_v2 went stale. The engine owns dedup + the clean
shelf, so it must own the trigger too — that is what this module provides.

Two entry points:

  * ``drain()``          — one drain cycle (loops passes until the inbox is empty
                           or a cap is hit). Used by the internal scheduler AND
                           the manual ``POST /refine`` endpoint.
  * ``scheduler_loop()`` — periodic ``drain()`` on an asyncio interval, started
                           from the FastAPI lifespan.

Concurrency (AUDIT-P1-04): the old in-process ``_refine_running`` single-flight
bool is GONE. It only covered one process — ``--workers 2`` or a second replica
would double-process the same batch (the second marking a just-refined job
'duplicate' against the first's freshly-added MinHash bands, doubling resolver
spend). Correctness now lives in the DB: ``fetch_new`` claims its batch through
the ``claim_refine_batch`` RPC (``SELECT … FOR UPDATE SKIP LOCKED`` + ``UPDATE
status='refining'`` in one transaction), so ANY number of concurrent drains —
across workers, replicas, or the scheduler racing ``POST /refine`` — receive
disjoint batches. Crash recovery: rows stuck 'refining' (a drain died mid-pass)
are released back to 'new' by the time-based reclaim at every drain start.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.repositories.raw_jobs import RawJobRepository
from app.repositories.refine_runs import RefineRunRepository
from app.services.refine_pipeline import RefinePipeline

logger = logging.getLogger(__name__)

# A 'refining' claim older than this is presumed orphaned by a crashed drain and
# is released back to 'new' at the next drain start. Generously above one pass's
# worst case (200 rows x HTTP description-resolution) so a slow-but-alive pass
# never has its claim stolen by a sibling worker.
# debt: hardcoded window; make a setting when a deploy needs passes >30 min
# (upgrade-trigger: refine_batch_limit raised past ~1000).
_CLAIM_STALE_SECONDS = 1800


async def drain(limit: int = 200, max_passes: int = 100) -> dict:
    """Drain raw_jobs(status='new') through the refine pipeline until empty.

    Loops ``RefinePipeline.run(limit)`` until a pass fetches fewer than ``limit``
    rows (inbox drained) or ``max_passes`` is reached (safety cap). Safe to run
    concurrently (scheduler vs POST /refine, multiple workers/replicas): each
    pass CLAIMS its batch atomically in the DB, so parallel drains work disjoint
    rows instead of double-processing.

    Returns aggregate terminal-state counts across all passes this cycle.
    """
    # Fresh client per cycle (mirrors the old endpoint worker); created lazily so
    # importing this module never touches Supabase.
    from app.dependencies import get_supabase

    # Crash recovery FIRST: release claims orphaned by a previous drain that died
    # mid-pass (redeploy/crash), so those rows re-enter this cycle's inbox instead
    # of rotting invisible in 'refining'. Best-effort — a reclaim failure must
    # never block the drain. Mirrors scrape_runner's stale-reclaim pattern.
    try:
        reclaimed = await RawJobRepository(get_supabase()).reclaim_stale_refining(
            _CLAIM_STALE_SECONDS
        )
        if reclaimed:
            logger.warning("refine_reclaimed_stale_refining", extra={"count": reclaimed})
    except Exception as exc:  # noqa: BLE001
        logger.warning("refine_reclaim_failed", extra={"error": str(exc)})

    totals = {
        "fetched": 0, "refined": 0, "rejected": 0, "duplicate": 0,
        "errors": 0, "passes": 0,
    }

    # Flow telemetry (P1 flow diagnostics): snapshot WIP before the cycle, write
    # one refine_runs row after it. Best-effort throughout — metrics are
    # observability, not critical path, so a telemetry failure must never block
    # or fail the drain (spec: .specs/p1-flow-diagnostics.md).
    started_at = datetime.now(timezone.utc)
    refine_runs_repo: RefineRunRepository | None = None
    wip_before = 0
    oldest_new_age = 0
    try:
        refine_runs_repo = RefineRunRepository(get_supabase())
        wip_before = await refine_runs_repo.get_wip_count()
        oldest_new_age = await refine_runs_repo.get_oldest_new_age_seconds()
    except Exception as exc:  # noqa: BLE001
        logger.warning("refine_run_pre_stats_failed", extra={"error": str(exc)})

    try:
        pipeline = RefinePipeline(get_supabase())
        for _ in range(max_passes):
            summary = await pipeline.run(limit=limit)
            totals["passes"] += 1
            for k in ("fetched", "refined", "rejected", "duplicate", "errors"):
                totals[k] += summary.get(k, 0)
            # Short batch ⇒ inbox drained ⇒ stop this cycle.
            if summary.get("fetched", 0) < limit:
                break
    except Exception as exc:  # noqa: BLE001
        logger.error("refine_drain_failed", extra={"error": str(exc)})

    # Persist this cycle's telemetry (best-effort: log + continue on failure).
    if refine_runs_repo is not None:
        try:
            finished_at = datetime.now(timezone.utc)
            wip_after = await refine_runs_repo.get_wip_count()
            stats = {
                "fetched": totals["fetched"],
                "refined": totals["refined"],
                "rejected": totals["rejected"],
                "duplicate": totals["duplicate"],
                "errors": totals.get("errors", 0),
                "passes": totals.get("passes", 0),
                "wip_before": wip_before,
                "wip_after": wip_after,
                "oldest_new_age_seconds": oldest_new_age,
            }
            await refine_runs_repo.insert(
                started_at=started_at,
                finished_at=finished_at,
                stats=stats,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("refine_run_record_failed", extra={"error": str(exc)})
            # Don't block drain; metric is observability, not critical path.

    logger.info("refine_drain_complete", extra=totals)
    return totals


async def scheduler_loop(
    stop: asyncio.Event,
    interval_seconds: int = 300,
    limit: int = 200,
    max_passes: int = 100,
) -> None:
    """Periodic drain loop, owned by the FastAPI lifespan.

    Drains immediately on startup (clears any overnight backlog), then every
    ``interval_seconds`` until ``stop`` is set. A drain failure is logged and the
    loop continues — one bad cycle must never kill the scheduler.
    """
    logger.info(
        "refine_scheduler_started",
        extra={"interval_seconds": interval_seconds, "limit": limit, "max_passes": max_passes},
    )
    while not stop.is_set():
        try:
            await drain(limit=limit, max_passes=max_passes)
        except Exception as exc:  # noqa: BLE001
            logger.error("refine_scheduler_cycle_failed", extra={"error": str(exc)})
        # Interruptible sleep: wake early when shutdown sets the event.
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass
    logger.info("refine_scheduler_stopped")
