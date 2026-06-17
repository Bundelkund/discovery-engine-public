import asyncio
import logging

from fastapi import APIRouter, Depends

from app.data_quality.context import get_dq_context
from app.dependencies import get_supabase
from app.registry.enricher_registry import EnricherRegistry
from app.registry.source_registry import SourceRegistry
from app.repositories.jobs import JobRepository
from app.repositories.raw_jobs import RawJobRepository
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

    return {
        "status": "ok",
        "sources": SourceRegistry.registered_ids(),
        "enrichers": EnricherRegistry.registered_ids(),
        "data_quality": data_quality,
        "coverage": coverage,
        "refine_backlog": refine_backlog,
        "last_scrape": last_scrape,
    }
