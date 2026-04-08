import logging
import re
from urllib.parse import urlparse

from app.enrichment.base import BaseEnricher
from app.models.company import CompanyProfile, EnrichmentContext
from app.registry.enricher_registry import EnricherRegistry

logger = logging.getLogger(__name__)


@EnricherRegistry.register("domain_resolver")
class DomainResolver(BaseEnricher):
    enricher_id = "domain_resolver"
    requires = []
    optional = False

    def __init__(self, config: dict = None):
        self.config = config or {}

    async def enrich(
        self, company: CompanyProfile, ctx: EnrichmentContext
    ) -> CompanyProfile:
        if company.domain:
            return company

        # Try to extract domain from job URLs in context
        for job in ctx.jobs:
            url = job.get("url", "")
            domain = self._extract_domain_from_url(url)
            if domain:
                company.domain = domain
                logger.info(f"Resolved domain from URL: {domain}")
                return company

        # Fallback: heuristic from company name
        if company.name:
            domain = self._heuristic_domain(company.name)
            if domain:
                company.domain = domain
                logger.info(f"Resolved domain from name heuristic: {domain}")
                return company

        logger.warning(f"Could not resolve domain for company: {company.name}")
        return company

    def _extract_domain_from_url(self, url: str) -> str:
        """Extract company domain from job URL."""
        try:
            parsed = urlparse(url)
            host = parsed.hostname or ""
            # Greenhouse: boards.greenhouse.io/slug -> slug.com (approximate)
            if "greenhouse.io" in host:
                path_parts = parsed.path.strip("/").split("/")
                if path_parts:
                    return f"{path_parts[0]}.com"
            # Adzuna/Indeed: redirect URLs don't help
            if any(site in host for site in ["indeed.com", "adzuna.com"]):
                return ""
            # Direct company career page
            host = host.replace("www.", "")
            if host and "." in host:
                return host
        except Exception:
            pass
        return ""

    def _heuristic_domain(self, company_name: str) -> str:
        """Guess domain from company name."""
        name = company_name.lower().strip()
        name = re.sub(r"[^a-z0-9\s]", "", name)
        name = name.split()[0] if name.split() else name
        if name:
            return f"{name}.com"
        return ""
