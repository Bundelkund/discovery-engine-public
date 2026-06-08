import logging

from app.models.job import NormalizedJob

logger = logging.getLogger(__name__)


class DeduplicationService:
    def __init__(self, supabase_client, jobs_table: str = "jobs"):
        """Initialise with a Supabase client and the active shelf table name.

        *jobs_table* defaults to "jobs" (current shelf). Pass "jobs_v2" post-cutover
        so _batch_check queries the correct table. A2 (storage) provides the active
        table name via its read-switch and passes it here at construction time.
        """
        self.client = supabase_client
        self.jobs_table = jobs_table

    async def filter_batch(
        self, jobs: list[NormalizedJob]
    ) -> tuple[list[NormalizedJob], int, set[int]]:
        """Filter out duplicate jobs using batch queries.

        3-tier dedup: external_id -> URL -> content_hash.

        Returns:
            (kept_jobs, dup_count, duplicate_indices) where
            - kept_jobs: jobs whose index is NOT in duplicate_indices
            - dup_count: total number of duplicates found (== len(duplicate_indices))
            - duplicate_indices: set[int] of positions in the input *jobs* list
              that matched an existing record; callers (e.g. refine pipeline) use
              this to mark the corresponding raw_jobs as status='duplicate'.
        """
        if not jobs:
            return [], 0, set()

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
            logger.error("batch_dedup_failed: %s", e)

        filtered = [
            job for i, job in enumerate(jobs) if i not in duplicate_indices
        ]
        dup_count = len(duplicate_indices)
        logger.info(
            "dedup_filter_batch",
            extra={"total": len(jobs), "duplicates": dup_count, "table": self.jobs_table},
        )
        return filtered, dup_count, duplicate_indices

    async def _batch_check(
        self, column: str, values: list[str]
    ) -> set[str]:
        """Check which values already exist in the active shelf table, in chunks of 500.

        Targets self.jobs_table (set at construction time) rather than hardcoding "jobs",
        so post-cutover the query goes to jobs_v2. A2's read-switch provides the name.
        """
        existing: set[str] = set()
        for start in range(0, len(values), 500):
            chunk = values[start : start + 500]
            try:
                result = (
                    self.client.table(self.jobs_table)
                    .select(column)
                    .in_(column, chunk)
                    .execute()
                )
                existing.update(
                    row[column] for row in (result.data or [])
                )
            except Exception as e:
                logger.warning(
                    "batch_check_failed",
                    extra={"column": column, "table": self.jobs_table, "error": str(e)},
                )
        return existing
