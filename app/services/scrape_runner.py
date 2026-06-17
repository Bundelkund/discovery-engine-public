"""Autonomous scrape runner — triggers scrapes from INSIDE the engine.

Historically every source was scraped only by an external n8n cron hitting
``POST /scrape/{source}``. That is the same class of dependency that once froze
the refine step (a drifted n8n cron stalled the whole pipeline). The engine owns
the inbox (``raw_jobs``) and the clean shelf, so it should own the trigger too.

Difference from refine_runner: refine is cheap + local (DB only), so it drains on
every startup. Scraping hits EXTERNAL, partly PAID APIs (adzuna/jooble/indeed), so
re-scraping on every container redeploy would burn API quota. The cadence gate is
therefore PERSISTENT: ``scrape_runs`` records each source's last successful run, and
``run_due`` scrapes a source only when its last success is older than
``min_interval_hours`` (default 24h). A redeploy within the day re-reads that state
and skips — no external calls. A missed day self-heals on the next tick.

Single-flight guard (``_scrape_running``) prevents a slow cycle from overlapping the
next tick. Set/read with NO ``await`` in between → atomic w.r.t. the asyncio loop.
CAVEAT (mirrors refine_runner): single-process only. Multi-worker → promote to a
Postgres advisory lock. Single-container Coolify deploy → one worker → enough.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.config import load_sources_config
from app.registry.source_registry import SourceRegistry
from app.repositories.scrape_runs import ScrapeRunRepository
from app.services.scrape_orchestrator import ScrapeOrchestrator

logger = logging.getLogger(__name__)

# Module-level single-flight guard shared across scheduler ticks.
_scrape_running = False

# Small spacing between sequential source scrapes so a cycle does not fire all
# sources at the same instant (rate limits / thundering herd on external APIs).
_INTER_SOURCE_PAUSE_S = 2


def is_running() -> bool:
    """True while a scrape cycle is in flight (for tests / introspection)."""
    return _scrape_running


def _enabled_sources() -> list[str]:
    """Sources that are both registered AND enabled in sources.yaml."""
    cfg = load_sources_config().get("sources", {})
    registered = set(SourceRegistry.registered_ids())
    return [
        sid for sid in registered
        if cfg.get(sid, {}).get("enabled", True)
    ]


async def run_due(min_interval_hours: int = 24, source_timeout_seconds: int = 1800) -> dict:
    """Scrape every enabled source whose last success is older than the window.

    For each due source: record a 'running' row → ScrapeOrchestrator.run → record
    'done' (with stats) or, on error/timeout, 'failed'. ONLY a 'done' run resets the
    cadence clock, so a failed source retries next tick. Per-source isolation: one
    source raising never stops the others. Each source is capped at
    ``source_timeout_seconds`` so a hung source can't wedge the whole sequential
    cycle (and hold the single-flight guard). Single-flight: a no-op returning
    ``{"skipped": True}`` if another cycle is already running.

    Returns ``{checked, scraped, skipped, failed, sources: [...]}``.
    """
    global _scrape_running
    if _scrape_running:
        logger.info("scrape_skip_already_running")
        return {"skipped": True}
    _scrape_running = True

    # Lazy import so importing this module never touches Supabase.
    from app.dependencies import get_supabase

    totals = {"checked": 0, "scraped": 0, "skipped": 0, "failed": 0, "sources": []}
    try:
        client = get_supabase()
        runs = ScrapeRunRepository(client)
        now = datetime.now(timezone.utc)
        cutoff_seconds = min_interval_hours * 3600

        for source_id in _enabled_sources():
            totals["checked"] += 1
            last = await runs.last_success_at(source_id)
            if last is not None and (now - last).total_seconds() < cutoff_seconds:
                totals["skipped"] += 1
                continue

            run_id = await runs.record_start(source_id)
            try:
                resp = await asyncio.wait_for(
                    ScrapeOrchestrator(client).run(source_id=source_id),
                    timeout=source_timeout_seconds,
                )
                stats = {
                    "jobs_found": resp.jobs_found,
                    "jobs_stored": resp.jobs_stored,
                    "duration_ms": resp.duration_ms,
                }
                if run_id:
                    await runs.record_finish(run_id, "done", stats=stats)
                totals["scraped"] += 1
                totals["sources"].append({"source": source_id, **stats})
                logger.info("scrape_run_done", extra={"source": source_id, **stats})
            except asyncio.TimeoutError:
                if run_id:
                    await runs.record_finish(
                        run_id, "failed", error=f"timeout after {source_timeout_seconds}s"
                    )
                totals["failed"] += 1
                logger.error("scrape_run_timeout", extra={"source": source_id, "timeout_s": source_timeout_seconds})
            except Exception as exc:  # noqa: BLE001
                if run_id:
                    await runs.record_finish(run_id, "failed", error=str(exc))
                totals["failed"] += 1
                logger.error("scrape_run_failed", extra={"source": source_id, "error": str(exc)})

            await asyncio.sleep(_INTER_SOURCE_PAUSE_S)
    except Exception as exc:  # noqa: BLE001
        logger.error("scrape_run_due_failed", extra={"error": str(exc)})
    finally:
        _scrape_running = False

    logger.info("scrape_cycle_complete", extra={k: totals[k] for k in ("checked", "scraped", "skipped", "failed")})
    return totals


async def scheduler_loop(
    stop: asyncio.Event,
    check_interval_seconds: int = 3600,
    min_interval_hours: int = 24,
    source_timeout_seconds: int = 1800,
) -> None:
    """Periodic cadence check, owned by the FastAPI lifespan.

    Wakes every ``check_interval_seconds`` and scrapes any source due per the
    persistent ``scrape_runs`` gate. Runs a first check shortly after startup (a
    fresh DB has no prior runs ⇒ all sources due). A cycle failure is logged and the
    loop continues — one bad cycle must never kill the scheduler.

    On startup it first reclaims any 'running' rows orphaned by a previous process
    that died mid-scrape (redeploy), so they don't linger as zombies on /health.
    """
    logger.info(
        "scrape_scheduler_started",
        extra={"check_interval_seconds": check_interval_seconds, "min_interval_hours": min_interval_hours},
    )
    try:
        from app.dependencies import get_supabase
        from app.repositories.scrape_runs import ScrapeRunRepository

        reclaimed = await ScrapeRunRepository(get_supabase()).reclaim_stale_running()
        if reclaimed:
            logger.info("scrape_reclaimed_stale_running", extra={"count": reclaimed})
    except Exception as exc:  # noqa: BLE001
        logger.warning("scrape_reclaim_failed", extra={"error": str(exc)})

    while not stop.is_set():
        try:
            await run_due(
                min_interval_hours=min_interval_hours,
                source_timeout_seconds=source_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("scrape_scheduler_cycle_failed", extra={"error": str(exc)})
        # Interruptible sleep: wake early when shutdown sets the event.
        try:
            await asyncio.wait_for(stop.wait(), timeout=check_interval_seconds)
        except asyncio.TimeoutError:
            pass
    logger.info("scrape_scheduler_stopped")
