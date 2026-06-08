-- Spec 11 follow-up — RLS on the three new pipeline tables (deep-review 2026-06-08).
--
-- Advisor flagged ERROR rls_disabled_in_public on raw_jobs, jobs_v2, dedup_memory.
-- The engine writes via the service_role key, which BYPASSES RLS, so enabling RLS
-- does not affect the pipeline.
--
-- ALL THREE: RLS on, NO policy. anon/authenticated denied, service_role bypasses.
--
--   raw_jobs / dedup_memory : internal engine state — never externally read.
--   jobs_v2                 : the agnostic clean shelf. Deliberately NOT exposed
--                             via PostgREST. Per Draht-2 discipline (Spec 11 AC-009
--                             + tenant-module AC-T-007: "kein SQL-Join auf die
--                             Engine-jobs-Tabelle"), consumers read the shelf ONLY
--                             through the engine's GET /jobs HTTP endpoint. A public
--                             SELECT policy would re-create the forbidden "hidden 3rd
--                             wire" (direct anon/SQL access) the two-module split
--                             exists to prevent.
--
-- NOTE: v1 `jobs` carries a legacy public-read policy ("Jobs are viewable by
-- everyone") that the wonderapply frontend reads via the anon key. That pattern is
-- NOT carried forward to jobs_v2 — WA must migrate to GET /jobs at cutover
-- (florian-knowledge wonderapply tasks WA-DE-07).

ALTER TABLE public.raw_jobs     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.dedup_memory ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.jobs_v2      ENABLE ROW LEVEL SECURITY;
