"""fetch-checksum-skip — skip parse+insert when a fetch-unit's raw response is unchanged.

A daily re-scrape re-fetches the SAME postings (jobs stay online for weeks). Storing the
SHA-256 of each fetch-unit's raw response lets a scraper skip parse+insert when a board/page
is byte-identical to last time — killing the daily re-insert churn that (together with the
per-row write path, since fixed) saturated the DB on 2026-06-17.

See migrations/fetch-cache.sql for the table and .specs/fetch-checksum-skip (florian-knowledge)
for the design. fetch_key granularity: ATS -> board slug, aggregators -> "keyword:page".

Fail-open by design: ANY cache error (no creds, DB down, timeout) degrades to the normal path
(parse+insert) and never blocks a scrape. Mirrors db_slugs.load_active_slugs.
"""
import asyncio
import hashlib
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

TABLE = "fetch_cache"


def checksum(body: str) -> str:
    """SHA-256 hex of the raw response body (computed on resp.text, before JSON parse)."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class FetchCache:
    """Per fetch-unit SHA-256 cache. One instance per scrape run, shared across boards.

    ``seen_unchanged`` returns True when the body matches the stored checksum (and bumps
    ``last_fetched_at``); False on a miss/change OR on ANY error (fail-open). ``record``
    upserts the new checksum on a miss/change. The scraper does ~3 lines in its board loop::

        cache = FetchCache()
        ...
        if await cache.seen_unchanged(src, slug, body):
            continue          # unchanged board — skip parse+insert
        ... parse ...
        await cache.record(src, slug, body)
    """

    def __init__(self, client=None):
        # client injectable for tests; otherwise built lazily from settings (mirrors
        # db_slugs). Missing creds / build failure -> None -> every op fails open.
        self.client = client if client is not None else self._build_client()

    @staticmethod
    def _build_client():
        try:
            from supabase import create_client

            from app.config import get_settings

            s = get_settings()
            if not s.supabase_url or not s.supabase_key:
                return None
            return create_client(s.supabase_url, s.supabase_key)
        except Exception as e:  # noqa: BLE001 — any failure must fail open
            logger.warning("fetch_cache: client build failed (%s) — fail-open", e)
            return None

    async def seen_unchanged(self, source: str, fetch_key: str, body: str) -> bool:
        """True iff ``body`` matches the stored checksum for ``(source, fetch_key)``.

        On a match, bumps ``last_fetched_at`` and returns True so the caller skips
        parse+insert. On a miss/change returns False (caller parses, then calls ``record``).
        On ANY error returns False — fail-open onto the normal path.
        """
        if self.client is None:
            return False
        digest = checksum(body)
        try:
            res = await asyncio.to_thread(
                lambda: self.client.table(TABLE)
                .select("checksum")
                .eq("source_name", source)
                .eq("fetch_key", fetch_key)
                .limit(1)
                .execute()
            )
            rows = res.data or []
            if rows and rows[0].get("checksum") == digest:
                await asyncio.to_thread(
                    lambda: self.client.table(TABLE)
                    .update({"last_fetched_at": datetime.now(timezone.utc).isoformat()})
                    .eq("source_name", source)
                    .eq("fetch_key", fetch_key)
                    .execute()
                )
                return True
            return False
        except Exception as e:  # noqa: BLE001 — fail-open onto normal parse+insert path
            logger.warning(
                "fetch_cache.seen_unchanged failed (%s/%s): %s — normal path",
                source,
                fetch_key,
                e,
            )
            return False

    async def record(self, source: str, fetch_key: str, body: str) -> None:
        """Upsert the checksum for ``(source, fetch_key)`` after a miss/change.

        Bumps both ``last_fetched_at`` and ``last_changed_at`` (this row only changes when
        the response changed). Errors are swallowed — recording is best-effort.
        """
        if self.client is None:
            return
        now = datetime.now(timezone.utc).isoformat()
        digest = checksum(body)
        try:
            await asyncio.to_thread(
                lambda: self.client.table(TABLE)
                .upsert(
                    {
                        "source_name": source,
                        "fetch_key": fetch_key,
                        "checksum": digest,
                        "last_fetched_at": now,
                        "last_changed_at": now,
                    },
                    on_conflict="source_name,fetch_key",
                )
                .execute()
            )
        except Exception as e:  # noqa: BLE001 — recording is best-effort
            logger.warning("fetch_cache.record failed (%s/%s): %s", source, fetch_key, e)
