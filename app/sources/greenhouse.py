import logging
from pathlib import Path

import httpx
import yaml

from app.config import resolve_local_override
from app.models.job import RawJob
from app.registry.source_registry import SourceRegistry
from app.services.fetch_cache import FetchCache
from app.sources.base import BaseScraper
from app.sources.db_slugs import merge_slugs

logger = logging.getLogger(__name__)
CONFIG_DIR = Path(__file__).parent.parent.parent / "config"


@SourceRegistry.register("greenhouse")
class GreenhouseScraper(BaseScraper):
    source_id = "greenhouse"
    BASE_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"

    async def fetch(self, config: dict) -> list[RawJob]:
        try:
            portals_file = config.get("portals_file", "config/portals.yaml")
            portals_path = resolve_local_override(portals_file)
            slugs = merge_slugs(self._load_slugs(portals_path), self.source_id)

            all_jobs = []
            cache = FetchCache()
            async with httpx.AsyncClient(timeout=30.0) as client:
                for slug in slugs:
                    try:
                        url = self.BASE_URL.format(slug=slug)
                        resp = await client.get(url, params={"content": "true"})
                        resp.raise_for_status()
                        body = resp.text  # raw body (pre-JSON) = stablest checksum input
                        if await cache.seen_unchanged(self.source_id, slug, body):
                            continue  # board byte-identical to last run — skip parse+insert
                        data = resp.json()
                        for job_data in data.get("jobs", []):
                            raw = RawJob(
                                title=job_data.get("title", ""),
                                url=job_data.get("absolute_url", ""),
                                company=slug,
                                location=self._extract_location(job_data),
                                description=job_data.get("content", ""),
                                source="greenhouse",
                                external_id=str(job_data.get("id", "")),
                                raw_data=job_data,
                            )
                            all_jobs.append(raw)
                        await cache.record(self.source_id, slug, body)
                    except Exception as e:
                        logger.warning(f"Greenhouse slug '{slug}' failed: {e}")
                        continue

            logger.info(
                f"Greenhouse: fetched {len(all_jobs)} jobs from {len(slugs)} boards"
            )
            return all_jobs
        except Exception as e:
            logger.error(f"Greenhouse fetch failed: {e}")
            return []

    def _load_slugs(self, portals_path: Path) -> list[str]:
        """Extract Greenhouse slugs from portals.yaml."""
        if not portals_path.exists():
            logger.warning(f"Portals file not found: {portals_path}")
            return []
        with open(portals_path) as f:
            data = yaml.safe_load(f)
        slugs = []
        for company in data.get("tracked_companies", []):
            if not company.get("enabled", True):
                continue
            api_url = company.get("api", "")
            if "greenhouse.io" in api_url:
                parts = api_url.split("/boards/")
                if len(parts) > 1:
                    slug = parts[1].split("/")[0]
                    slugs.append(slug)
        return slugs

    def _extract_location(self, job_data: dict) -> str:
        location = job_data.get("location", {})
        if isinstance(location, dict):
            return location.get("name", "")
        return str(location)
