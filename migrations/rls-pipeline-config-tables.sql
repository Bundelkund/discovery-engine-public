-- Advisor follow-up — RLS on the remaining backend-internal pipeline tables
-- (schema-audit + app-audit 2026-07-03).
--
-- Advisor flagged ERROR rls_disabled_in_public on three more tables that PostgREST
-- exposes but that only the engine's service_role touches:
--
--   sources      : the scrape source/connector registry (18 rows). Written by the
--                  engine at config time; never read by a frontend.
--   scrape_runs  : per-run scrape log / observability (279 rows). Backend telemetry.
--   fetch_cache  : upstream board fetch checksums + payloads (1 768 rows). Internal
--                  fetch-skip cache; may hold scraped upstream payloads → the most
--                  sensitive of the three to leave anon-readable.
--
-- service_role BYPASSES RLS, so enabling RLS with no policy denies anon/authenticated
-- (closing the public-read exposure) without touching the pipeline. Same decision as
-- the raw_jobs / dedup_memory / jobs_v2 (rls-new-tables.sql) and ats_* / cr_*
-- (rls-internal-tables.sql) cohorts.
--
-- Reversible: `ALTER TABLE ... DISABLE ROW LEVEL SECURITY` if an anon reader surfaces.

ALTER TABLE public.sources     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.scrape_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.fetch_cache ENABLE ROW LEVEL SECURITY;
