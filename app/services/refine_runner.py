"""Autonomous refine runner — drives raw_jobs → jobs_v2 from INSIDE the engine.

Historically the refine step (raw_jobs(status='new') → RefinePipeline → jobs_v2)
was triggered only by an external n8n cron hitting ``POST /refine``. When that
cron drifted out of config the whole pipeline silently stalled: scrapes kept
filling raw_jobs while jobs_v2 went stale. The engine owns dedup + the clean
shelf, so it must own the trigger too — that is what this module provides.

Two entry points share ONE single-flight guard (``_refine_running``):

  * ``drain()``          — one drain cycle (loops passes until the inbox is empty
                           or a cap is hit). Used by the internal scheduler AND
                           the manual ``POST /refine`` endpoint.
  * ``scheduler_loop()`` — periodic ``drain()`` on an asyncio interval, started
                           from the FastAPI lifespan.

Single-flight matters: refine has no atomic row-claim (``fetch_new`` is a plain
SELECT status='new'), so two concurrent drains would double-process the same
batch — the second marking a just-refined job 'duplicate' against the first's
freshly-added MinHash bands, and doubling resolver spend. The guard is set/read
with NO ``await`` in between, so it is atomic w.r.t. the asyncio event loop.
CAVEAT: it only covers a single process. With multiple uvicorn workers, promote
to a Postgres advisory lock (pg_try_advisory_lock) or SELECT ... FOR UPDATE SKIP
LOCKED on raw_jobs. Single-container Coolify deploy → one worker → this is enough.
"""
from __future__ import annotations

import asyncio
import logging

from app.services.refine_pipeline import RefinePipeline

logger = logging.getLogger(__name__)

# Module-level single-flight guard shared by drain() (scheduler + endpoint).
_refine_running = False


def is_running() -> bool:
    """True while a drain cycle is in flight (for tests / introspection)."""
    return _refine_running


async def drain(limit: int = 200, max_passes: int = 100) -> dict:
    """Drain raw_jobs(status='new') through the refine pipeline until empty.

    Loops ``RefinePipeline.run(limit)`` until a pass fetches fewer than ``limit``
    rows (inbox drained) or ``max_passes`` is reached (safety cap). Single-flight:
    a no-op returning ``{"skipped": True}`` if another drain is already running.

    Returns aggregate terminal-state counts across all passes this cycle.
    """
    global _refine_running
    if _refine_running:
        logger.info("refine_skip_already_running", extra={"limit": limit})
        return {"skipped": True}
    _refine_running = True

    # Fresh client per cycle (mirrors the old endpoint worker); created lazily so
    # importing this module never touches Supabase.
    from app.dependencies import get_supabase

    totals = {
        "fetched": 0, "refined": 0, "rejected": 0, "duplicate": 0,
        "errors": 0, "passes": 0,
    }
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
    finally:
        _refine_running = False

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
