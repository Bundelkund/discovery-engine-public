from fastapi import APIRouter, Depends

from app.dependencies import get_supabase, require_scope
from app.enrichment.service import enrich_domain

enrich_router = APIRouter(tags=["enrichment"])


@enrich_router.post(
    "/enrich/{domain}",
    dependencies=[Depends(require_scope("scrape:trigger"))],
)
async def enrich_company(domain: str, supabase=Depends(get_supabase)):
    result = await enrich_domain(supabase, domain)
    if result:
        return result
    return {"domain": domain, "status": "no_enrichment"}
