import logging
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
import yaml

from app.models.job import RawJob
from app.registry.source_registry import SourceRegistry
from app.sources.base import BaseScraper

logger = logging.getLogger(__name__)
CONFIG_DIR = Path(__file__).parent.parent.parent / "config"


@SourceRegistry.register("personio")
class PersonioScraper(BaseScraper):
    source_id = "personio"
    FEED_URL = "https://{slug}.jobs.personio.de/xml"
    JOB_URL = "https://{slug}.jobs.personio.de/job/{job_id}"

    async def fetch(self, config: dict) -> list[RawJob]:
        try:
            portals_file = config.get("portals_file", "config/portals.yaml")
            portals_path = (
                Path(portals_file)
                if Path(portals_file).is_absolute()
                else CONFIG_DIR.parent / portals_file
            )
            slugs = self._load_slugs(portals_path)

            all_jobs = []
            async with httpx.AsyncClient(timeout=30.0) as client:
                for slug in slugs:
                    try:
                        url = self.FEED_URL.format(slug=slug)
                        resp = await client.get(url)
                        resp.raise_for_status()
                        jobs = self._parse_xml(resp.text, slug)
                        all_jobs.extend(jobs)
                    except Exception as e:
                        logger.warning(f"Personio slug '{slug}' failed: {e}")
                        continue

            logger.info(
                f"Personio: fetched {len(all_jobs)} jobs from {len(slugs)} portals"
            )
            return all_jobs
        except Exception as e:
            logger.error(f"Personio fetch failed: {e}")
            return []

    def _parse_xml(self, xml_text: str, slug: str) -> list[RawJob]:
        root = ET.fromstring(xml_text)
        jobs = []
        for position in root.findall("position"):
            job_id = position.findtext("id", "")
            title = position.findtext("name", "")
            office = position.findtext("office", "")
            department = position.findtext("department", "")

            description_parts = []
            for jd in position.findall("jobDescriptions/jobDescription"):
                section_name = jd.findtext("name", "")
                section_value = jd.findtext("value", "")
                if section_name:
                    description_parts.append(section_name)
                if section_value:
                    description_parts.append(section_value)
            description = "\n\n".join(description_parts)

            job_url = self.JOB_URL.format(slug=slug, job_id=job_id)

            raw = RawJob(
                title=title,
                url=job_url,
                company=slug,
                location=office,
                description=description,
                source="personio",
                external_id=job_id,
                raw_data={
                    "department": department,
                    "employment_type": position.findtext("employmentType", ""),
                    "seniority": position.findtext("seniority", ""),
                    "schedule": position.findtext("schedule", ""),
                    "years_of_experience": position.findtext("yearsOfExperience", ""),
                    "created_at": position.findtext("createdAt", ""),
                },
            )
            jobs.append(raw)
        return jobs

    def _load_slugs(self, portals_path: Path) -> list[str]:
        """Extract Personio slugs from portals.yaml."""
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
            if "personio.de" in careers_url:
                slug = careers_url.split("//")[1].split(".jobs")[0] if "//" in careers_url else ""
                if slug:
                    slugs.append(slug)
        return slugs
