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

Concurrency (AUDIT-P1-04): the old in-process ``_scrape_running`` single-flight
bool is GONE (single-process only — useless against ``--workers 2`` / a second
replica). The claim now lives in the DB, per source: ``record_start``'s INSERT of
the 'running' scrape_runs row is guarded by a partial unique index (one 'running'
row per source, migrations/atomic-refine-claim.sql), so a concurrent second start
of the same source loses the insert race (23505 → ``None`` → skipped). Stale
'running' rows from a crashed process are reclaimed TIME-BASED at each cycle
start: every live scrape is hard-capped by ``asyncio.wait_for``, so a row older
than the timeout + margin is provably a crash orphan — unlike the old
reclaim-all-at-startup, this never steals a sibling worker's in-flight run.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.config import load_sources_config
from app.registry.source_registry import SourceRegistry
from app.repositories.scrape_runs import ScrapeRunRepository
from app.services.scrape_orchestrator import ScrapeOrchestrator

logger = logging.getLogger(__name__)

# Small spacing between sequential source scrapes so a cycle does not fire all
# sources at the same instant (rate limits / thundering herd on external APIs).
_INTER_SOURCE_PAUSE_S = 2

# Extra slack on top of source_timeout_seconds before a 'running' scrape_runs row
# counts as a crash orphan (covers the record_finish write + clock skew).
_RECLAIM_MARGIN_S = 120


def _enabled_sources() -> list[str]:
    """Sources that are both registered AND enabled in sources.yaml."""
    cfg = load_sources_config().get("sources", {})
    registered = set(SourceRegistry.registered_ids())
    return [
        sid for sid in registered
        if cfg.get(sid, {}).get("enabled", True)
    ]


async def run_due(
    min_interval_hours: int = 24,
    source_timeout_seconds: int = 1800,
    daily_anchor_hour: int | None = None,
) -> dict:
    """Scrape every enabled source whose last success is older than the window.

    Two cadence gates (``daily_anchor_hour`` selects which):
      * anchored (default in prod) — a source is due once per day, after the most
        recent ``daily_anchor_hour``:00 UTC instant. This does NOT drift: the gate
        is pinned to a wall-clock hour, so completion time no longer creeps later
        each day past downstream consumers (e.g. the Telegram digest).
      * interval (``daily_anchor_hour=None``) — due when last success is older than
        ``min_interval_hours``. Kept for tests / opt-out.
    Both share the same redeploy/quota safety: a same-day redeploy re-reads the
    persistent ``scrape_runs`` state and skips already-done sources.

    For each due source: CLAIM it by inserting its 'running' row (record_start;
    the one-running-per-source unique index makes this the cross-process
    single-flight gate — a lost claim means another worker/replica is scraping
    that source right now, so it is skipped) → ScrapeOrchestrator.run → record
    'done' (with stats) or, on error/timeout, 'failed'. ONLY a 'done' run resets
    the cadence clock, so a failed source retries next tick. Per-source isolation:
    one source raising never stops the others. Each source is capped at
    ``source_timeout_seconds`` so a hung source can't wedge the whole sequential
    cycle (or leave an eternally-blocking claim).

    Returns ``{checked, scraped, skipped, failed, sources: [...]}``.
    """
    # Lazy import so importing this module never touches Supabase.
    from app.dependencies import get_supabase

    totals = {"checked": 0, "scraped": 0, "skipped": 0, "failed": 0, "sources": []}
    try:
        client = get_supabase()
        runs = ScrapeRunRepository(client)
        now = datetime.now(timezone.utc)
        cutoff_seconds = min_interval_hours * 3600

        # Crash recovery FIRST: a 'running' row older than the per-source timeout
        # (+ margin) is provably orphaned (live scrapes are wait_for-capped) and
        # would otherwise block its source forever via the unique claim index.
        # Time-based → never steals a younger, legitimately in-flight claim.
        # Best-effort: a reclaim failure must not stop the cycle.
        try:
            stale_before = now - timedelta(
                seconds=source_timeout_seconds + _RECLAIM_MARGIN_S
            )
            reclaimed = await runs.reclaim_stale_running(stale_before)
            if reclaimed:
                logger.warning("scrape_reclaimed_stale_running", extra={"count": reclaimed})
        except Exception as exc:  # noqa: BLE001
            logger.warning("scrape_reclaim_failed", extra={"error": str(exc)})

        # Anchored gate: most recent <daily_anchor_hour>:00 UTC at or before now.
        anchor: datetime | None = None
        if daily_anchor_hour is not None:
            anchor = now.replace(hour=daily_anchor_hour, minute=0, second=0, microsecond=0)
            if anchor > now:
                anchor -= timedelta(days=1)

        for source_id in _enabled_sources():
            totals["checked"] += 1
            last = await runs.last_success_at(source_id)
            if anchor is not None:
                # Due once per day: skip only if last success already passed today's anchor.
                if last is not None and last >= anchor:
                    totals["skipped"] += 1
                    continue
            elif last is not None and (now - last).total_seconds() < cutoff_seconds:
                totals["skipped"] += 1
                continue

            run_id = await runs.record_start(source_id)
            if run_id is None:
                # Claim lost: another worker/replica holds this source's 'running'
                # row (or the audit insert failed) — skip; the loser retries next
                # tick if the winner doesn't finish 'done'.
                totals["skipped"] += 1
                logger.info("scrape_claim_lost", extra={"source": source_id})
                continue
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

    logger.info("scrape_cycle_complete", extra={k: totals[k] for k in ("checked", "scraped", "skipped", "failed")})
    return totals


async def scheduler_loop(
    stop: asyncio.Event,
    check_interval_seconds: int = 3600,
    min_interval_hours: int = 24,
    source_timeout_seconds: int = 1800,
    daily_anchor_hour: int | None = None,
) -> None:
    """Periodic cadence check, owned by the FastAPI lifespan.

    Wakes every ``check_interval_seconds`` and scrapes any source due per the
    persistent ``scrape_runs`` gate. Runs a first check shortly after startup (a
    fresh DB has no prior runs ⇒ all sources due). A cycle failure is logged and the
    loop continues — one bad cycle must never kill the scheduler.

    Crash-orphan reclaim moved INTO run_due (time-based, every cycle): the old
    startup reclaim-all was only safe with exactly one process — a second
    worker/replica starting up would have marked a sibling's legitimately
    in-flight 'running' rows as failed (AUDIT-P1-04).
    """
    logger.info(
        "scrape_scheduler_started",
        extra={
            "check_interval_seconds": check_interval_seconds,
            "min_interval_hours": min_interval_hours,
            "daily_anchor_hour": daily_anchor_hour,
        },
    )
    while not stop.is_set():
        try:
            await run_due(
                min_interval_hours=min_interval_hours,
                source_timeout_seconds=source_timeout_seconds,
                daily_anchor_hour=daily_anchor_hour,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("scrape_scheduler_cycle_failed", extra={"error": str(exc)})
        # Interruptible sleep: wake early when shutdown sets the event.
        try:
            await asyncio.wait_for(stop.wait(), timeout=check_interval_seconds)
        except asyncio.TimeoutError:
            pass
    logger.info("scrape_scheduler_stopped")
