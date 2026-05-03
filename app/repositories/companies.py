import asyncio
import logging
from datetime import datetime, timedelta, timezone

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
        result = await asyncio.to_thread(
            lambda: self.client.table(self.TABLE)
            .upsert(data, on_conflict="domain")
            .execute()
        )
        return result.data[0] if result.data else {}

    async def get(self, domain: str) -> dict | None:
        result = await asyncio.to_thread(
            lambda: self.client.table(self.TABLE)
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
        enriched_dt = datetime.fromisoformat(
            enriched_at.replace("Z", "+00:00")
        )
        return datetime.now(timezone.utc) - enriched_dt > timedelta(
            days=max_age_days
        )

    async def get_with_watchlist(self, domain: str) -> dict | None:
        """Get company profile merged with watchlist signals."""
        company = await self.get(domain)
        if not company:
            return None

        # Fetch watchlist signals separately
        signals = None
        try:
            watchlist_result = await asyncio.to_thread(
                lambda: self.client.table("company_watchlist")
                .select(
                    "transformation_signal_score, signal_type, "
                    "signal_evidence, kununu_score, kununu_sentiment"
                )
                .eq("domain", domain)
                .execute()
            )
            if watchlist_result.data:
                signals = watchlist_result.data[0]
        except Exception as e:
            logger.warning(f"Watchlist query failed for {domain}: {e}")

        company["signals"] = signals
        return company
