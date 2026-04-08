import logging
from datetime import datetime, timezone

from app.models.company import CompanyProfile
from app.repositories.base import BaseRepository

logger = logging.getLogger(__name__)


class CompanyRepository(BaseRepository):
    TABLE = "company_profiles"

    async def upsert(self, company: CompanyProfile) -> dict:
        data = {
            "domain": company.domain,
            "name": company.name,
            "size": company.size,
            "industry": company.industry,
            "location": company.location,
            "linkedin": company.linkedin,
            "hunter_data": company.hunter_data,
            "cvf_scores": company.cvf_scores,
            "hiring_signal_count": company.hiring_signal_count,
            "enriched_at": datetime.now(timezone.utc).isoformat(),
        }
        result = (
            self.client.table(self.TABLE)
            .upsert(data, on_conflict="domain")
            .execute()
        )
        return result.data[0] if result.data else {}

    async def get(self, domain: str) -> dict | None:
        result = (
            self.client.table(self.TABLE)
            .select("*")
            .eq("domain", domain)
            .execute()
        )
        return result.data[0] if result.data else None

    async def needs_enrichment(
        self, domain: str, max_age_days: int = 30
    ) -> bool:
        existing = await self.get(domain)
        if not existing:
            return True
        enriched_at = existing.get("enriched_at")
        if not enriched_at:
            return True
        return True  # Simplified: always re-enrich for now
