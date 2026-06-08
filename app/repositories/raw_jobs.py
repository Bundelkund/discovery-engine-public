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
