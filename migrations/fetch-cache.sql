-- Fetch-Checksum-Cache for the checksum-skip optimization (fetch-checksum-skip task).
-- NOT YET APPLIED — ships together with the code that uses it (see .specs/fetch-checksum-skip).
--
-- Purpose: a daily re-scrape re-fetches the SAME postings (jobs stay online for weeks).
-- Storing the SHA-256 of each fetch-unit's raw response lets the scraper skip parse+insert
-- when a board/page is byte-identical to last time — killing the daily re-insert churn that
-- (together with the per-row write path, since fixed) saturated the DB on 2026-06-17.
--
-- Mirrors scrape_runs/ats_scan_runs conventions: server-only, RLS off. Apply via Supabase MCP
-- (project guocdgjpbvsvcvchgolm, migration name: fetch_cache).
--
-- fetch_key granularity (per source_name):
--   ATS (greenhouse/ashby/personio/recruitee/breezy/factorial/softgarden/lever) -> board slug
--   aggregators (adzuna/jooble/careerjet/indeed/themuse/arbeitsagentur)          -> keyword + page
-- One row per (source_name, fetch_key); checksum updated in place when the response changes.
CREATE TABLE IF NOT EXISTS public.fetch_cache (
  id              uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
  source_name     text         NOT NULL,
  fetch_key       text         NOT NULL,                 -- slug (ATS) | keyword:page (aggregator)
  checksum        varchar(64)  NOT NULL,                 -- SHA-256 hex of the raw response body
  last_fetched_at timestamptz  NOT NULL DEFAULT now(),   -- bumped on every fetch (changed or not)
  last_changed_at timestamptz  NOT NULL DEFAULT now(),   -- bumped only when checksum changes
  UNIQUE (source_name, fetch_key)
);

ALTER TABLE public.fetch_cache DISABLE ROW LEVEL SECURITY;

CREATE INDEX IF NOT EXISTS fetch_cache_source_key_idx
  ON public.fetch_cache (source_name, fetch_key);

COMMENT ON TABLE public.fetch_cache IS
  'Per fetch-unit SHA-256 of the last raw response (checksum-skip). Scraper skips parse+insert '
  'when checksum is unchanged, eliminating daily re-scrape churn. Server-only, RLS off.';
