"""Source metadata lookup, backed by the `sources` dimension table.

Single source of truth for "is this provider an ATS, aggregator, feed or internal?".
The distinction used to live only implicitly in @SourceRegistry.register classes; this
exposes it as data so DE-filter / monitor / refine logic can branch on type instead of
hard-matching source-string lists. See docs/adr/sources-dimension.md.

Degrades to {} (-> source_type == 'unknown') on any failure (no creds, table missing),
mirroring db_slugs.load_active_slugs so pytest stays green without a live DB.
"""
import logging
from functools import lru_cache

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _types() -> dict[str, str]:
    """Return ``{code: type}`` for every row in ``sources``. ``{}`` on any error."""
    try:
        from supabase import create_client

        from app.config import get_settings

        s = get_settings()
        if not s.supabase_url or not s.supabase_key:
            return {}
        client = create_client(s.supabase_url, s.supabase_key)
        res = client.table("sources").select("code,type").execute()
        return {r["code"]: r["type"] for r in (res.data or []) if r.get("code")}
    except Exception as e:  # noqa: BLE001 — any failure degrades to unknown
        logger.warning("source_meta load failed: %s", e)
        return {}


def source_type(code: str) -> str:
    """ats | aggregator | feed | internal | unknown (unmapped / DB unavailable)."""
    return _types().get(code, "unknown")


def is_aggregator(code: str) -> bool:
    """True if ``code`` is a keyword-search aggregator (no per-slug company feed)."""
    return source_type(code) == "aggregator"
