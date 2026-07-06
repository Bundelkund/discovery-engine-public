import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends

from app.data_quality.context import get_dq_context
from app.dependencies import get_supabase
from app.registry.enricher_registry import EnricherRegistry
from app.registry.source_registry import SourceRegistry
from app.repositories.jobs import JobRepository
from app.repositories.raw_jobs import RawJobRepository
from app.repositories.refine_runs import RefineRunRepository
from app.repositories.scrape_runs import ScrapeRunRepository

logger = logging.getLogger(__name__)

health_router = APIRouter(tags=["health"])


@health_router.get("/health")
async def health(supabase=Depends(get_supabase)):
    # DQ state via shared singleton — stays in sync with scrape orchestrator
    try:
        dq = get_dq_context()
        data_quality = {
            "minhash_enabled": True,
            "rules_mode": dq.rules_mode,
            "geonames_loaded": dq.geonames_loaded,
        }
    except Exception as exc:
        logger.error("dq_state_build_failed", extra={"error": str(exc)})
        data_quality = {
            "minhash_enabled": False,
            "rules_mode": "unknown",
            "geonames_loaded": False,
        }

    # Coverage metrics via repository (F3)
    coverage = {
        "jobs_total": 0,
        "location_normalized_pct": 0.0,
        "dq_flags_pct": 0.0,
        "jobs_last_24h": 0,
    }
    # Refine inbox backlog — stalled-pipeline early warning (raw_jobs stuck 'new').
    refine_backlog = {"new_count": 0, "oldest_new_age_hours": 0.0}
    # Scrape scheduler visibility — latest run per source (replaces the n8n dashboard).
    last_scrape: list[dict] = []
    # Flow diagnostics (P1) — CFD inputs: WIP, throughput vs. arrivals, waste share,
    # last cycle, WIP-gate state (spec: .specs/p1-flow-diagnostics.md).
    flow_stats: dict = {}
    if supabase is not None:
        try:
            coverage = await asyncio.to_thread(
                JobRepository(supabase).get_coverage_metrics
            )
        except Exception as exc:
            logger.warning("coverage_metrics_health_failed", extra={"error": str(exc)})
        try:
            refine_backlog = await RawJobRepository(supabase).backlog_metrics()
        except Exception as exc:
            logger.warning("refine_backlog_health_failed", extra={"error": str(exc)})
        try:
            last_scrape = await ScrapeRunRepository(supabase).latest_per_source()
        except Exception as exc:
            logger.warning("last_scrape_health_failed", extra={"error": str(exc)})
        # === Flow Diagnostics (CFD + WIP) ===
        try:
            refine_runs_repo = RefineRunRepository(supabase)

            # 1. Current WIP
            wip_new_count = await refine_runs_repo.get_wip_count()
            oldest_new_age = await refine_runs_repo.get_oldest_new_age_seconds()

            # 2. Last 24h throughput (from refine_runs)
            last_24h_cycles = await refine_runs_repo.get_last_n_cycles(n=500, hours=24)
            if last_24h_cycles:
                throughput_24h = sum(
                    (c.get("stats") or {}).get("refined", 0) for c in last_24h_cycles
                )
                waste_24h = sum(
                    (c.get("stats") or {}).get("duplicate", 0)
                    + (c.get("stats") or {}).get("rejected", 0)
                    for c in last_24h_cycles
                )
                duplicate_rate_24h = (
                    waste_24h / (throughput_24h + waste_24h)
                    if (throughput_24h + waste_24h) > 0
                    else 0
                )
                newest = max(last_24h_cycles, key=lambda x: x["finished_at"])
                last_cycle = {
                    "finished_at": newest["finished_at"],
                    "refined": (newest.get("stats") or {}).get("refined", 0),
                    "duplicate": (newest.get("stats") or {}).get("duplicate", 0),
                    "rejected": (newest.get("stats") or {}).get("rejected", 0),
                }
            else:
                throughput_24h = 0
                duplicate_rate_24h = 0
                last_cycle = None

            # 3. Arrivals (from scrape_runs.stats.jobs_stored, same 24h window)
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            arrivals_res = await asyncio.to_thread(
                lambda: supabase.table("scrape_runs")
                .select("stats")
                .gte("finished_at", cutoff)
                .execute()
            )
            arrivals_24h = sum(
                (r.get("stats") or {}).get("jobs_stored", 0)
                for r in (arrivals_res.data or [])
            )

            # 4. WIP Gate config (read from env, default 30k)
            wip_soft_limit = int(os.getenv("WIP_SOFT_LIMIT", "30000"))
            gate_state = "throttled" if wip_new_count >= wip_soft_limit else "open"

            flow_stats = {
                "wip_new": wip_new_count,
                "oldest_new_age_hours": round(oldest_new_age / 3600, 1),
                "throughput_24h": throughput_24h,
                "duplicate_rate_24h": round(duplicate_rate_24h, 2),
                "arrivals_24h": arrivals_24h,
                "last_cycle": last_cycle,
                "wip_gate": {
                    "soft_limit": wip_soft_limit,
                    "state": gate_state,
                },
            }
        except Exception as exc:
            logger.warning("flow_stats_health_failed", extra={"error": str(exc)})
            flow_stats = {"error": str(exc)}

    return {
        "status": "ok",
        "sources": SourceRegistry.registered_ids(),
        "enrichers": EnricherRegistry.registered_ids(),
        "data_quality": data_quality,
        "coverage": coverage,
        "refine_backlog": refine_backlog,
        "last_scrape": last_scrape,
        "flow": flow_stats,
    }
