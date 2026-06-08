-- Tenant live-test P0 follow-up (2026-06-08): trgm GIN was insufficient.
--
-- The keywords_positive filter ORs title/description ILIKE '%term%' across N terms.
-- With a low-selectivity term (e.g. 'new work'), the planner estimates the 8-way
-- trgm BitmapOr cost above a Seq Scan and reverts to Seq Scan -> 8 ILIKE x 10513
-- rows ~= 5.4s; with count="exact" (COUNT(*) OVER()) it exceeds the authenticated
-- 8s statement_timeout -> 500. trgm only wins for 1-2 selective terms.
--
-- Real fix: token-based FTS. Expression GIN indexes on to_tsvector('simple', col)
-- — NO stored column, so no table rewrite, just an index build (like trgm). The
-- query() rewrite (app/repositories/jobs.py) switches keywords_positive from
-- `title.ilike.%t%` to PostgREST `title.wfts(simple).t`, which generates exactly
-- `to_tsvector('simple', title) @@ websearch_to_tsquery('simple', t)` — matching
-- these index expressions so the planner uses them (BitmapOr of FTS index scans).
--
-- config 'simple' = tokenize + lowercase, NO stemming/stopwords -> closest to the
-- old substring recall. Semantics change is token-boundary not substring
-- (Florian-approved 2026-06-08): 'coaching' no longer matches token 'coach'.
--
-- to_tsvector(regconfig, text) (2-arg, explicit config) is IMMUTABLE -> indexable.
-- Built on BOTH shelves: public.jobs (live /jobs source until cutover) + jobs_v2.
-- Additive + reversible (DROP INDEX). keywords_negative + SHOULD-filters unchanged.

CREATE INDEX IF NOT EXISTS idx_jobs_title_fts
  ON public.jobs USING gin (to_tsvector('simple', title));
CREATE INDEX IF NOT EXISTS idx_jobs_description_fts
  ON public.jobs USING gin (to_tsvector('simple', description));

CREATE INDEX IF NOT EXISTS idx_jobs_v2_title_fts
  ON public.jobs_v2 USING gin (to_tsvector('simple', title));
CREATE INDEX IF NOT EXISTS idx_jobs_v2_description_fts
  ON public.jobs_v2 USING gin (to_tsvector('simple', description));
