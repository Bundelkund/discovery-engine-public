"""Search-terms provider for source adapters.

Separates term resolution from scraper logic so adapters never hard-code
a fallback list.  Two providers are available:

  local  (default) — reads ``search_terms`` from config/sources.yaml for
                      the requested source.  Backward-compatible: produces
                      the same list the adapters used to inline.
  union             — fetches an anonymous DISTINCT union from the tenant
                      API (GET /tenant/search-terms/union).  INERT behind
                      the default flag; the endpoint does not exist yet.
                      Flip ``terms_provider: union`` in sources.yaml only
                      when the tenant service is live (Spec T P5).

Provider selection: set ``terms_provider: "local"`` (default) or
``terms_provider: "union"`` at the top-level of sources.yaml.

Returned type is always ``list[str]`` — no profile_id, no person data.
"""

import logging
from abc import ABC, abstractmethod

import httpx

from app.config import load_sources_config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class BaseTermsProvider(ABC):
    @abstractmethod
    def get_terms(self, source_id: str | None = None) -> list[str]:
        """Return a deduplicated list of search terms."""


# ---------------------------------------------------------------------------
# Local provider — reads search_terms from sources.yaml
# ---------------------------------------------------------------------------


class LocalTermsProvider(BaseTermsProvider):
    """Default provider: reads the ``search_terms`` array from sources.yaml.

    Falls back to an empty list when no terms are configured so adapters
    that call ``resolve_search_terms()`` never raise; they just return no
    results, which is the same behaviour as before de-hardcoding.
    """

    def get_terms(self, source_id: str | None = None) -> list[str]:
        cfg = load_sources_config()
        sources = cfg.get("sources", {})
        if source_id is None:
            # No source_id: return deduplicated union across all sources
            seen: set[str] = set()
            result: list[str] = []
            for src_cfg in sources.values():
                for t in src_cfg.get("search_terms", []):
                    if t not in seen:
                        seen.add(t)
                        result.append(t)
            return result
        # Named source_id: return only that source's terms (empty if unknown)
        raw = sources.get(source_id, {}).get("search_terms", [])
        seen_set: set[str] = set()
        deduped: list[str] = []
        for term in raw:
            if term not in seen_set:
                seen_set.add(term)
                deduped.append(term)
        return deduped


# ---------------------------------------------------------------------------
# Union provider — anonymous HTTP GET, inert behind default flag
# ---------------------------------------------------------------------------


class UnionTermsProvider(BaseTermsProvider):
    """Fetches an anonymous DISTINCT union of terms from the tenant API.

    Contract: GET /tenant/search-terms/union -> {"terms": ["...", ...]}
    No profile_id is sent or returned.

    INERT: the tenant endpoint does not exist yet (Spec T P5).  This
    provider must NOT be selected by default.  The flag-flip is owned by
    a separate spec item.
    """

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def get_terms(self, source_id: str | None = None) -> list[str]:
        url = f"{self._base_url}/tenant/search-terms/union"
        try:
            resp = httpx.get(url, timeout=self._timeout)
            resp.raise_for_status()
            raw: list[str] = resp.json().get("terms", [])
        except Exception as e:
            logger.error("union_terms_provider_failed url=%s error=%s", url, e)
            raw = []
        # Deduplicate while preserving server order
        seen: set[str] = set()
        deduped: list[str] = []
        for term in raw:
            if term not in seen:
                seen.add(term)
                deduped.append(term)
        return deduped


# ---------------------------------------------------------------------------
# Factory + public resolve function
# ---------------------------------------------------------------------------

_PROVIDER_LOCAL = "local"
_PROVIDER_UNION = "union"


def _build_provider() -> BaseTermsProvider:
    """Build the active provider from the top-level ``terms_provider`` key.

    Default: ``local``.  ``union`` requires explicit opt-in and a
    ``terms_provider_url`` key in sources.yaml.
    """
    cfg = load_sources_config()
    provider_name = cfg.get("terms_provider", _PROVIDER_LOCAL)

    if provider_name == _PROVIDER_UNION:
        base_url = cfg.get("terms_provider_url", "")
        if not base_url:
            logger.error(
                "terms_provider=union but terms_provider_url not set; "
                "falling back to local provider"
            )
            return LocalTermsProvider()
        logger.info("terms_provider=union url=%s", base_url)
        return UnionTermsProvider(base_url=base_url)

    if provider_name != _PROVIDER_LOCAL:
        logger.warning(
            "unknown terms_provider=%r; falling back to local", provider_name
        )
    return LocalTermsProvider()


def resolve_search_terms(source_id: str | None = None) -> list[str]:
    """Return the active search-term list for *source_id*.

    This is the single public entry-point that all source adapters call
    instead of hard-coding ``["AI Consultant"]``.

    Args:
        source_id: The registry ID of the scraper (e.g. ``"adzuna"``).
                   Pass ``None`` to get the global union of all sources.

    Returns:
        Deduplicated ``list[str]``.  Never raises; returns ``[]`` on any
        config or network error so adapters degrade gracefully.
    """
    try:
        provider = _build_provider()
        return provider.get_terms(source_id=source_id)
    except Exception as e:
        logger.error("resolve_search_terms failed source_id=%s error=%s", source_id, e)
        return []
