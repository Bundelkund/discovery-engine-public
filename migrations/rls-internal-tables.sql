-- Advisor follow-up — RLS on backend-internal tables (deep-review 2026-06-08).
--
-- Advisor flagged ERROR rls_disabled_in_public on five tables that are exposed to
-- PostgREST but only ever read/written by a backend role:
--
--   ats_companies, ats_scan_runs  : discovery-engine ATS-scanner state. The engine
--                                   writes via the service_role key (bypasses RLS).
--                                   No frontend reads these.
--   cr_companies, cr_candidates,  : company-radar n8n job-monitor registry. The n8n
--   cr_directories                  Supabase credential is the service_role key
--                                   (company-radar phase-1 spec: "Supabase Credential
--                                   in n8n verified (service_role key)"). No frontend.
--
-- service_role BYPASSES RLS, so enabling RLS with no policy denies anon/authenticated
-- (closing the public-read exposure) without touching the pipelines. Mirrors the
-- raw_jobs / dedup_memory / jobs_v2 decision (rls-new-tables.sql).
--
-- Reversible: `ALTER TABLE ... DISABLE ROW LEVEL SECURITY` if an anon reader surfaces.

ALTER TABLE public.ats_companies  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ats_scan_runs  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cr_companies   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cr_candidates  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cr_directories ENABLE ROW LEVEL SECURITY;
