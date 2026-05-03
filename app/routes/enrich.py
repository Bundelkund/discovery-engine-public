from fastapi import APIRouter, Depends

from app.config import load_enrichment_config
from app.dependencies import get_supabase, require_scope
from app.enrichment.pipeline import EnrichmentPipeline
from app.models.company import CompanyProfile, EnrichmentContext
from app.repositories.companies import CompanyRepository

enrich_router = APIRouter(tags=["enrichment"])


@enrich_router.post(
    "/enrich/{domain}",
    dependencies=[Depends(require_scope("scrape:trigger"))],
)
async def enrich_company(domain: str, supabase=Depends(get_supabase)):
    company_repo = CompanyRepository(supabase)

    existing = await company_repo.get(domain)
    company = CompanyProfile(domain=domain, **(existing or {}))

    enrichment_config = load_enrichment_config().get("enrichment", {})
    pipeline = EnrichmentPipeline(enrichment_config)

    ctx = EnrichmentContext(profile_id="", source="manual")
    results = await pipeline.run([company], ctx)

    if results:
        await company_repo.upsert(results[0])
        return results[0].model_dump()

    return {"domain": domain, "status": "no_enrichment"}
