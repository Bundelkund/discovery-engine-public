import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.models.job import RawJob
from app.repositories.base import BaseRepository

logger = logging.getLogger(__name__)


class RawJobRepository(BaseRepository):
    TABLE = "raw_jobs"

    # Bulk-insert chunk size. PostgREST sends one INSERT per chunk; 500 keeps the
    # request body modest while turning a 17k-row scrape into ~34 round-trips
    # instead of 17k.
    _INSERT_CHUNK = 500

    # Refine source-priority tiers. fetch_new drains tier 1 first, then the middle
    # (any unlisted source), then tier 3 last. greenhouse alone is ~40% of the inbox
    # and is high-volume / low-signal; draining it first (or in arbitrary physical
    # order) let it hog refine's per-job HTTP-resolution throughput while the
    # targeted niche sources actually applied through sat unrefined for days.
    # debt: priorities hardcoded; move to config/refine.yaml when a second consumer
    # needs a different order (upgrade-trigger: multi-tenant shelf).
    REFINE_PRIORITY_SOURCES = ["softgarden", "personio", "adzuna", "linkedin", "arbeitsagentur"]
    REFINE_DEFERRED_SOURCES = ["greenhouse"]

    @staticmethod
    def _build_row(job: RawJob) -> dict:
        row: dict = {
            "title": job.title,
            "url": job.url,
            "company": job.company,
            "location": job.location,
            "description": job.description,
            "salary": job.salary,
            "source": job.source,
            "external_id": job.external_id,
            # raw_data is intentionally NOT persisted (L1, 2026-07-02): it was 66% of
            # the raw_jobs table (~106 MB) yet the refine pipeline never reads it back
            # from the DB — refine works off `description`. The field still lives on the
            # in-memory RawJob (adapters like arbeitsagentur/factorial read it during the
            # same fetch), we just don't write the blob to staging. Column dropped in
            # migration drop-raw-data.sql.
            "content_hash": job.content_hash,
            # posted_at is optional; omit if absent so DB default / NULL applies
        }
        if job.posted_at is not None:
            row["posted_at"] = job.posted_at.isoformat()
        return row

    async def insert_batch(self, raw_jobs: list[RawJob]) -> int:
        """Insert raw jobs with status defaulting to 'new'.

        raw_data is NOT persisted (L1): it was dead weight in staging (never read by
        refine) and dominated table size. Returns the count of successfully inserted rows.

        Performance: a daily re-scrape re-fetches the SAME postings (jobs stay
        online for weeks), so almost every row collides with the
        uq_raw_jobs_source_external_id unique index. The old path issued ONE insert
        per row and swallowed the 23505 — ~17k individual round-trips for greenhouse,
        >1h wall-time, repeated every day. Instead we now:
          1. pre-filter: drop rows whose (source, external_id) already exists in
             raw_jobs (mirrors the unique index), so known postings never hit the DB;
          2. bulk-insert the genuinely-new rows in chunks;
          3. fall back to per-row only for the rare residual conflict (a race with a
             concurrent insert) — never for the common all-known case.
        The partial unique index (WHERE external_id <> '') can't be used as an
        ON CONFLICT target via PostgREST, which is why we pre-filter rather than upsert.
        """
        if not raw_jobs:
            return 0

        rows = [self._build_row(job) for job in raw_jobs]

        existing = await self._existing_keys(rows)
        # external_id == '' is outside the partial unique index → never deduped,
        # always pass through. Everything else is kept only if not already present.
        fresh = [
            r for r in rows
            if not r["external_id"] or (r["source"], r["external_id"]) not in existing
        ]
        skipped = len(rows) - len(fresh)
        if skipped:
            logger.info(
                "raw_jobs_prefiltered_known",
                extra={"skipped": skipped, "fresh": len(fresh)},
            )
        return await self._bulk_insert(fresh)

    async def _existing_keys(self, rows: list[dict]) -> set[tuple[str, str]]:
        """Return the set of (source, external_id) already present in raw_jobs.

        Pulls all known external_ids for each source in the batch (paginated). This
        is the same scope the unique index enforces, so pre-filtering against it
        exactly mirrors which rows would 23505. One scan per source (~18 round-trips
        for a 17k-row source) vs one failed insert per duplicate row.
        """
        by_source: dict[str, set[str]] = {}
        for r in rows:
            if r["external_id"]:
                by_source.setdefault(r["source"], set()).add(r["external_id"])

        existing: set[tuple[str, str]] = set()
        for source in by_source:
            page = 0
            while True:
                res = await asyncio.to_thread(
                    lambda p=page, s=source: self.client.table(self.TABLE)
                    .select("external_id")
                    .eq("source", s)
                    .neq("external_id", "")
                    .range(p * 1000, p * 1000 + 999)
                    .execute()
                )
                data = res.data or []
                for row in data:
                    eid = row.get("external_id")
                    if eid:
                        existing.add((source, eid))
                if len(data) < 1000:
                    break
                page += 1
        return existing

    async def _bulk_insert(self, rows: list[dict]) -> int:
        """Insert rows in chunks; per-row fallback only on a chunk error."""
        if not rows:
            return 0
        inserted = 0
        for i in range(0, len(rows), self._INSERT_CHUNK):
            chunk = rows[i : i + self._INSERT_CHUNK]
            try:
                await asyncio.to_thread(
                    lambda c=chunk: self.client.table(self.TABLE).insert(c).execute()
                )
                inserted += len(chunk)
            except Exception as exc:
                # A residual unique collision (race vs concurrent insert) or a single
                # bad row must not drop the whole chunk — retry the chunk row-by-row.
                logger.warning(
                    "raw_jobs_bulk_insert_fallback",
                    extra={"chunk": len(chunk), "error": str(exc)[:200]},
                )
                inserted += await self._insert_per_row(chunk)
        return inserted

    async def _insert_per_row(self, rows: list[dict]) -> int:
        """Per-row insert tolerating duplicate (23505) skips. Fallback path only."""
        inserted = 0
        for row in rows:
            try:
                await asyncio.to_thread(
                    lambda r=row: self.client.table(self.TABLE).insert(r).execute()
                )
                inserted += 1
            except Exception as exc:
                if "23505" in str(exc):
                    logger.debug("raw_job duplicate skipped: %s", row.get("url", "")[:80])
                else:
                    logger.error(
                        "raw_job_insert_failed",
                        extra={"url": row.get("url", "")[:80], "error": str(exc)},
                    )
        return inserted

    async def fetch_new(self, limit: int = 100) -> list[dict]:
        """Atomically CLAIM up to `limit` raw_jobs(status='new') rows for the refine
        pipeline, ordered by SOURCE PRIORITY, then oldest-first within each tier.

        AUDIT-P1-04: this is no longer a plain SELECT (two concurrent drains —
        multiple uvicorn workers, a second replica, or POST /refine racing the
        scheduler — would fetch and double-process the same rows). Selection AND
        claim now happen in ONE Postgres transaction via the ``claim_refine_batch``
        RPC (migrations/atomic-refine-claim.sql): ``SELECT … FOR UPDATE SKIP
        LOCKED`` + ``UPDATE status='refining'``. Concurrent callers receive
        DISJOINT batches; the tier order (priority first, unlisted middle,
        deferred/greenhouse last; FIFO by ingested_at within tier) is preserved
        inside the SQL — external behavior is unchanged apart from the rows being
        claimed ('refining') instead of left 'new'.

        Lifecycle: the pipeline moves every claimed row to a terminal status
        (refined | rejected | duplicate) or explicitly releases it back to 'new'
        for retry. Rows orphaned in 'refining' by a crash are recovered by
        ``reclaim_stale_refining`` (called at every drain start).
        """
        res = await asyncio.to_thread(
            lambda: self.client.rpc(
                "claim_refine_batch",
                {
                    "p_limit": limit,
                    "p_priority_sources": self.REFINE_PRIORITY_SOURCES,
                    "p_deferred_sources": self.REFINE_DEFERRED_SOURCES,
                },
            ).execute()
        )
        return res.data or []

    async def reclaim_stale_refining(self, stale_after_seconds: int = 1800) -> int:
        """Release 'refining' rows claimed more than ``stale_after_seconds`` ago
        back to 'new'. Returns the count released.

        Crash recovery for the claim state (mirrors ScrapeRunRepository's stale
        reclaim, the AUDIT-P0-05 zombie lesson): a drain that dies mid-pass leaves
        its batch stuck 'refining' — invisible to fetch_new — forever. TIME-BASED
        (not reclaim-all-at-startup) so a sibling worker's in-flight claim is never
        stolen: only claims older than the window (default 30 min, far beyond one
        pass) are presumed dead. Called best-effort at every drain start.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)
        ).isoformat()
        res = await asyncio.to_thread(
            lambda: self.client.table(self.TABLE)
            .update({"status": "new", "refine_claimed_at": None})
            .eq("status", "refining")
            .lt("refine_claimed_at", cutoff)
            .execute()
        )
        return len(res.data or [])

    async def backlog_metrics(self) -> dict:
        """Health signal for the refine inbox: how many rows are stuck 'new' and
        how old the oldest one is.

        ``new_count`` climbing or ``oldest_new_age_hours`` exceeding the scrape
        cadence means refine has stalled (the exact failure that left jobs_v2
        frozen for 8 days, undetected). Surfaced on /health so monitoring can
        alert long before the shelf goes stale.
        """
        count_res = await asyncio.to_thread(
            lambda: self.client.table(self.TABLE)
            .select("id", count="exact")
            .eq("status", "new")
            .limit(1)
            .execute()
        )
        new_count = count_res.count or 0

        oldest_age_hours = 0.0
        if new_count:
            oldest_res = await asyncio.to_thread(
                lambda: self.client.table(self.TABLE)
                .select("ingested_at")
                .eq("status", "new")
                .order("ingested_at")
                .limit(1)
                .execute()
            )
            rows = oldest_res.data or []
            if rows and rows[0].get("ingested_at"):
                oldest = datetime.fromisoformat(rows[0]["ingested_at"])
                if oldest.tzinfo is None:
                    oldest = oldest.replace(tzinfo=timezone.utc)
                delta = datetime.now(timezone.utc) - oldest
                oldest_age_hours = round(delta.total_seconds() / 3600, 1)

        return {"new_count": new_count, "oldest_new_age_hours": oldest_age_hours}

    async def mark_status(self, job_id: str, status: str) -> None:
        """Update the status of a single raw_jobs row.

        Valid values: 'refined' | 'rejected' | 'duplicate' (terminal), or 'new' to
        RELEASE a claimed row for retry (e.g. after an upsert failure). 'refining'
        is never set here — only the claim_refine_batch RPC claims rows.
        """
        await asyncio.to_thread(
            lambda: self.client.table(self.TABLE)
            .update({"status": status})
            .eq("id", job_id)
            .execute()
        )
