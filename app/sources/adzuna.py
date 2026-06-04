import logging

import httpx

from app.config import get_settings
from app.models.job import RawJob
from app.registry.source_registry import SourceRegistry
from app.sources.base import BaseScraper

logger = logging.getLogger(__name__)


@SourceRegistry.register("adzuna")
class AdzunaScraper(BaseScraper):
    source_id = "adzuna"
    BASE_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"

    async def fetch(self, config: dict) -> list[RawJob]:
        try:
            settings = get_settings()
            app_id = config.get("app_id") or settings.adzuna_app_id
            app_key = config.get("app_key") or settings.adzuna_app_key
            if not app_id or not app_key:
                logger.warning("Adzuna: missing app_id or app_key, skipping")
                return []

            country = config.get("country", "de")
            limit = config.get("limit", 50)
            search_terms = config.get("search_terms", ["AI Consultant"])

            all_jobs = []
            async with httpx.AsyncClient(timeout=30.0) as client:
                for term in search_terms:
                    url = self.BASE_URL.format(country=country, page=1)
                    params = {
                        "app_id": app_id,
                        "app_key": app_key,
                        "what": term,
                        "results_per_page": min(limit, 50),
                    }
                    resp = await client.get(url, params=params)
                    resp.raise_for_status()
                    data = resp.json()
                    for result in data.get("results", []):
                        raw = RawJob(
                            title=result.get("title", ""),
                            url=result.get("redirect_url", ""),
                            company=result.get("company", {}).get(
                                "display_name", ""
                            ),
                            location=result.get("location", {}).get(
                                "display_name", ""
                            ),
                            description=result.get("description", ""),
                            salary=str(result.get("salary_min", "")),
                            source="adzuna",
                            external_id=str(result.get("id", "")),
                        )
                        all_jobs.append(raw)

            logger.info(f"Adzuna: fetched {len(all_jobs)} jobs")
            return all_jobs
        except Exception as e:
            logger.error(f"Adzuna fetch failed: {e}")
            return []
