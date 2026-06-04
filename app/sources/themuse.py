import logging
from datetime import datetime

import httpx

from app.config import get_settings
from app.models.job import RawJob
from app.registry.source_registry import SourceRegistry
from app.sources.base import BaseScraper

logger = logging.getLogger(__name__)


@SourceRegistry.register("themuse")
class TheMuseScraper(BaseScraper):
    """The Muse public jobs API — jobs + company-culture profiles.

    GET https://www.themuse.com/api/public/jobs — api_key optional
    (works keyless but rate-limited). Description is HTML.
    """

    source_id = "themuse"
    BASE_URL = "https://www.themuse.com/api/public/jobs"

    async def fetch(self, config: dict) -> list[RawJob]:
        try:
            # api_key optional — keyless works (rate-limited)
            api_key = config.get("api_key") or get_settings().themuse_api_key
            location = config.get("location", "Germany")
            limit = config.get("limit", 50)
            category = config.get("category")
            max_pages = max(1, (limit + 19) // 20)

            all_jobs: list[RawJob] = []
            async with httpx.AsyncClient(timeout=30.0) as client:
                for page in range(1, max_pages + 1):
                    params: dict = {"page": page, "location": location}
                    if api_key:
                        params["api_key"] = api_key
                    if category:
                        params["category"] = category
                    resp = await client.get(self.BASE_URL, params=params)
                    resp.raise_for_status()
                    data = resp.json()
                    results = data.get("results", [])
                    if not results:
                        break
                    for result in results:
                        company = (result.get("company") or {}).get("name", "")
                        locations = result.get("locations") or []
                        loc = locations[0].get("name", "") if locations else ""
                        landing = (result.get("refs") or {}).get("landing_page", "")
                        raw = RawJob(
                            title=result.get("name", ""),
                            url=landing,
                            company=company,
                            location=loc,
                            description=result.get("contents", ""),
                            source="themuse",
                            external_id=str(result.get("id", "")),
                            posted_at=self._parse_date(
                                result.get("publication_date", "")
                            ),
                            raw_data={"levels": result.get("levels", [])},
                        )
                        all_jobs.append(raw)
                    if len(all_jobs) >= limit:
                        break

            all_jobs = all_jobs[:limit]
            logger.info(f"The Muse: fetched {len(all_jobs)} jobs")
            return all_jobs
        except Exception as e:
            logger.error(f"The Muse fetch failed: {e}")
            return []

    def _parse_date(self, value: str):
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
