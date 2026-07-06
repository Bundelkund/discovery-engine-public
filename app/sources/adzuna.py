import logging
import re

import httpx

from app.config import get_settings
from app.models.job import RawJob
from app.registry.source_registry import SourceRegistry
from app.services.terms_provider import resolve_search_terms
from app.sources.base import BaseScraper

logger = logging.getLogger(__name__)

# Adzuna's apply-page CTA ("Bewerbung als …") leaks into company.display_name
# and sometimes prefixes the title. It is presentation garbage, never an
# employer name — observed 2026-07 (dedup-company-noise-escape): the same Capco
# posting shipped once as company="Bewerbung als" and once as company="Capco".
_APPLY_LABEL_RE = re.compile(r"^\s*bewerbung\s+als\b[:\s]*", re.IGNORECASE)


def _clean_company(company: str) -> str:
    """Drop the 'Bewerbung als …' apply-label; it is a CTA, not a company."""
    company = (company or "").strip()
    if _APPLY_LABEL_RE.match(company):
        return ""
    return company


def _clean_title(title: str, company: str) -> str:
    """Strip Adzuna presentation artefacts from the title.

    - leading 'Bewerbung als ' apply-label ("Bewerbung als Pflegekraft" → "Pflegekraft")
    - trailing company echo ("Title - Capco" with company="Capco" → "Title");
      some feed variants append the employer to the title, others don't — the
      echo would poison the canonical content_hash and double the company in UI.
    """
    t = (title or "").strip()
    t = _APPLY_LABEL_RE.sub("", t)
    if company:
        echo = re.compile(
            r"\s*[-–—|:]\s*" + re.escape(company.strip()) + r"\s*$", re.IGNORECASE
        )
        t = echo.sub("", t)
    return t.strip()


@SourceRegistry.register("adzuna")
class AdzunaScraper(BaseScraper):
    source_id = "adzuna"
    BASE_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"

    async def fetch(self, config: dict) -> list[RawJob]:
        try:
            settings = get_settings()
            app_id = config.get("app_id") or settings.adzuna_app_id
            app_key = config.get("app_key") or settings.adzuna_app_key
            if not app_id or not app_key:
                logger.warning("Adzuna: missing app_id or app_key, skipping")
                return []

            country = config.get("country", "de")
            limit = config.get("limit", 50)
            search_terms = config.get("search_terms") or resolve_search_terms("adzuna")

            all_jobs = []
            async with httpx.AsyncClient(timeout=30.0) as client:
                for term in search_terms:
                    url = self.BASE_URL.format(country=country, page=1)
                    params = {
                        "app_id": app_id,
                        "app_key": app_key,
                        "what": term,
                        "results_per_page": min(limit, 50),
                    }
                    resp = await client.get(url, params=params)
                    resp.raise_for_status()
                    data = resp.json()
                    for result in data.get("results", []):
                        company = _clean_company(
                            result.get("company", {}).get("display_name", "")
                        )
                        raw = RawJob(
                            title=_clean_title(result.get("title", ""), company),
                            url=result.get("redirect_url", ""),
                            company=company,
                            location=result.get("location", {}).get(
                                "display_name", ""
                            ),
                            description=result.get("description", ""),
                            salary=str(result.get("salary_min", "")),
                            source="adzuna",
                            external_id=str(result.get("id", "")),
                            raw_data=result,
                        )
                        all_jobs.append(raw)

            logger.info(f"Adzuna: fetched {len(all_jobs)} jobs")
            return all_jobs
        except Exception as e:
            logger.error(f"Adzuna fetch failed: {e}")
            return []
