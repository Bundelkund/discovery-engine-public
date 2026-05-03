import logging
from pathlib import Path

import httpx
import yaml

from app.config import resolve_local_override
from app.models.job import RawJob
from app.registry.source_registry import SourceRegistry
from app.sources.base import BaseScraper

logger = logging.getLogger(__name__)
CONFIG_DIR = Path(__file__).parent.parent.parent / "config"


@SourceRegistry.register("lever")
class LeverScraper(BaseScraper):
    source_id = "lever"
    BASE_URL = "https://api.lever.co/v0/postings/{slug}?mode=json"

    async def fetch(self, config: dict) -> list[RawJob]:
        try:
            portals_file = config.get("portals_file", "config/portals.yaml")
            portals_path = resolve_local_override(portals_file)
            slugs = self._load_slugs(portals_path)

            all_jobs = []
            async with httpx.AsyncClient(timeout=30.0) as client:
                for slug in slugs:
                    try:
                        url = self.BASE_URL.format(slug=slug)
                        resp = await client.get(url)
                        resp.raise_for_status()
                        data = resp.json()
                        for job_data in data:
                            categories = job_data.get("categories", {})
                            raw = RawJob(
                                title=job_data.get("text", ""),
                                url=job_data.get("hostedUrl", ""),
                                company=slug,
                                location=categories.get("location", ""),
                                description=job_data.get("descriptionPlain", ""),
                                source="lever",
                                external_id=str(job_data.get("id", "")),
                                raw_data=job_data,
                            )
                            all_jobs.append(raw)
                    except Exception as e:
                        logger.warning(f"Lever slug '{slug}' failed: {e}")
                        continue

            logger.info(
                f"Lever: fetched {len(all_jobs)} jobs from {len(slugs)} boards"
            )
            return all_jobs
        except Exception as e:
            logger.error(f"Lever fetch failed: {e}")
            return []

    def _load_slugs(self, portals_path: Path) -> list[str]:
        """Extract Lever slugs from portals.yaml."""
        if not portals_path.exists():
            logger.warning(f"Portals file not found: {portals_path}")
            return []
        with open(portals_path) as f:
            data = yaml.safe_load(f)
        slugs = []
        for company in data.get("tracked_companies", []):
            if not company.get("enabled", True):
                continue
            careers_url = company.get("careers_url", "")
            if "lever.co" in careers_url:
                parts = careers_url.split("jobs.lever.co/")
                if len(parts) > 1:
                    slug = parts[1].split("/")[0]
                    slugs.append(slug)
        return slugs
