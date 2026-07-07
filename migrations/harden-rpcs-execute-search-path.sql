-- AUDIT-P0-04 — Lock down SECURITY DEFINER / mutable-search_path RPCs
-- (security-review + supabase-schema-audit 2026-07-05; live advisor 2026-07-07).
--
-- Two issues flagged by the Supabase security advisor:
--
-- 1) anon_security_definer_function_executable: get_unmatched_jobs() and db_health()
--    are SECURITY DEFINER and EXECUTABLE by anon/authenticated/PUBLIC via /rest/v1/rpc/.
--    Caller audit (owner:Bundelkund, 2026-07-07): the ONLY code caller of
--    get_unmatched_jobs is jobhunt worker/services/matching_service.py, which connects
--    with the service_role key (worker/services/auth.py, supabase_client.py). db_health
--    has no code caller (manual/monitoring probe). The WonderApply *frontend* (the only
--    anon client) calls neither. => revoking anon/authenticated/PUBLIC is safe;
--    service_role keeps its own EXECUTE grant.
--
-- 2) function_search_path_mutable: get_unmatched_jobs, purge_raw_jobs, merge_step_status
--    have no pinned search_path. Pin it. get_unmatched_jobs and purge_raw_jobs reference
--    every object schema-qualified (public.jobs, public.user_job_data, public.raw_jobs)
--    so search_path='' is safe. merge_step_status references wa_pipeline_runs UNqualified,
--    so it is pinned to `public` (behaviour-preserving — that is where the table lives).
--    (db_health already has search_path=public and is not re-touched here.)
--
-- Reversible: re-GRANT EXECUTE ... TO anon, authenticated; RESET the search_path.

-- 1) Revoke public execute (service_role + postgres retain their explicit grants)
REVOKE EXECUTE ON FUNCTION public.get_unmatched_jobs(uuid, integer) FROM anon, authenticated, PUBLIC;
REVOKE EXECUTE ON FUNCTION public.db_health()                       FROM anon, authenticated, PUBLIC;

-- 2) Pin search_path
ALTER FUNCTION public.get_unmatched_jobs(uuid, integer)     SET search_path = '';
ALTER FUNCTION public.purge_raw_jobs(integer)               SET search_path = '';
ALTER FUNCTION public.merge_step_status(uuid, text, jsonb)  SET search_path = public;
