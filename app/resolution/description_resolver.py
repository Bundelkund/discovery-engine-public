import asyncio
import logging
from urllib.parse import urlparse

import httpx

from app.models.job import NormalizedJob
from app.resolution.fingerprint import detect_provider
from app.resolution.html_text import html_to_text

logger = logging.getLogger(__name__)

GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}"
DEFAULT_UA = "Mozilla/5.0 (compatible; discovery-engine/1.0)"

# Anti-bot / interstitial fingerprints. A page that 200s but contains one of
# these is a block-page (captcha, "unusual traffic"), NOT a posting — reject
# even though it may be longer than the thin original. Matched against
# text.lower(); keep ascii-safe variants alongside umlaut forms.
DEFAULT_BLOCK_MARKERS = (
    "unusual traffic",
    "ungewöhnlichen datenverkehr",
    "datenverkehr von ihrem",
    "kein roboter",
    "not a robot",
    "verify you are human",
    "captcha",
    "enable javascript",
    "access denied",
)


class DescriptionResolver:
    """Backfills thin job descriptions by fetching full text from the posting's
    origin (ATS or career page).

    Generic path: follow redirects, strip the final page's HTML. Greenhouse
    special-case: hit its JSON board API (cleaner than scraping HTML). Best-
    effort throughout — any fetch failure leaves the job unchanged.
    """

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.min_chars = self.config.get("min_description_chars", 200)
        self.max_resolve = self.config.get("max_resolve", 100)
        self.concurrency = self.config.get("concurrency", 5)
        self.timeout_s = self.config.get("timeout_s", 20)
        # Output-quality gate (A): accept resolved text only if it clears this
        # absolute floor — not merely "longer than the thin original". A 231-char
        # careerjet captcha beat a 140-char snippet on length alone; the floor
        # rejects it before it can poison Stage-1 scoring.
        self.min_resolved_chars = self.config.get("min_resolved_chars", 300)
        # Tracker/aggregator redirect hosts (B): their URLs are click-trackers,
        # not posting origins — server-side fetches hit anti-bot interstitials.
        # Skipped before fetch so they don't burn the max_resolve budget.
        self.blocked_hosts = [
            h.lower() for h in self.config.get("blocked_hosts", [])
        ]
        self.block_markers = [
            m.lower()
            for m in self.config.get("block_page_markers", DEFAULT_BLOCK_MARKERS)
        ]

    async def resolve_batch(self, jobs: list[NormalizedJob]) -> int:
        """Fill thin descriptions in-place. Returns count of jobs actually filled."""
        targets = [
            j
            for j in jobs
            if (getattr(j, "url", "") or "")
            and len(getattr(j, "description", "") or "") < self.min_chars
            and not self._host_blocked(getattr(j, "url", "") or "")
        ]
        if not targets:
            return 0
        if len(targets) > self.max_resolve:
            logger.info(
                "Resolution capped at %d/%d targets", self.max_resolve, len(targets)
            )
            targets = targets[: self.max_resolve]

        headers = {"User-Agent": DEFAULT_UA, "Accept": "*/*"}
        sem = asyncio.Semaphore(self.concurrency)
        filled = 0
        async with httpx.AsyncClient(
            timeout=self.timeout_s, headers=headers, follow_redirects=True
        ) as client:
            results = await asyncio.gather(
                *(self._resolve_one(client, sem, j) for j in targets)
            )
        filled = sum(1 for r in results if r)
        logger.info("Descriptions resolved: %d/%d targets", filled, len(targets))
        return filled

    async def _resolve_one(self, client, sem, job: NormalizedJob) -> bool:
        url = getattr(job, "url", "") or ""
        provider = detect_provider(url)
        if not provider:
            return False
        async with sem:
            try:
                if provider == "greenhouse":
                    text = await self._fetch_greenhouse(client, url)
                else:
                    text = await self._fetch_generic(client, url)
            except Exception as e:
                logger.debug("Resolve failed for %s: %s", url[:80], e)
                return False
        if not text or len(text) < self.min_resolved_chars:
            return False
        if self._looks_blocked(text):
            logger.debug("Block-page rejected for %s", url[:80])
            return False
        if len(text) > len(job.description or ""):
            job.description = text
            return True
        return False

    def _host_blocked(self, url: str) -> bool:
        """True if the URL's host is a configured tracker/aggregator redirect."""
        if not self.blocked_hosts:
            return False
        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            return False
        return any(
            host == b or host.endswith("." + b) for b in self.blocked_hosts
        )

    def _looks_blocked(self, text: str) -> bool:
        """True if resolved text matches an anti-bot / interstitial fingerprint."""
        low = text.lower()
        return any(m in low for m in self.block_markers)

    async def _fetch_generic(self, client, url: str) -> str:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except Exception as e:
            logger.debug("Generic fetch failed for %s: %s", url[:80], e)
            return ""
        # Re-fingerprint the FINAL url: an aggregator redirect may land on
        # Greenhouse, in which case its JSON API gives cleaner text.
        final_url = str(resp.url)
        if final_url != url and detect_provider(final_url) == "greenhouse":
            gh = await self._fetch_greenhouse(client, final_url)
            if gh:
                return gh
        return html_to_text(resp.text)

    async def _fetch_greenhouse(self, client, url: str) -> str:
        board, job_id = self._parse_greenhouse(url)
        if not board or not job_id:
            return ""
        try:
            resp = await client.get(
                GREENHOUSE_API.format(board=board, job_id=job_id)
            )
            resp.raise_for_status()
            content = resp.json().get("content", "") or ""
        except Exception as e:
            logger.debug("Greenhouse fetch failed for %s: %s", url[:80], e)
            return ""
        return html_to_text(content)

    @staticmethod
    def _parse_greenhouse(url: str) -> tuple[str, str]:
        """Extract (board, job_id) from a Greenhouse posting URL.

        Handles boards.greenhouse.io/{board}/jobs/{id} and
        job-boards.greenhouse.io/{board}/jobs/{id}.
        """
        try:
            parts = [p for p in urlparse(url).path.split("/") if p]
        except Exception:
            return "", ""
        if "jobs" in parts:
            idx = parts.index("jobs")
            if idx >= 1 and idx + 1 < len(parts):
                board = parts[idx - 1]
                job_id = parts[idx + 1]
                if job_id.isdigit():
                    return board, job_id
        return "", ""
