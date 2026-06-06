import logging
from pathlib import Path

import httpx
import yaml

from app.config import resolve_local_override
from app.models.job import RawJob
from app.registry.source_registry import SourceRegistry
from app.sources.base import BaseScraper
from app.sources.db_slugs import merge_slugs

logger = logging.getLogger(__name__)
CONFIG_DIR = Path(__file__).parent.parent.parent / "config"


@SourceRegistry.register("recruitee")
class RecruiteeScraper(BaseScraper):
    source_id = "recruitee"
    BASE_URL = "https://{slug}.recruitee.com/api/offers"

    async def fetch(self, config: dict) -> list[RawJob]:
        try:
            portals_file = config.get("portals_file", "config/portals.yaml")
            portals_path = resolve_local_override(portals_file)
            slugs = merge_slugs(self._load_slugs(portals_path), self.source_id)

            all_jobs = []
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                for slug in slugs:
                    try:
                        url = self.BASE_URL.format(slug=slug)
                        resp = await client.get(url)
                        resp.raise_for_status()
                        data = resp.json()
                        for offer in data.get("offers", []):
                            if offer.get("status") and offer.get("status") != "published":
                                continue
                            salary_obj = offer.get("salary") or {}
                            salary_str = ""
                            if salary_obj:
                                lo = salary_obj.get("min")
                                hi = salary_obj.get("max")
                                cur = salary_obj.get("currency", "")
                                per = salary_obj.get("period", "")
                                if lo or hi:
                                    salary_str = f"{lo or ''}-{hi or ''} {cur} / {per}".strip()
                            raw = RawJob(
                                title=offer.get("title", ""),
                                url=offer.get("careers_url", ""),
                                company=offer.get("company_name") or slug,
                                location=offer.get("location", "") or offer.get("city", ""),
                                description=offer.get("description", ""),
                                salary=salary_str,
                                source="recruitee",
                                external_id=str(offer.get("id", "")),
                                raw_data=offer,
                            )
                            all_jobs.append(raw)
                    except Exception as e:
                        logger.warning(f"Recruitee slug '{slug}' failed: {e}")
                        continue

            logger.info(
                f"Recruitee: fetched {len(all_jobs)} jobs from {len(slugs)} boards"
            )
            return all_jobs
        except Exception as e:
            logger.error(f"Recruitee fetch failed: {e}")
            return []

    def _load_slugs(self, portals_path: Path) -> list[str]:
        """Extract Recruitee slugs from portals.yaml."""
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
            if "recruitee.com" in careers_url and "//" in careers_url:
                slug = careers_url.split("//")[1].split(".recruitee.com")[0]
                if slug:
                    slugs.append(slug)
        return slugs
