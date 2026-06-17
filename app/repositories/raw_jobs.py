import asyncio
import logging

from app.models.job import RawJob
from app.repositories.base import BaseRepository

logger = logging.getLogger(__name__)


class RawJobRepository(BaseRepository):
    TABLE = "raw_jobs"

    async def insert_batch(self, raw_jobs: list[RawJob]) -> int:
        """Insert raw jobs with status defaulting to 'new'.

        raw_data is persisted verbatim — never overwritten or defaulted to '{}'.
        Returns the count of successfully inserted rows.
        """
        if not raw_jobs:
            return 0

        rows = []
        for job in raw_jobs:
            row: dict = {
                "title": job.title,
                "url": job.url,
                "company": job.company,
                "location": job.location,
                "description": job.description,
                "salary": job.salary,
                "source": job.source,
                "external_id": job.external_id,
                "raw_data": job.raw_data,
                "content_hash": job.content_hash,
                # posted_at is optional; omit if absent so DB default / NULL applies
            }
            if job.posted_at is not None:
                row["posted_at"] = job.posted_at.isoformat()
            rows.append(row)

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
        """Return up to `limit` raw_jobs rows with status='new' for the refine pipeline."""
        result = await asyncio.to_thread(
            lambda: self.client.table(self.TABLE)
            .select("*")
            .eq("status", "new")
            .limit(limit)
            .execute()
        )
        return result.data or []

    async def backlog_metrics(self) -> dict:
        """Health signal for the refine inbox: how many rows are stuck 'new' and
        how old the oldest one is.

        ``new_count`` climbing or ``oldest_new_age_hours`` exceeding the scrape
        cadence means refine has stalled (the exact failure that left jobs_v2
        frozen for 8 days, undetected). Surfaced on /health so monitoring can
        alert long before the shelf goes stale.
        """
        from datetime import datetime, timezone

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

        Valid values: 'new', 'refined', 'rejected', 'duplicate'.
        """
        await asyncio.to_thread(
            lambda: self.client.table(self.TABLE)
            .update({"status": status})
            .eq("id", job_id)
            .execute()
        )
