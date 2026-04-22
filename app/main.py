import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

# Import all adapters/scorers/enrichers to trigger self-registration
from app.sources import *  # noqa: F401, F403
from app.scoring import *  # noqa: F401, F403
from app.enrichment import *  # noqa: F401, F403

from app.registry.source_registry import SourceRegistry
from app.routes.health import health_router
from app.routes.scrape import make_scrape_router
from app.routes.enrich import enrich_router
from app.routes.jobs_api import jobs_api_router
from app.routes.companies_api import companies_api_router

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
