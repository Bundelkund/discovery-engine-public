import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

# Import the plugin packages to trigger their self-registration side effects.
# Each package's __init__.py imports every concrete plugin module, which in turn
# applies the @SourceRegistry.register / @ScorerRegistry.register / @EnricherRegistry.register
# decorators at import time.
import app.sources  # noqa: F401
import app.scoring  # noqa: F401
import app.enrichment  # noqa: F401

from app.registry.source_registry import SourceRegistry
from app.routes.health import health_router
from app.routes.scrape import make_scrape_router
from app.routes.enrich import enrich_router
from app.routes.jobs_api import jobs_api_router
from app.routes.companies_api import companies_api_router
from app.routes.scan import scan_router
from app.routes.refine import refine_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Auto-register scrape routes for each registered source
    for source_id in SourceRegistry.registered_ids():
        router = make_scrape_router(source_id)
        app.include_router(
            router, prefix=f"/scrape/{source_id}", tags=[source_id]
        )
    yield


app = FastAPI(title="Discovery Engine", version="0.1.0", lifespan=lifespan)
app.include_router(health_router)
app.include_router(enrich_router)
app.include_router(jobs_api_router)
app.include_router(companies_api_router)
app.include_router(scan_router)
app.include_router(refine_router)
