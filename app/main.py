import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

# Import the plugin packages to trigger their self-registration side effects.
# Each package's __init__.py imports every concrete plugin module, which in turn
# applies the @SourceRegistry.register / @EnricherRegistry.register decorators at
# import time. (Scoring was removed 2026-06-09 — the engine is profile-agnostic.)
import app.sources  # noqa: F401
import app.enrichment  # noqa: F401

from app.config import get_settings
from app.registry.source_registry import SourceRegistry
from app.routes.health import health_router
from app.routes.scrape import make_scrape_router
from app.routes.enrich import enrich_router
from app.routes.jobs_api import jobs_api_router
from app.routes.companies_api import companies_api_router
from app.routes.scan import scan_router
from app.routes.refine import refine_router
from app.services.refine_runner import scheduler_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Auto-register scrape routes for each registered source
    for source_id in SourceRegistry.registered_ids():
        router = make_scrape_router(source_id)
        app.include_router(
            router, prefix=f"/scrape/{source_id}", tags=[source_id]
        )

    # Autonomous refine: drain raw_jobs → jobs_v2 on an internal loop so the
    # pipeline no longer depends on an external n8n cron. Single-flight guard in
    # refine_runner keeps this and the manual /refine endpoint mutually exclusive.
    settings = get_settings()
    refine_stop = asyncio.Event()
    refine_task: asyncio.Task | None = None
    if settings.refine_auto_enabled:
        refine_task = asyncio.create_task(
            scheduler_loop(
                refine_stop,
                interval_seconds=settings.refine_interval_seconds,
                limit=settings.refine_batch_limit,
                max_passes=settings.refine_max_passes,
            )
        )
    else:
        logger.info("refine_scheduler_disabled")

    try:
        yield
    finally:
        if refine_task is not None:
            refine_stop.set()          # let the loop exit at its next checkpoint
            refine_task.cancel()       # interrupt an in-flight sleep/drain on shutdown
            try:
                await refine_task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="Discovery Engine", version="0.1.0", lifespan=lifespan)
app.include_router(health_router)
app.include_router(enrich_router)
app.include_router(jobs_api_router)
app.include_router(companies_api_router)
app.include_router(scan_router)
app.include_router(refine_router)
