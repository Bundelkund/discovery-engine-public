-- Tenant live-test P0 (2026-06-08): /jobs 500 on profiles with >=4 search-terms.
--
-- Root cause: the consumer-agnostic query (app/repositories/jobs.py query()) filters
-- keywords_positive as `title ILIKE '%term%' OR description ILIKE '%term%'` and uses
-- select("*", count="exact"). Neither jobs nor jobs_v2 had an index supporting ILIKE
-- substring match -> Seq Scan over all rows per term per column. 4 terms x 2 cols x
-- 10513 rows ~= 6.6s for the count alone -> exceeds the authenticated-role 8s
-- statement_timeout -> 500. Scales linearly with term count.
--
-- Fix: pg_trgm GIN indexes on title + description. ILIKE '%...%' (>=3-char terms)
-- resolves via the trigram index (BitmapOr across the two columns), turning the
-- filter AND the exact count into ms-level index scans. No API/contract change.
--
-- Applied to BOTH shelves: public.jobs (the live /jobs source until the jobs_v2
-- cutover, WA-DE-07) AND jobs_v2 (so the cutover does not re-introduce the seq scan
-- once jobs_v2 is populated — it currently has NO title/description index either).
--
-- Additive + reversible: DROP INDEX ... to roll back. ~10k rows -> sub-second build.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS idx_jobs_title_trgm
  ON public.jobs USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_jobs_description_trgm
  ON public.jobs USING gin (description gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_jobs_v2_title_trgm
  ON public.jobs_v2 USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_jobs_v2_description_trgm
  ON public.jobs_v2 USING gin (description gin_trgm_ops);
