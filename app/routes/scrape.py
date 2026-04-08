from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.dependencies import get_supabase, require_api_key
from app.services.scrape_orchestrator import ScrapeOrchestrator


class ScrapeRequest(BaseModel):
    profile_id: str
    location: Optional[str] = None
    limit: Optional[int] = None
    store: bool = True


def make_scrape_router(source_id: str) -> APIRouter:
    router = APIRouter()

    @router.post("", dependencies=[Depends(require_api_key)])
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
