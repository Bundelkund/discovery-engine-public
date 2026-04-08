from fastapi import APIRouter

from app.registry.source_registry import SourceRegistry
from app.registry.scorer_registry import ScorerRegistry
from app.registry.enricher_registry import EnricherRegistry

health_router = APIRouter(tags=["health"])


@health_router.get("/health")
async def health():
    return {
        "status": "ok",
        "sources": SourceRegistry.registered_ids(),
        "scorers": ScorerRegistry.registered_ids(),
        "enrichers": EnricherRegistry.registered_ids(),
    }
