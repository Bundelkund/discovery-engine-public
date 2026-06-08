-- Tenant live-test P0 (2026-06-08): /jobs 500 on profiles with multi-term keyword filters.
--
-- keywords_positive ORs title/description ILIKE '%term%'. With a low-selectivity term
-- the planner rejects trigram GIN and reverts to a ~5s Seq Scan over public.jobs ->
-- with count="exact" it exceeds the authenticated 8s statement_timeout -> 500.
--
-- Fix: token FTS. Expression GIN indexes on to_tsvector('simple', col) — no stored
-- column, so no table rewrite, just an index build. query() (app/repositories/jobs.py)
-- switches keywords_positive to PostgREST `title.wfts(simple).term`, which generates
-- `to_tsvector('simple', title) @@ websearch_to_tsquery('simple', term)` and matches
-- these index expressions (BitmapOr index scan). config 'simple' = tokenize+lowercase,
-- no stemming/stopwords. EXPLAIN on a real 4-term profile: 5403ms -> 252ms.
--
-- Applied live to guocdgjpbvsvcvchgolm. Additive + reversible (DROP INDEX). Built on
-- both public.jobs (live /jobs source) and jobs_v2 (post-cutover).

CREATE INDEX IF NOT EXISTS idx_jobs_title_fts
  ON public.jobs USING gin (to_tsvector('simple', title));
CREATE INDEX IF NOT EXISTS idx_jobs_description_fts
  ON public.jobs USING gin (to_tsvector('simple', description));

CREATE INDEX IF NOT EXISTS idx_jobs_v2_title_fts
  ON public.jobs_v2 USING gin (to_tsvector('simple', title));
CREATE INDEX IF NOT EXISTS idx_jobs_v2_description_fts
  ON public.jobs_v2 USING gin (to_tsvector('simple', description));
