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


@SourceRegistry.register("softgarden")
class SoftgardenScraper(BaseScraper):
    """softgarden — DE ATS. Each customer board is {slug}.career.softgarden.de.

    Feed: /jobs.json = schema.org DataFeed; jobs live under dataFeedElement[].item
    as full JobPosting objects (title, url, description, jobLocation.address,
    identifier). DE-only TLD -> DE-leaning. See docs/ats-enumerability.md.
    """

    source_id = "softgarden"
    BASE_URL = "https://{slug}.career.softgarden.de/jobs.json"
    SUFFIX = ".career.softgarden.de"

    async def fetch(self, config: dict) -> list[RawJob]:
        try:
            portals_file = config.get("portals_file", "config/portals.yaml")
            portals_path = resolve_local_override(portals_file)
            slugs = merge_slugs(self._load_slugs(portals_path), self.source_id)

            all_jobs: list[RawJob] = []
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                for slug in slugs:
                    try:
                        resp = await client.get(self.BASE_URL.format(slug=slug))
                        resp.raise_for_status()
                        elements = resp.json().get("dataFeedElement", [])
                        for elem in elements:
                            jp = elem.get("item") if isinstance(elem, dict) else None
                            if not isinstance(jp, dict):
                                continue
                            all_jobs.append(self._to_raw(jp, slug))
                    except Exception as e:
                        logger.warning(f"Softgarden slug '{slug}' failed: {e}")
                        continue

            logger.info(
                f"Softgarden: fetched {len(all_jobs)} jobs from {len(slugs)} boards"
            )
            return all_jobs
        except Exception as e:
            logger.error(f"Softgarden fetch failed: {e}")
            return []

    def _to_raw(self, jp: dict, slug: str) -> RawJob:
        identifier = jp.get("identifier") or {}
        company = identifier.get("name") or self._org_name(jp) or slug
        external_id = str(identifier.get("value") or "")
        return RawJob(
            title=jp.get("title", ""),
            url=jp.get("url", ""),
            company=company,
            location=self._format_location(jp.get("jobLocation")),
            description=jp.get("description", ""),
            source=self.source_id,
            external_id=external_id,
            posted_at=jp.get("datePosted") or None,
            raw_data=jp,
        )

    @staticmethod
    def _org_name(jp: dict) -> str:
        org = jp.get("hiringOrganization")
        if isinstance(org, dict):
            return org.get("name", "") or ""
        return org if isinstance(org, str) else ""

    @staticmethod
    def _format_location(job_location) -> str:
        """schema.org Place.address -> 'Locality, Region' (fallback postalCode/country)."""
        addr = (job_location or {}).get("address") if isinstance(job_location, dict) else None
        if not isinstance(addr, dict):
            return ""
        parts = [addr.get("addressLocality"), addr.get("addressRegion")]
        loc = ", ".join(p for p in parts if p)
        if loc:
            return loc
        return addr.get("addressCountry") or addr.get("postalCode") or ""

    def _load_slugs(self, portals_path: Path) -> list[str]:
        """Extract softgarden slugs from portals.yaml (subdomain label of *.career.softgarden.de)."""
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
            if self.SUFFIX in careers_url and "//" in careers_url:
                host = careers_url.split("//")[1]
                slug = host.split(self.SUFFIX)[0].split(".")[-1]
                if slug:
                    slugs.append(slug)
        return slugs
