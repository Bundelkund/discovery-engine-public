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
# Job-posting URLs in the sitemap. The path VARIES per b-ite tenant:
#   SPK  /jobposting/<40-hex sha1>      (hex id; also = identifier.value)
#   DRK  /job-postings/<readable-slug>  (slug; id comes from JSON-LD identifier)
# Require a non-empty segment after the path so the bare listing page
# (…/job-postings) is not matched. The real external_id is JSON-LD identifier.value;
# the URL is only a fallback (_SHA_RE).
# debt: two known path schemes hardcoded; upgrade-trigger: add an employer whose
# b-ite job URLs use a third path -> extend this alternation.
_JOB_LOC_RE = re.compile(
    r"<loc>\s*([^<\s]+/(?:jobposting/[a-f0-9]{40}|job-postings/[^<\s/]+))\s*</loc>",
    re.I,
)
# Any <loc> — used to follow sitemap-index nesting (locs pointing to sub-sitemaps).
_ANY_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
_SHA_RE = re.compile(r"/jobposting/([a-f0-9]{40})", re.I)


@SourceRegistry.register("bite")
class BiteScraper(BaseScraper):
    """b-ite — DE ATS. Each customer runs a careers site on its OWN domain
    (e.g. karriere.<employer>.de), rendered by jobs.b-ite.com.

    Unlike softgarden there is no ``{slug}.vendor.tld`` board universe to
    enumerate: each employer is added explicitly. Index = the job sitemap the
    site DECLARES in robots.txt (path varies per tenant — SPK /sitemap.xml.gz,
    DRK /sitemap-job-postings.xml), a urlset of ``/jobposting/<sha>`` links; each
    posting page embeds a full schema.org JobPosting as ld+json (title,
    description, hiringOrganization, jobLocation, datePosted, identifier). Config:
    ``sources.yaml`` -> ``bite.sites = [{name, base_url}]``. Common in public sector.
    """

    source_id = "bite"
    # Job sitemap path is DISCOVERED from robots.txt (varies per tenant); these
    # are only the fallback when robots.txt declares no Sitemap.
    DEFAULT_SITEMAP_PATHS = ("/sitemap.xml.gz", "/sitemap.xml")

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
                        job_urls = await self._discover_job_urls(client, base)
                        # Board-level checksum skip keyed on the sorted job-URL set
                        # (the "index" of live postings): unchanged set -> nothing
                        # added/removed, skip the per-posting fetch. Per-posting edits
                        # aren't re-ingested anyway (raw_jobs dedups on external_id).
                        index_key = "\n".join(sorted(set(job_urls)))
                        if await cache.seen_unchanged(self.source_id, base, index_key):
                            continue
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
                        await cache.record(self.source_id, base, index_key)
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

    async def probe_site(self, client: httpx.AsyncClient, base: str) -> dict:
        """Cheaply verify a CANDIDATE careers domain is a b-ite site worth adding.

        robots.txt -> job sitemap -> ONE sample posting's fingerprint. Fetches at
        most a handful of pages regardless of how many jobs the employer has
        (used by scripts/bite_discover.py to vet discovery candidates before they
        go into config). Read-only. Returns a verdict dict::

            {base, is_bite, job_count, employer, sample_title, sitemaps, reason}
        """
        base = base.rstrip("/")
        if not base.startswith(("http://", "https://")):
            base = "https://" + base
        v = {"base": base, "is_bite": False, "job_count": 0,
             "employer": "", "sample_title": "", "sitemaps": [], "reason": ""}
        try:
            v["sitemaps"] = await self._sitemap_urls_from_robots(client, base)
            job_urls = await self._discover_job_urls(client, base, sitemaps=v["sitemaps"])
        except Exception as e:  # noqa: BLE001 — verdict, not crash
            v["reason"] = f"sitemap discovery failed: {e}"
            return v
        v["job_count"] = len(job_urls)
        if not job_urls:
            v["reason"] = "no b-ite job URLs (/jobposting or /job-postings) in sitemaps"
            return v
        try:
            resp = await client.get(job_urls[0])
            resp.raise_for_status()
            html = resp.text
        except Exception as e:  # noqa: BLE001
            v["reason"] = f"sample posting fetch failed: {e}"
            return v
        raw = self._parse_posting(html, job_urls[0], base)
        fingerprint = ("jobs.b-ite.com" in html) or ("Bite JobPosting" in html)
        if raw is None or not fingerprint:
            v["reason"] = "sample posting carries no b-ite JobPosting"
            return v
        v.update(is_bite=True, employer=raw.company, sample_title=raw.title)
        return v

    async def _discover_job_urls(
        self, client: httpx.AsyncClient, base: str, sitemaps: list[str] | None = None
    ) -> list[str]:
        """Return every ``/jobposting/<sha>`` URL for an employer.

        The job sitemap path VARIES per b-ite tenant (SPK /sitemap.xml.gz, DRK
        /sitemap-job-postings.xml), so we don't guess it: read the ``Sitemap:``
        line(s) from robots.txt and scan them, following one level of
        sitemap-INDEX nesting. Falls back to ``DEFAULT_SITEMAP_PATHS`` when robots
        declares none. Job-posting locs are collected across all declared
        sitemaps (a static/pages sitemap simply contributes none).

        ``sitemaps`` may be passed pre-fetched (probe_site already read robots) to
        avoid a second robots.txt request; pass ``None`` to have it read robots."""
        if sitemaps is None:
            sitemaps = await self._sitemap_urls_from_robots(client, base)
        if not sitemaps:
            sitemaps = [base + p for p in self.DEFAULT_SITEMAP_PATHS]

        job_urls: list[str] = []
        seen: set[str] = set()
        queue = list(sitemaps)
        guard = 0
        while queue and guard < 50:  # guard against a runaway sitemap-index loop
            guard += 1
            sm_url = queue.pop(0)
            if sm_url in seen:
                continue
            seen.add(sm_url)
            try:
                text = await self._fetch_xml(client, sm_url)
            except Exception as e:
                logger.warning(f"Bite sitemap '{sm_url}' failed: {e}")
                continue
            job_urls.extend(_JOB_LOC_RE.findall(text))
            # sitemap-index: follow nested <loc>s that point to more sitemaps
            for loc in _ANY_LOC_RE.findall(text):
                if "/jobposting/" in loc:
                    continue
                if (loc.endswith(".xml") or loc.endswith(".xml.gz")) and loc not in seen:
                    queue.append(loc)
        return list(dict.fromkeys(job_urls))  # dedup, preserve order

    async def _sitemap_urls_from_robots(
        self, client: httpx.AsyncClient, base: str
    ) -> list[str]:
        """``Sitemap:`` URLs declared in robots.txt ([] on any failure)."""
        try:
            resp = await client.get(base + "/robots.txt")
            resp.raise_for_status()
            out: list[str] = []
            for line in resp.text.splitlines():
                if line.strip().lower().startswith("sitemap:"):
                    url = line.split(":", 1)[1].strip()
                    if url:
                        out.append(url)
            return out
        except Exception:
            return []

    async def _fetch_xml(self, client: httpx.AsyncClient, url: str) -> str:
        """Fetch a sitemap URL as text. A `.gz` sitemap is served either as gzip
        bytes or (some deployments) already-decompressed XML — handle both."""
        resp = await client.get(url)
        resp.raise_for_status()
        raw = resp.content
        try:
            return gzip.decompress(raw).decode("utf-8", "replace")
        except (OSError, gzip.BadGzipFile):
            return raw.decode("utf-8", "replace")

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
