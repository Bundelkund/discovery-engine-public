import logging
from datetime import datetime

import httpx

from app.config import get_settings
from app.models.job import RawJob
from app.registry.source_registry import SourceRegistry
from app.sources.base import BaseScraper

logger = logging.getLogger(__name__)


@SourceRegistry.register("jooble")
class JoobleScraper(BaseScraper):
    """Jooble aggregator API (strong DE coverage).

    POST https://jooble.org/api/{API_KEY} with JSON body {keywords, location}.
    """

    source_id = "jooble"
    BASE_URL = "https://{host}/api/{key}"
    DEFAULT_HOST = "jooble.org"
    DEFAULT_UA = "Mozilla/5.0 (compatible; discovery-engine/1.0)"

    async def fetch(self, config: dict) -> list[RawJob]:
        try:
            api_key = config.get("api_key") or get_settings().jooble_api_key
            if not api_key:
                logger.warning("Jooble: missing api_key, skipping")
                return []

            location = config.get("location", "Deutschland")
            limit = config.get("limit", 50)
            search_terms = config.get("search_terms", ["AI Consultant"])
            host = config.get("host", self.DEFAULT_HOST)
            url = self.BASE_URL.format(host=host, key=api_key)
            headers = {"User-Agent": config.get("user_agent", self.DEFAULT_UA)}

            all_jobs = []
            async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
                for term in search_terms:
                    body = {"keywords": term, "location": location}
                    resp = await client.post(url, json=body)
                    resp.raise_for_status()
                    data = resp.json()
                    for result in data.get("jobs", [])[:limit]:
                        raw = RawJob(
                            title=result.get("title", ""),
                            url=result.get("link", ""),
                            company=result.get("company", ""),
                            location=result.get("location", ""),
                            description=result.get("snippet", ""),
                            salary=result.get("salary", ""),
                            source="jooble",
                            external_id=str(result.get("id", "")),
                            posted_at=self._parse_date(result.get("updated", "")),
                            raw_data={"type": result.get("type", ""), "term": term},
                        )
                        all_jobs.append(raw)

            logger.info(f"Jooble: fetched {len(all_jobs)} jobs")
            return all_jobs
        except Exception as e:
            logger.error(f"Jooble fetch failed: {e}")
            return []

    def _parse_date(self, value: str):
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")[:26])
        except ValueError:
            return None
