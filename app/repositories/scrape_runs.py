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
        """Atomically CLAIM ``source`` by inserting its 'running' row; return the
        row id, or None when the claim is lost.

        AUDIT-P1-04: the partial unique index uq_scrape_runs_one_running_per_source
        (one 'running' row per source, migrations/atomic-refine-claim.sql) makes
        this insert the cross-process single-flight gate — it replaces the old
        in-process ``_scrape_running`` bool. A 23505 means another worker/replica
        is scraping this source RIGHT NOW → return None, caller skips. Any other
        error propagates (unchanged behavior).
        """
        try:
            res = await asyncio.to_thread(
                lambda: self.client.table(self.TABLE)
                .insert({"source": source, "status": "running"})
                .execute()
            )
        except Exception as exc:
            if "23505" in str(exc):
                logger.info("scrape_run_claim_conflict", extra={"source": source})
                return None
            raise
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

    async def reclaim_stale_running(self, stale_before: datetime) -> int:
        """Mark 'running' rows started BEFORE ``stale_before`` as 'failed'.

        Crash recovery for the per-source claim: a process killed mid-scrape never
        record_finish-es, and its zombie 'running' row would block that source
        forever via the one-running-per-source unique index. Every LIVE scrape is
        hard-capped by ``asyncio.wait_for(source_timeout_seconds)``, so any row
        older than timeout + margin is provably a crash orphan. TIME-BASED (was:
        reclaim-all at startup) so a sibling worker's legitimately in-flight run
        is never stolen (AUDIT-P1-04, multi-worker safety). Called best-effort at
        every run_due cycle start. Returns the count reclaimed.
        """
        res = await asyncio.to_thread(
            lambda: self.client.table(self.TABLE)
            .update(
                {
                    "status": "failed",
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "error": "abandoned: stale running row (crashed process)",
                }
            )
            .eq("status", "running")
            .lt("started_at", stale_before.isoformat())
            .execute()
        )
        return len(res.data or [])

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
