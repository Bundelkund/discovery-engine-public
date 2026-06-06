"""DB-driven slug source (T5 / db-driven-slugs).

ats_companies is the source-of-truth for which ATS boards to scrape;
config/portals.yaml is the committed curated fallback. Slug-scrapers
union both: yaml slugs (demo set) ∪ active+monitored slugs from the
table — which unlocks the ~5915 boards seeded into the registry.

load_active_slugs() returns [] on ANY error (no creds, network down,
table missing) so callers degrade to yaml-only. This keeps pytest green
without a live DB and keeps the demo deploy working unchanged.
"""
import logging

logger = logging.getLogger(__name__)


def load_active_slugs(ats: str) -> list[str]:
    """Return active+monitored slugs for ``ats`` from ats_companies.

    Equivalent to::

        SELECT slug FROM ats_companies
        WHERE ats = :ats AND status = 'active' AND monitor = true

    Returns ``[]`` on any failure so the caller falls back to its
    portals.yaml slugs (Tabelle = SoT, yaml = Fallback).
    """
    try:
        from supabase import create_client

        from app.config import get_settings

        s = get_settings()
        if not s.supabase_url or not s.supabase_key:
            return []
        client = create_client(s.supabase_url, s.supabase_key)
        res = (
            client.table("ats_companies")
            .select("slug")
            .eq("ats", ats)
            .eq("status", "active")
            .eq("monitor", True)
            .execute()
        )
        slugs = [r["slug"] for r in (res.data or []) if r.get("slug")]
        logger.info("db_slugs: %d active slugs for ats=%s", len(slugs), ats)
        return slugs
    except Exception as e:  # noqa: BLE001 — any failure must degrade to yaml-only
        logger.warning("db_slugs load failed for ats=%s: %s — yaml fallback", ats, e)
        return []


def merge_slugs(yaml_slugs: list[str], ats: str) -> list[str]:
    """Return ``yaml_slugs ∪ active-DB-slugs`` (order-preserving dedup).

    yaml slugs come first (curated set), then any DB slugs not already
    present. On DB error the result is just ``yaml_slugs`` unchanged.
    """
    db_slugs = load_active_slugs(ats)
    merged = list(dict.fromkeys([*yaml_slugs, *db_slugs]))
    logger.info(
        "merge_slugs ats=%s: %d total (%d yaml + %d db)",
        ats,
        len(merged),
        len(yaml_slugs),
        len(db_slugs),
    )
    return merged
