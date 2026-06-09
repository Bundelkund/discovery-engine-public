"""Shared company-enrichment entrypoint.

Single place that runs the EnrichmentPipeline for one domain and persists the
result. Used by BOTH the explicit ``POST /enrich/{domain}`` route and the
enrich-on-read path of ``GET /companies/{domain}`` — so enrichment runs at
read/apply time (when a consumer actually needs the company) instead of eagerly
for every scraped company in the refine pipeline.
"""
import logging

from app.config import load_enrichment_config
from app.enrichment.pipeline import EnrichmentPipeline
from app.models.company import CompanyProfile, EnrichmentContext
from app.repositories.companies import CompanyRepository

logger = logging.getLogger(__name__)


async def enrich_domain(supabase, domain: str) -> dict | None:
    """Enrich one company domain and upsert the result. Returns the enriched
    company dict, or None if the pipeline produced nothing.

    Best-effort at the call site: callers that must not fail (enrich-on-read)
    should wrap this in try/except and fall back to whatever they had.
    """
    repo = CompanyRepository(supabase)
    existing = await repo.get(domain)
    company = CompanyProfile(domain=domain, **(existing or {}))

    cfg = load_enrichment_config().get("enrichment", {})
    pipeline = EnrichmentPipeline(cfg)
    results = await pipeline.run([company], EnrichmentContext(source="on_read"))

    if results:
        await repo.upsert(results[0])
        return results[0].model_dump()
    return None
