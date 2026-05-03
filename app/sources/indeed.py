import asyncio
import logging

from jobspy import scrape_jobs

from app.models.job import RawJob
from app.registry.source_registry import SourceRegistry
from app.sources.base import BaseScraper

logger = logging.getLogger(__name__)


@SourceRegistry.register("indeed")
class IndeedScraper(BaseScraper):
    source_id = "indeed"

    async def fetch(self, config: dict) -> list[RawJob]:
        try:
            search_terms = config.get("search_terms", ["AI Consultant"])
            country = config.get("country", "germany")
            limit = config.get("limit", 50)
            proxy = config.get("proxy")

            all_jobs = []
            for term in search_terms:
                jobs_df = await asyncio.to_thread(
                    scrape_jobs,
                    site_name=["indeed"],
                    search_term=term,
                    location="Germany",
                    country_indeed=country,
                    results_wanted=limit,
                    proxy=proxy,
                )
                for _, row in jobs_df.iterrows():
                    raw = RawJob(
                        title=str(row.get("title", "")),
                        url=str(row.get("job_url", "")),
                        company=str(row.get("company", "")),
                        location=str(row.get("location", "")),
                        description=str(row.get("description", "")),
                        salary=str(row.get("compensation", "")),
                        source="indeed",
                        external_id=str(row.get("id", "")),
                    )
                    all_jobs.append(raw)
            logger.info(f"Indeed: fetched {len(all_jobs)} jobs")
            return all_jobs
        except Exception as e:
            logger.error(f"Indeed fetch failed: {e}")
            return []
