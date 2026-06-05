import asyncio
import base64
import logging
from datetime import datetime

import httpx

from app.models.job import RawJob
from app.registry.source_registry import SourceRegistry
from app.sources.base import BaseScraper

logger = logging.getLogger(__name__)


@SourceRegistry.register("arbeitsagentur")
class ArbeitsagenturScraper(BaseScraper):
    """Bundesagentur fuer Arbeit Jobsuche API (the German master aggregator).

    Two-phase fetch:
      1. Search  GET /pc/v4/jobs?was={term}&wo={location}&umkreis={r}&size={n}
                 -> list of postings with `refnr` (+ metadata only, no full text).
      2. Detail  GET /pc/v4/jobdetails/{base64(refnr)}
                 -> `stellenangebotsBeschreibung` = full description.

    The search endpoint returns metadata ONLY (like adzuna). The detail call
    backfills the full job text so downstream Stage-1 scoring + MinHash dedup
    have real content to work on. Detail calls are concurrency-capped and
    best-effort: a failed detail keeps the job (without description) rather
    than dropping it.

    Public static API key, no secret needed: header X-API-Key: jobboerse-jobsuche.
    `wo=""` (empty) searches all of Germany -> lifts the Berlin-lock that made
    HDI/Hannover postings structurally invisible.
    """

    source_id = "arbeitsagentur"
    SEARCH_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobs"
    DETAIL_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobdetails/{token}"
    PORTAL_URL = "https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}"
    API_KEY = "jobboerse-jobsuche"
    DEFAULT_UA = "Mozilla/5.0 (compatible; discovery-engine/1.0)"

    async def fetch(self, config: dict) -> list[RawJob]:
        try:
            search_terms = config.get("search_terms", ["AI Consultant"])
            location = config.get("location", "")          # "" -> nationwide
            umkreis = config.get("umkreis", 50)
            size = config.get("size", 50)
            detail = config.get("detail", True)
            max_detail = config.get("max_detail", 300)

            headers = {
                "X-API-Key": self.API_KEY,
                "User-Agent": config.get("user_agent", self.DEFAULT_UA),
                "Accept": "application/json",
            }

            # ---- Phase 1: search, collect unique postings by refnr ----------
            by_refnr: dict[str, RawJob] = {}
            async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
                for term in search_terms:
                    params = {"was": term, "umkreis": umkreis, "size": size}
                    if location:
                        params["wo"] = location
                    try:
                        resp = await client.get(self.SEARCH_URL, params=params)
                        resp.raise_for_status()
                        data = resp.json()
                    except Exception as e:
                        logger.warning(f"BA search failed for '{term}': {e}")
                        continue
                    for item in data.get("stellenangebote", []):
                        refnr = item.get("refnr", "")
                        if not refnr or refnr in by_refnr:
                            continue
                        by_refnr[refnr] = self._parse_listing(item, refnr, term)

            jobs = list(by_refnr.values())
            logger.info(f"Arbeitsagentur: {len(jobs)} unique postings from search")

            # ---- Phase 2: detail-enrich (full description) ------------------
            if detail and jobs:
                targets = jobs[:max_detail]
                if len(jobs) > max_detail:
                    logger.info(
                        f"Arbeitsagentur: detail capped at {max_detail}/{len(jobs)}"
                    )
                sem = asyncio.Semaphore(5)
                async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
                    await asyncio.gather(
                        *(self._enrich(client, sem, job) for job in targets)
                    )

            logger.info(f"Arbeitsagentur: fetched {len(jobs)} jobs")
            return jobs
        except Exception as e:
            logger.error(f"Arbeitsagentur fetch failed: {e}")
            return []

    def _parse_listing(self, item: dict, refnr: str, term: str) -> RawJob:
        ort = item.get("arbeitsort", {}) or {}
        location = " ".join(
            p for p in [ort.get("plz", ""), ort.get("ort", ""), ort.get("region", "")] if p
        ).strip()
        external_url = item.get("externeUrl", "") or ""
        url = external_url or self.PORTAL_URL.format(refnr=refnr)
        return RawJob(
            title=item.get("titel", "") or item.get("beruf", ""),
            url=url,
            company=item.get("arbeitgeber", ""),
            location=location,
            description="",  # backfilled in _enrich
            source=self.source_id,
            external_id=f"ba_{refnr}",
            posted_at=self._parse_date(item.get("aktuelleVeroeffentlichungsdatum", "")),
            raw_data={"refnr": refnr, "externeUrl": external_url, "term": term},
        )

    async def _enrich(self, client, sem, job: RawJob) -> None:
        refnr = job.raw_data.get("refnr", "")
        if not refnr:
            return
        token = base64.b64encode(refnr.encode()).decode()
        async with sem:
            try:
                resp = await client.get(self.DETAIL_URL.format(token=token))
                resp.raise_for_status()
                data = resp.json()
                job.description = data.get("stellenangebotsBeschreibung", "") or ""
            except Exception as e:
                logger.debug(f"BA detail failed for {refnr}: {e}")

    def _parse_date(self, value: str):
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")[:26])
        except ValueError:
            return None
