import asyncio
import logging
from datetime import datetime, timezone

from app.repositories.base import BaseRepository

logger = logging.getLogger(__name__)


class ScrapeRunRepository(BaseRepository):
    """Audit + cadence state for the in-engine scrape scheduler (scrape_runs table).

    One row per source per trigger. The latest ``status='done'`` row's
    ``finished_at`` is the 24h cadence gate: ``run_due`` skips a source that
    already ran successfully within the window, so a container redeploy does NOT
    re-hit external/paid APIs. Mirrors RawJobRepository (asyncio.to_thread around
    the sync supabase client).
    """

    TABLE = "scrape_runs"

    async def last_success_at(self, source: str) -> datetime | None:
        """Most recent successful (status='done') finish time for ``source``.

        Returns None when the source has never completed a run — which makes it
        immediately due on a fresh DB. tz-aware UTC.
        """
        res = await asyncio.to_thread(
            lambda: self.client.table(self.TABLE)
            .select("finished_at")
            .eq("source", source)
            .eq("status", "done")
            .order("finished_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows or not rows[0].get("finished_at"):
            return None
        ts = datetime.fromisoformat(rows[0]["finished_at"])
        return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts

    async def record_start(self, source: str) -> str | None:
        """Insert a 'running' row for ``source``; return its id (or None on failure)."""
        res = await asyncio.to_thread(
            lambda: self.client.table(self.TABLE)
            .insert({"source": source, "status": "running"})
            .execute()
        )
        rows = res.data or []
        return rows[0].get("id") if rows else None

    async def record_finish(
        self,
        run_id: str,
        status: str,
        stats: dict | None = None,
        error: str | None = None,
    ) -> None:
        """Close a run row: set status ('done'|'failed'), finished_at, stats/error."""
        update: dict = {
            "status": status,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        if stats is not None:
            update["stats"] = stats
        if error is not None:
            update["error"] = error[:1000]
        await asyncio.to_thread(
            lambda: self.client.table(self.TABLE)
            .update(update)
            .eq("id", run_id)
            .execute()
        )

    async def latest_per_source(self) -> list[dict]:
        """Latest run per source for /health (source, status, finished_at, stats).

        Pulls a bounded recent window and keeps the newest row per source in
        Python — small table (one row per source per day), so this stays cheap and
        avoids a DISTINCT ON RPC.
        """
        res = await asyncio.to_thread(
            lambda: self.client.table(self.TABLE)
            .select("source,status,started_at,finished_at,stats")
            .order("started_at", desc=True)
            .limit(500)
            .execute()
        )
        seen: dict[str, dict] = {}
        for row in res.data or []:
            src = row.get("source")
            if src and src not in seen:
                seen[src] = {
                    "source": src,
                    "status": row.get("status"),
                    "finished_at": row.get("finished_at"),
                    "stats": row.get("stats"),
                }
        return list(seen.values())
