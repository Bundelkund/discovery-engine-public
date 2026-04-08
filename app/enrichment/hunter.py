import asyncio
import logging

import httpx

from app.config import get_settings
from app.enrichment.base import BaseEnricher
from app.models.company import CompanyProfile, EnrichmentContext
from app.registry.enricher_registry import EnricherRegistry

logger = logging.getLogger(__name__)


@EnricherRegistry.register("hunter")
class HunterEnricher(BaseEnricher):
    enricher_id = "hunter"
    requires = ["domain_resolver"]
    optional = True

    BASE_URL = "https://api.hunter.io/v2"

    def __init__(self, config: dict = None):
        self.config = config or {}
        rate_limit = self.config.get("rate_limit", {})
        self.requests_per_second = rate_limit.get("requests_per_second", 2)
        self.daily_limit = rate_limit.get("daily_limit", 500)
        self._request_count = 0

    async def enrich(
        self, company: CompanyProfile, ctx: EnrichmentContext
    ) -> CompanyProfile:
        if not company.domain:
            logger.warning("Hunter: no domain to enrich")
            return company

        if self._request_count >= self.daily_limit:
            logger.warning("Hunter: daily limit reached")
            return company

        settings = get_settings()
        api_key = settings.hunter_api_key
        if not api_key:
            logger.warning("Hunter: no API key configured")
            return company

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Rate limiting
                if self.requests_per_second > 0:
                    await asyncio.sleep(1.0 / self.requests_per_second)

                resp = await client.get(
                    f"{self.BASE_URL}/companies/find",
                    params={"domain": company.domain, "api_key": api_key},
                )
                self._request_count += 1

                if resp.status_code == 404:
                    logger.info(f"Hunter: company not found for {company.domain}")
                    return company

                if resp.status_code == 429:
                    logger.warning("Hunter: rate limit exceeded")
                    return company

                resp.raise_for_status()
                data = resp.json().get("data", {})

                # Map Hunter data to CompanyProfile
                company.name = data.get("name", company.name)
                metrics = data.get("metrics", {})
                company.size = (
                    str(metrics.get("employees", ""))
                    if metrics.get("employees")
                    else company.size
                )
                company.industry = (
                    ", ".join(data.get("tags", []))
                    if data.get("tags")
                    else company.industry
                )
                company.location = data.get("location", company.location)
                linkedin = data.get("linkedin", {})
                if linkedin and linkedin.get("handle"):
                    company.linkedin = f"https://linkedin.com/{linkedin['handle']}"
                company.hunter_data = {
                    "domain": data.get("domain"),
                    "description": data.get("description"),
                    "founded": data.get("foundedYear"),
                    "company_type": data.get("companyType"),
                    "tech_stack": data.get("tech", []),
                    "country": data.get("geo", {}).get("country"),
                }

                logger.info(f"Hunter: enriched {company.domain} -> {company.name}")
                return company

        except Exception as e:
            logger.error(f"Hunter enrichment failed for {company.domain}: {e}")
            raise
