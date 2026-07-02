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


@SourceRegistry.register("breezy")
class BreezyScraper(BaseScraper):
    source_id = "breezy"
    BASE_URL = "https://{slug}.breezy.hr/json"

    async def fetch(self, config: dict) -> list[RawJob]:
        try:
            portals_file = config.get("portals_file", "config/portals.yaml")
            portals_path = resolve_local_override(portals_file)
            slugs = merge_slugs(self._load_slugs(portals_path), self.source_id)

            all_jobs = []
            cache = FetchCache()
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                for slug in slugs:
                    try:
                        url = self.BASE_URL.format(slug=slug)
                        resp = await client.get(url)
                        resp.raise_for_status()
                        body = resp.text  # raw body (pre-JSON) = stablest checksum input
                        if await cache.seen_unchanged(self.source_id, slug, body):
                            continue  # board byte-identical to last run — skip parse+insert
                        positions = resp.json()
                        if not isinstance(positions, list):
                            logger.warning(
                                f"Breezy slug '{slug}' returned non-list: {type(positions).__name__}"
                            )
                            continue
                        for pos in positions:
                            company_obj = pos.get("company") or {}
                            company_name = company_obj.get("name") or slug
                            loc = self._format_location(pos.get("location") or {})
                            raw = RawJob(
                                title=pos.get("name", ""),
                                url=pos.get("url", ""),
                                company=company_name,
                                location=loc,
                                description="",
                                salary=pos.get("salary", "") or "",
                                source="breezy",
                                external_id=str(pos.get("id", "")),
                                raw_data=pos,
                            )
                            all_jobs.append(raw)
                        await cache.record(self.source_id, slug, body)
                    except Exception as e:
                        logger.warning(f"Breezy slug '{slug}' failed: {e}")
                        continue

            logger.info(
                f"Breezy: fetched {len(all_jobs)} jobs from {len(slugs)} boards"
            )
            return all_jobs
        except Exception as e:
            logger.error(f"Breezy fetch failed: {e}")
            return []

    def _format_location(self, loc: dict) -> str:
        """Build 'City, Country' from Breezy location object."""
        if not loc:
            return ""
        city = loc.get("city", "")
        country = (loc.get("country") or {}).get("name", "")
        state = (loc.get("state") or {}).get("name", "")
        parts = [p for p in (city, state, country) if p]
        return ", ".join(parts)

    def _load_slugs(self, portals_path: Path) -> list[str]:
        """Extract Breezy slugs from portals.yaml."""
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
            if "breezy.hr" in careers_url and "//" in careers_url:
                slug = careers_url.split("//")[1].split(".breezy.hr")[0]
                if slug:
                    slugs.append(slug)
        return slugs
