import asyncio
import html as html_module
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import httpx
import yaml

from app.config import resolve_local_override
from app.models.job import RawJob
from app.registry.source_registry import SourceRegistry
from app.sources.base import BaseScraper

logger = logging.getLogger(__name__)
CONFIG_DIR = Path(__file__).parent.parent.parent / "config"

SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
JOB_PATH_RE = re.compile(r"/job_posting/(?P<slug>[^/?#]+)$")
TRAILING_ID_RE = re.compile(r"-(?P<id>\d+)$")

H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.DOTALL)
SPAN_RE = re.compile(r"<span[^>]*>([^<]{2,80})</span>")
SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
END_MARKERS = ("Jetzt bewerben", "Bewerbungsformular", "Apply now")

WORK_MODEL_KEYWORDS = {"Remote", "Vor Ort", "Hybrid", "Onsite", "Home Office", "Home-Office"}
POSTAL_CODE_RE = re.compile(r"\b\d{4,5}\b")
COUNTRY_KEYWORDS = {"Deutschland", "Germany", "Österreich", "Austria", "Schweiz", "Switzerland"}

DETAIL_CONCURRENCY = 5
DETAIL_TIMEOUT = 20.0


@SourceRegistry.register("factorial")
class FactorialScraper(BaseScraper):
    """Factorial HR has no public JSON API.

    Strategy: fetch `{slug}.factorialhr.{de|com}/sitemap.xml` to enumerate
    `/job_posting/[kebab-title]-[id]` URLs, then fetch each detail page to
    extract the real title (h1), description body, and sidebar metadata
    (employment type, schedule, work model, department).
    """

    source_id = "factorial"
    SITEMAP_URL = "https://{slug}.factorialhr.{tld}/sitemap.xml"

    async def fetch(self, config: dict) -> list[RawJob]:
        try:
            portals_file = config.get("portals_file", "config/portals.yaml")
            portals_path = resolve_local_override(portals_file)
            entries = self._load_slugs(portals_path)

            all_jobs = []
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                for slug, tld in entries:
                    try:
                        jobs = await self._fetch_company(client, slug, tld)
                        all_jobs.extend(jobs)
                    except Exception as e:
                        logger.warning(f"Factorial slug '{slug}.{tld}' failed: {e}")
                        continue

            logger.info(
                f"Factorial: fetched {len(all_jobs)} jobs from {len(entries)} sitemaps"
            )
            return all_jobs
        except Exception as e:
            logger.error(f"Factorial fetch failed: {e}")
            return []

    async def _fetch_company(
        self, client: httpx.AsyncClient, slug: str, tld: str
    ) -> list[RawJob]:
        url = self.SITEMAP_URL.format(slug=slug, tld=tld)
        resp = await client.get(url)
        resp.raise_for_status()
        stubs = self._parse_sitemap(resp.text, slug)
        if not stubs:
            return []

        sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

        async def _enrich(stub: RawJob) -> RawJob:
            async with sem:
                try:
                    detail_resp = await client.get(stub.url, timeout=DETAIL_TIMEOUT)
                    detail_resp.raise_for_status()
                    return self._merge_detail(stub, detail_resp.text)
                except Exception as e:
                    logger.debug(f"Factorial detail fetch failed for {stub.url}: {e}")
                    return stub

        return await asyncio.gather(*(_enrich(s) for s in stubs))

    def _parse_sitemap(self, xml_text: str, slug: str) -> list[RawJob]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            logger.warning(f"Factorial sitemap parse error for {slug}: {e}")
            return []

        jobs = []
        for url_elem in root.findall(f"{SITEMAP_NS}url"):
            loc = (url_elem.findtext(f"{SITEMAP_NS}loc") or "").strip()
            m = JOB_PATH_RE.search(loc)
            if not m:
                continue
            url_slug = m.group("slug")
            id_match = TRAILING_ID_RE.search(url_slug)
            if id_match:
                external_id = id_match.group("id")
                title_slug = url_slug[: id_match.start()]
            else:
                external_id = url_slug
                title_slug = url_slug
            title = self._slug_to_title(title_slug)

            posted_at = None
            lastmod = (url_elem.findtext(f"{SITEMAP_NS}lastmod") or "").strip()
            if lastmod:
                try:
                    posted_at = datetime.fromisoformat(lastmod.replace("Z", "+00:00"))
                except ValueError:
                    posted_at = None

            jobs.append(
                RawJob(
                    title=title,
                    url=loc,
                    company=slug,
                    location="",
                    description="",
                    source="factorial",
                    external_id=external_id,
                    posted_at=posted_at,
                    raw_data={"url_slug": url_slug, "lastmod": lastmod},
                )
            )
        return jobs

    def _merge_detail(self, stub: RawJob, html: str) -> RawJob:
        """Enrich the sitemap stub with title, description, and sidebar metadata."""
        h1_title = self._extract_h1(html)
        sidebar = self._extract_sidebar(html)
        description = self._extract_description(html, sidebar)

        location = self._pick_location(sidebar)
        title = h1_title or stub.title

        raw_data = {**stub.raw_data, "sidebar": sidebar}
        if location:
            raw_data["work_model"] = location

        return RawJob(
            title=title,
            url=stub.url,
            company=stub.company,
            location=location,
            description=description,
            salary=stub.salary,
            source=stub.source,
            external_id=stub.external_id,
            posted_at=stub.posted_at,
            raw_data=raw_data,
        )

    def _extract_h1(self, html: str) -> str:
        m = H1_RE.search(html)
        if not m:
            return ""
        raw = m.group(1)
        text = TAG_RE.sub(" ", raw)
        text = html_module.unescape(text)
        return WS_RE.sub(" ", text).strip()

    def _extract_sidebar(self, html: str) -> list[str]:
        """Return the up-to-4 short metadata spans (employment, schedule, work_model, department).

        Heuristic: take the first few non-empty spans appearing after the <h1>.
        """
        h1_end = html.find("</h1>")
        if h1_end < 0:
            search_html = html
        else:
            search_html = html[h1_end : h1_end + 6000]
        spans = []
        seen = set()
        for raw in SPAN_RE.findall(search_html):
            text = html_module.unescape(raw).strip()
            text = WS_RE.sub(" ", text)
            if not text or len(text) > 60:
                continue
            if text in seen:
                continue
            seen.add(text)
            spans.append(text)
            if len(spans) >= 4:
                break
        return spans

    def _pick_location(self, sidebar: list[str]) -> str:
        """Find the most location-like sidebar item.

        Priority: work-model keyword > postal-code/country indicator > nothing.
        """
        for item in sidebar:
            if any(kw.lower() in item.lower() for kw in WORK_MODEL_KEYWORDS):
                return item
        for item in sidebar:
            if POSTAL_CODE_RE.search(item) or any(c in item for c in COUNTRY_KEYWORDS):
                return item
        return ""

    def _extract_description(self, html: str, sidebar: list[str]) -> str:
        h1_close = html.find("</h1>")
        if h1_close < 0:
            return ""
        end_idx = len(html)
        for marker in END_MARKERS:
            idx = html.find(marker, h1_close)
            if 0 < idx < end_idx:
                end_idx = idx
        body = html[h1_close:end_idx]
        body = SCRIPT_STYLE_RE.sub(" ", body)
        text = TAG_RE.sub(" ", body)
        text = html_module.unescape(text)
        text = WS_RE.sub(" ", text).strip()
        for item in sidebar:
            text = text.replace(item, "", 1)
        return WS_RE.sub(" ", text).strip()

    def _slug_to_title(self, url_slug: str) -> str:
        """Fallback title derivation when detail-page fetch fails."""
        return " ".join(part.capitalize() for part in url_slug.split("-") if part)

    def _load_slugs(self, portals_path: Path) -> list[tuple[str, str]]:
        """Extract Factorial (slug, tld) pairs from portals.yaml.

        Supports both factorialhr.de and factorialhr.com.
        """
        if not portals_path.exists():
            logger.warning(f"Portals file not found: {portals_path}")
            return []
        with open(portals_path) as f:
            data = yaml.safe_load(f)
        entries = []
        for company in data.get("tracked_companies", []):
            if not company.get("enabled", True):
                continue
            careers_url = company.get("careers_url", "")
            if "//" not in careers_url:
                continue
            host = careers_url.split("//", 1)[1].split("/", 1)[0]
            for tld in ("de", "com"):
                marker = f".factorialhr.{tld}"
                if host.endswith(marker):
                    slug = host[: -len(marker)]
                    if slug:
                        entries.append((slug, tld))
                    break
        return entries
