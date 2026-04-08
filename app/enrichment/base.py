from abc import ABC, abstractmethod

from app.models.company import CompanyProfile, EnrichmentContext


class BaseEnricher(ABC):
    enricher_id: str
    requires: list[str] = []
    optional: bool = True

    @abstractmethod
    async def enrich(
        self, company: CompanyProfile, ctx: EnrichmentContext
    ) -> CompanyProfile:
        """Enrich a company profile."""
