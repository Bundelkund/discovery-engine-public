import gzip
import json
import logging
import re
from datetime import datetime

import httpx

from app.models.job import RawJob
from app.registry.source_registry import SourceRegistry
from app.services.fetch_cache import FetchCache
from app.sources.base import BaseScraper

logger = logging.getLogger(__name__)

# schema.org JobPosting is embedded as ld+json in each posting page.
_JSONLD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)
# Job URLs in the sitemap: /jobposting/<40-hex sha1>. Same 40-hex is the b-ite
# posting id (identifier.value) -> stable external_id / dedup key.
_JOB_LOC_RE = re.compile(r"<loc>\s*([^<\s]+/jobposting/[a-f0-9]{40})\s*</loc>", re.I)
_SHA_RE = re.compile(r"/jobposting/([a-f0-9]{40})", re.I)


@SourceRegistry.register("bite")
class BiteScraper(BaseScraper):
    """b-ite — DE ATS. Each customer runs a careers site on its OWN domain
    (e.g. karriere.<employer>.de), rendered by jobs.b-ite.com.

    Unlike softgarden there is no ``{slug}.vendor.tld`` board universe to
    enumerate: each employer is added explicitly. Index = ``{base}/sitemap.xml.gz``
    (a urlset of ``/jobposting/<sha>`` links); each posting page embeds a full
    schema.org JobPosting as ld+json (title, description, hiringOrganization,
    jobLocation, datePosted, identifier). Config: ``sources.yaml`` ->
    ``bite.sites = [{name, base_url}]``. Common in the public sector.
    """

    source_id = "bite"
    SITEMAP_PATH = "/sitemap.xml.gz"

    async def fetch(self, config: dict) -> list[RawJob]:
        try:
            sites = config.get("sites", []) or []
            all_jobs: list[RawJob] = []
            cache = FetchCache()
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                for site in sites:
                    base = (site.get("base_url") or "").rstrip("/")
                    name = site.get("name") or ""
                    if not base:
                        continue
                    try:
                        sitemap_body = await self._fetch_sitemap(client, base)
                        # Board-level checksum skip: unchanged sitemap -> no posting
                        # added/removed, skip the whole site (per-posting edits are
                        # not re-ingested anyway — raw_jobs dedups on external_id).
                        if await cache.seen_unchanged(self.source_id, base, sitemap_body):
                            continue
                        job_urls = _JOB_LOC_RE.findall(sitemap_body)
                        for job_url in job_urls:
                            try:
                                resp = await client.get(job_url)
                                resp.raise_for_status()
                                raw = self._parse_posting(resp.text, job_url, name)
                                if raw is not None:
                                    all_jobs.append(raw)
                            except Exception as e:
                                logger.warning(f"Bite posting '{job_url}' failed: {e}")
                                continue
                        await cache.record(self.source_id, base, sitemap_body)
                    except Exception as e:
                        logger.warning(f"Bite site '{base}' failed: {e}")
                        continue

            logger.info(
                f"Bite: fetched {len(all_jobs)} jobs from {len(sites)} sites"
            )
            return all_jobs
        except Exception as e:
            logger.error(f"Bite fetch failed: {e}")
            return []

    async def _fetch_sitemap(self, client: httpx.AsyncClient, base: str) -> str:
        """Return the sitemap XML text. The `.gz` path is served either as gzip
        bytes or (some deployments) as already-decompressed XML — handle both."""
        resp = await client.get(base + self.SITEMAP_PATH)
        resp.raise_for_status()
        raw = resp.content
        try:
            return gzip.decompress(raw).decode("utf-8", "replace")
        except (OSError, gzip.BadGzipFile):
            return raw.decode("utf-8", "replace")
        # debt: flat urlset only; a sitemap-INDEX (sub-sitemaps) yields 0 job locs.
        # upgrade-trigger: add an employer whose sitemap.xml.gz is an index.

    def _parse_posting(self, html: str, url: str, site_name: str) -> RawJob | None:
        m = _JSONLD_RE.search(html)
        if not m:
            return None
        try:
            d = json.loads(m.group(1))
        except json.JSONDecodeError:
            return None
        if not isinstance(d, dict) or d.get("@type") != "JobPosting":
            return None

        ident = d.get("identifier")
        external_id = ""
        if isinstance(ident, dict):
            external_id = str(ident.get("value") or "")
        if not external_id:  # fall back to the sha in the URL
            sha = _SHA_RE.search(url)
            external_id = sha.group(1) if sha else url

        org = d.get("hiringOrganization")
        company = (org.get("name") if isinstance(org, dict) else "") or site_name

        return RawJob(
            title=d.get("title", ""),
            url=url.split("?")[0],
            company=company,
            location=self._format_location(d.get("jobLocation")),
            description=d.get("description", ""),
            source=self.source_id,
            external_id=external_id,
            posted_at=self._parse_date(d.get("datePosted")),
            raw_data=d,
        )

    @staticmethod
    def _parse_date(value) -> datetime | None:
        if not value or not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            try:
                return datetime.strptime(value[:10], "%Y-%m-%d")
            except ValueError:
                return None

    @staticmethod
    def _format_location(job_location) -> str:
        """schema.org Place.address -> 'Locality, Region' (fallback country/postalCode)."""
        addr = (job_location or {}).get("address") if isinstance(job_location, dict) else None
        if not isinstance(addr, dict):
            return ""
        parts = [addr.get("addressLocality"), addr.get("addressRegion")]
        loc = ", ".join(p for p in parts if p)
        if loc:
            return loc
        return addr.get("addressCountry") or addr.get("postalCode") or ""
