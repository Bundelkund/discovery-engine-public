import logging
import time

from app.config import load_sources_config
from app.models.job import RawJob
from app.models.responses import ScrapeResponse
from app.registry.source_registry import SourceRegistry
from app.repositories.raw_jobs import RawJobRepository

logger = logging.getLogger(__name__)


class ScrapeOrchestrator:
    def __init__(self, supabase_client):
        self.supabase = supabase_client
        self.raw_job_repo = RawJobRepository(supabase_client)

    async def run(
        self,
        source_id: str,
        location: str = None,
        limit: int = None,
        store: bool = True,
    ) -> ScrapeResponse:
        """Fetch → build RawJob list → insert into raw_jobs (status='new').

        All normalize / dedup / dq / location / score / enrich steps have moved
        to the refine pipeline (app/services/refine_pipeline.py — A3).
        """
        start = time.time()
        errors: list[str] = []
        response = ScrapeResponse(source=source_id)

        try:
            sources_config = load_sources_config().get("sources", {})
            source_config = sources_config.get(source_id, {})
            if limit:
                source_config["limit"] = limit
            if location:
                source_config["location"] = location

            scraper_cls = SourceRegistry.get(source_id)
            scraper = scraper_cls()
            raw_results = await scraper.fetch(source_config)
            response.jobs_found = len(raw_results)

            if not raw_results:
                response.duration_ms = int((time.time() - start) * 1000)
                return response

            # Build RawJob objects — raw_data holds the full source payload verbatim.
            raw_jobs: list[RawJob] = []
            for item in raw_results:
                if isinstance(item, RawJob):
                    raw_jobs.append(item)
                else:
                    # Scrapers that still return dicts or other shapes: wrap them.
                    # Normalisation (including content_hash) happens in refine pipeline.
                    data = item if isinstance(item, dict) else (
                        item.model_dump() if hasattr(item, "model_dump") else dict(item)
                    )
                    raw_jobs.append(
                        RawJob(
                            title=data.get("title", ""),
                            url=data.get("url", ""),
                            company=data.get("company", ""),
                            location=data.get("location", ""),
                            description=data.get("description", ""),
                            salary=data.get("salary", ""),
                            source=data.get("source", source_id),
                            external_id=data.get("external_id", ""),
                            posted_at=data.get("posted_at"),
                            raw_data=data,
                        )
                    )

            if store:
                stored = await self.raw_job_repo.insert_batch(raw_jobs)
                response.jobs_stored = stored

        except Exception as exc:
            logger.error("Scrape orchestrator failed: %s", exc)
            errors.append(str(exc))
            raise

        response.errors = errors
        response.duration_ms = int((time.time() - start) * 1000)
        return response
