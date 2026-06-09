-- raw_jobs inbox dedup + retention (C5, found by the 2026-06-09 Chaos-Guard smoke).
--
-- The store-first pipeline treats raw_jobs as an append-only inbox. The smoke
-- proved there is NO unique constraint on it: re-scraping the same source stored
-- 12 identical rows a second time (RawJobRepository.insert_batch already CATCHES
-- 23505 and skips — but that branch is dead code with no constraint to violate).
--
-- Impact is bounded: the refine pipeline's exact-dedup (vs the active jobs shelf)
-- + near-dedup (dedup_memory) still protect jobs_v2 — the smoke measured a v2
-- delta of 0 on the re-scrape. The ONLY cost is raw_jobs growing unbounded with
-- every cron scrape (themuse alone returns the same ~50 rows daily) plus the
-- wasted refine work of re-deduping rows already seen.
--
-- Fix = two parts:
--   (1) a partial UNIQUE index on (source, external_id) so a re-scrape of the same
--       posting hits 23505 and insert_batch's existing skip-branch finally fires.
--       Partial (external_id <> '') because parse_raw only GUARANTEES external_id
--       downstream; the raw inbox may still hold '' for sources that carry none,
--       and we must not collapse all empty-id rows into one.
--   (2) a retention helper to drop TERMINAL rows past a window so the inbox audit
--       trail stays bounded (mirrors MinHashDedup.purge_old for dedup_memory).
--
-- Additive + reversible. NOT YET APPLIED — operator (Florian) runs this against
-- guocdgjpbvsvcvchgolm via Supabase, then redeploys if needed. Build the index
-- CONCURRENTLY to avoid locking the inbox during a live scrape.

-- (1) inbox dedup ------------------------------------------------------------
-- NOTE: CREATE INDEX CONCURRENTLY cannot run inside a transaction block. If the
-- existing rows already contain (source, external_id) duplicates from the smoke,
-- this build will FAIL — dedup them first (keep the earliest per key):
--
--   DELETE FROM public.raw_jobs a USING public.raw_jobs b
--   WHERE a.ctid < b.ctid
--     AND a.source = b.source AND a.external_id = b.external_id
--     AND a.external_id <> '';
--
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_raw_jobs_source_external_id
  ON public.raw_jobs (source, external_id)
  WHERE external_id <> '';

-- (2) retention --------------------------------------------------------------
-- Drop terminal (refined/rejected/duplicate) rows older than the window. Call
-- daily from the same cron that triggers /refine. 'new' rows are never purged
-- (they are unprocessed work). Returns the deleted row count.
CREATE OR REPLACE FUNCTION public.purge_raw_jobs(window_days int DEFAULT 42)
  RETURNS integer
  LANGUAGE sql
AS $$
  WITH del AS (
    DELETE FROM public.raw_jobs
    WHERE status IN ('refined', 'rejected', 'duplicate')
      AND created_at < now() - make_interval(days => window_days)
    RETURNING 1
  )
  SELECT count(*)::int FROM del;
$$;
-- Adjust the timestamp column name above if raw_jobs uses scraped_at/inserted_at
-- instead of created_at — verify with: \d public.raw_jobs
