import logging
from urllib.parse import quote_plus

import httpx

from app.config import get_settings
from app.models.job import RawJob
from app.registry.source_registry import SourceRegistry
from app.sources.base import BaseScraper

logger = logging.getLogger(__name__)


@SourceRegistry.register("linkedin")
class LinkedInScraper(BaseScraper):
    source_id = "linkedin"
    APIFY_ACTOR = "curious_coder~linkedin-jobs-scraper"
    APIFY_URL = (
        "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
    )

    async def fetch(self, config: dict) -> list[RawJob]:
        try:
            token = get_settings().apify_api_token
            if not token:
                logger.warning("LinkedIn: APIFY_API_TOKEN not set, skipping")
                return []

            search_terms: list[str] = config.get("search_terms", [])
            location: str = config.get("location", "Berlin")
            results_per_term: int = config.get("results_per_term", 25)
            scrape_company: bool = config.get("scrape_company", False)
            timeout_s: float = config.get("timeout_s", 600.0)

            urls = [
                "https://www.linkedin.com/jobs/search/?"
                f"keywords={quote_plus(term)}&location={quote_plus(location)}"
                "&position=1&pageNum=0"
                for term in search_terms
            ]
            if not urls:
                logger.info("LinkedIn: no search_terms configured, skipping")
                return []

            payload = {
                "urls": urls,
                "count": results_per_term,
                "scrapeCompany": scrape_company,
            }
            url = self.APIFY_URL.format(actor=self.APIFY_ACTOR)

            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.post(
                    url, params={"token": token}, json=payload
                )
                resp.raise_for_status()
                data = resp.json()

            return [self._to_raw_job(item) for item in data if self._has_job(item)]
        except Exception as e:
            logger.error(f"LinkedIn fetch failed: {e}")
            return []

    @staticmethod
    def _has_job(item: dict) -> bool:
        return bool(item.get("title") or item.get("jobTitle"))

    @staticmethod
    def _to_raw_job(item: dict) -> RawJob:
        title = item.get("title") or item.get("jobTitle") or ""
        company = item.get("companyName") or item.get("company") or ""
        location = item.get("location") or item.get("jobLocation") or ""
        url = item.get("jobUrl") or item.get("link") or item.get("url") or ""
        job_id = str(item.get("jobId") or item.get("id") or "")
        external_id = job_id or url
        description = item.get("description") or ""
        salary = item.get("salary") or ""
        return RawJob(
            title=title,
            url=url,
            company=company,
            location=location,
            description=description,
            salary=str(salary) if salary else "",
            source="linkedin",
            external_id=external_id,
            raw_data=item,
        )
