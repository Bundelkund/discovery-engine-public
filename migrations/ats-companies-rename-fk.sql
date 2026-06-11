-- Canonical `source` rename + FK on ats_companies. See docs/adr/sources-dimension.md.
-- Resolves the 4-sided synonym (ats_companies.ats vs jobs/jobs_v2/raw_jobs.source) by
-- renaming the ONE odd-one-out to `source`. Fact tables already use `source` -> untouched.
-- Order matters: free the name first (provenance source -> origin), then ats -> source.
-- ATOMIC with code (seed on_conflict key + row_from key-swap) -- same deploy or seed breaks.
-- Applied via supabase apply_migration (project guocdgjpbvsvcvchgolm). Repo-of-record copy.

ALTER TABLE public.ats_companies RENAME COLUMN source TO origin;   -- provenance cc/scrape/manual; CHECK follows
ALTER TABLE public.ats_companies RENAME COLUMN ats    TO source;   -- canonical provider; PK (source,slug) + indexes follow

ALTER TABLE public.ats_companies
  ADD CONSTRAINT ats_companies_source_fkey FOREIGN KEY (source) REFERENCES public.sources(code);
