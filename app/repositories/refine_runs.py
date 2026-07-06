"""RefineRunRepository — flow telemetry for drain() cycles (refine_runs table).

One row per drain cycle: terminal-state counts plus WIP snapshots
(raw_jobs.status='new' before/after, oldest-'new' age). Backs the /health
``flow`` block (P1 flow diagnostics, spec: .specs/p1-flow-diagnostics.md).

Mirrors ScrapeRunRepository: async methods wrapping the sync supabase client in
``asyncio.to_thread`` (CLAUDE.md convention — supabase-py is sync and would
otherwise block the FastAPI event loop). All writes are observability, not
critical path: callers treat failures as best-effort.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.repositories.base import BaseRepository

logger = logging.getLogger(__name__)


class RefineRunRepository(BaseRepository):
    TABLE = "refine_runs"

    async def insert(
        self, started_at: datetime, finished_at: datetime, stats: dict
    ) -> dict | None:
        """Write one cycle's metrics; return the inserted row (or None)."""
        res = await asyncio.to_thread(
            lambda: self.client.table(self.TABLE)
            .insert(
                {
                    "started_at": started_at.isoformat(),
                    "finished_at": finished_at.isoformat(),
                    "stats": stats,
                }
            )
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None

    async def get_wip_count(self) -> int:
        """Count raw_jobs.status='new' (current refine WIP)."""
        res = await asyncio.to_thread(
            lambda: self.client.table("raw_jobs")
            .select("id", count="exact")
            .eq("status", "new")
            .limit(1)
            .execute()
        )
        return res.count or 0

    async def get_oldest_new_age_seconds(self) -> int:
        """Seconds since the oldest 'new' raw_job was ingested (flow-time proxy)."""
        res = await asyncio.to_thread(
            lambda: self.client.table("raw_jobs")
            .select("ingested_at")
            .eq("status", "new")
            .order("ingested_at")
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows or not rows[0].get("ingested_at"):
            return 0
        oldest = datetime.fromisoformat(rows[0]["ingested_at"].replace("Z", "+00:00"))
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        return int((datetime.now(timezone.utc) - oldest).total_seconds())

    async def get_last_n_cycles(self, n: int, hours: int = 24) -> list[dict]:
        """Last ``n`` cycles within the last ``hours``, newest first.

        The window cutoff is computed client-side: PostgREST filter values are
        literals, so ``now() - interval`` cannot be pushed into the query.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        res = await asyncio.to_thread(
            lambda: self.client.table(self.TABLE)
            .select("*")
            .gte("finished_at", cutoff)
            .order("finished_at", desc=True)
            .limit(n)
            .execute()
        )
        return res.data or []
