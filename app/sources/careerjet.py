import logging

import httpx

from app.config import get_settings
from app.models.job import RawJob
from app.registry.source_registry import SourceRegistry
from app.services.terms_provider import resolve_search_terms
from app.sources.base import BaseScraper

logger = logging.getLogger(__name__)


@SourceRegistry.register("careerjet")
class CareerjetScraper(BaseScraper):
    """Careerjet public job-search API (affid-based, broad DE coverage).

    GET http://public.api.careerjet.net/search — requires `affid` plus
    `user_ip` and `user_agent` (API rejects requests without them).
    """

    source_id = "careerjet"
    BASE_URL = "http://public.api.careerjet.net/search"
    DEFAULT_UA = "Mozilla/5.0 (compatible; discovery-engine/1.0)"
    DEFAULT_REFERER = "https://konektos.de/jobs"

    async def fetch(self, config: dict) -> list[RawJob]:
        try:
            affid = config.get("affid") or get_settings().careerjet_affid
            if not affid:
                logger.warning("Careerjet: missing affid, skipping")
                return []

            locale = config.get("locale_code", "de_DE")
            location = config.get("location", "Deutschland")
            limit = config.get("limit", 50)
            search_terms = config.get("search_terms") or resolve_search_terms("careerjet")
            user_ip = config.get("user_ip", "127.0.0.1")
            user_agent = config.get("user_agent", self.DEFAULT_UA)
            referer = config.get("referer", self.DEFAULT_REFERER)
            headers = {"Referer": referer, "User-Agent": user_agent}

            all_jobs = []
            async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
                for term in search_terms:
                    params = {
                        "affid": affid,
                        "keywords": term,
                        "location": location,
                        "locale_code": locale,
                        "pagesize": min(limit, 100),
                        "page": 1,
                        "user_ip": user_ip,
                        "user_agent": user_agent,
                    }
                    resp = await client.get(self.BASE_URL, params=params)
                    resp.raise_for_status()
                    data = resp.json()
                    for result in data.get("jobs", []):
                        url = result.get("url", "")
                        raw = RawJob(
                            title=result.get("title", ""),
                            url=url,
                            company=result.get("company", ""),
                            location=result.get("locations", ""),
                            description=result.get("description", ""),
                            salary=result.get("salary", ""),
                            source="careerjet",
                            external_id=url,
                            raw_data={"date": result.get("date", ""), "term": term},
                        )
                        all_jobs.append(raw)

            logger.info(f"Careerjet: fetched {len(all_jobs)} jobs")
            return all_jobs
        except Exception as e:
            logger.error(f"Careerjet fetch failed: {e}")
            return []
