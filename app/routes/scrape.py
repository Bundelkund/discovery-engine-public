from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.dependencies import get_consumer, get_supabase
from app.services.scrape_orchestrator import ScrapeOrchestrator


class ScrapeRequest(BaseModel):
    profile_id: Optional[str] = None
    location: Optional[str] = None
    limit: Optional[int] = None
    store: bool = True


def make_scrape_router(source_id: str) -> APIRouter:
    router = APIRouter()

    @router.post("", dependencies=[Depends(get_consumer)])
    async def scrape(
        request: ScrapeRequest, supabase=Depends(get_supabase)
    ):
        orchestrator = ScrapeOrchestrator(supabase)
        result = await orchestrator.run(
            source_id=source_id,
            profile_id=request.profile_id,
            location=request.location,
            limit=request.limit,
            store=request.store,
        )
        return result

    return router
