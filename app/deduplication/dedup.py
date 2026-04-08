import logging

from app.models.job import NormalizedJob

logger = logging.getLogger(__name__)


class DeduplicationService:
    def __init__(self, supabase_client):
        self.client = supabase_client

    async def filter_batch(
        self, jobs: list[NormalizedJob]
    ) -> tuple[list[NormalizedJob], int]:
        """Filter out duplicate jobs using batch queries.

        3-tier dedup: external_id -> URL -> content_hash.
        """
        if not jobs:
            return [], 0

        duplicate_indices: set[int] = set()

        # Build lookup maps
        url_map: dict[str, list[int]] = {}
        hash_map: dict[str, list[int]] = {}
        eid_map: dict[str, list[int]] = {}

        for i, job in enumerate(jobs):
            if job.url:
                url_map.setdefault(job.url, []).append(i)
            if job.content_hash:
                hash_map.setdefault(job.content_hash, []).append(i)
            if job.external_id:
                eid_map.setdefault(job.external_id, []).append(i)

        try:
            # Tier 1: Check external_id
            if eid_map:
                existing_eids = await self._batch_check(
                    "external_id", list(eid_map.keys())
                )
                for eid in existing_eids:
                    for idx in eid_map.get(eid, []):
                        duplicate_indices.add(idx)

            # Tier 2: Check URL
            remaining_urls = {
                u: idxs
                for u, idxs in url_map.items()
                if not all(i in duplicate_indices for i in idxs)
            }
            if remaining_urls:
                existing_urls = await self._batch_check(
                    "url", list(remaining_urls.keys())
                )
                for url in existing_urls:
                    for idx in url_map.get(url, []):
                        duplicate_indices.add(idx)

            # Tier 3: Check content_hash
            remaining_hashes = {
                h: idxs
                for h, idxs in hash_map.items()
                if not all(i in duplicate_indices for i in idxs)
            }
            if remaining_hashes:
                existing_hashes = await self._batch_check(
                    "content_hash", list(remaining_hashes.keys())
                )
                for h in existing_hashes:
                    for idx in hash_map.get(h, []):
                        duplicate_indices.add(idx)

        except Exception as e:
            logger.error(f"Batch dedup failed: {e}")

        filtered = [
            job for i, job in enumerate(jobs) if i not in duplicate_indices
        ]
        dup_count = len(duplicate_indices)
        logger.info(f"Dedup: {dup_count} duplicates from {len(jobs)} jobs")
        return filtered, dup_count

    async def _batch_check(
        self, column: str, values: list[str]
    ) -> set[str]:
        """Check which values already exist in jobs table, in chunks of 500."""
        existing: set[str] = set()
        for start in range(0, len(values), 500):
            chunk = values[start : start + 500]
            try:
                result = (
                    self.client.table("jobs")
                    .select(column)
                    .in_(column, chunk)
                    .execute()
                )
                existing.update(
                    row[column] for row in (result.data or [])
                )
            except Exception as e:
                logger.warning(
                    f"Batch check on '{column}' failed: {e}"
                )
        return existing
