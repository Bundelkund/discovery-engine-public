-- Drop raw_jobs.raw_data (L1, 2026-07-02). Ships with the code that stops writing it
-- (app/repositories/raw_jobs.py _build_row).
--
-- Why: raw_data was the full source payload stored verbatim per job. Measured 2026-07-02
-- it was 106 MB = 66% of the raw_jobs table and ~24% of the entire 500 MB free-tier DB —
-- yet the refine pipeline NEVER reads it back from the DB (refine works off `description`).
-- Pure dead weight in staging. Dropping it reclaims the space and makes every future insert
-- ~3x lighter, which is what lets the engine run Lauf #1 per source without wedging the DB.
--
-- The field still lives on the in-memory RawJob (adapters like arbeitsagentur/factorial read
-- it during the same fetch to build detail URLs / enrich) — this only removes DB persistence.
--
-- Apply via Supabase MCP (project guocdgjpbvsvcvchgolm, migration name: drop_raw_data).
-- NOTE: DROP COLUMN marks the column dead but does NOT reclaim disk on its own. Run
--   VACUUM FULL public.raw_jobs;
-- separately afterwards (ACCESS EXCLUSIVE lock, cannot run inside a transaction block) to
-- actually shrink the table on disk. Engine must be stopped during the VACUUM.

ALTER TABLE public.raw_jobs DROP COLUMN IF EXISTS raw_data;
