-- DROP guard for jobs v1 — DEFERRED ARTIFACT. DO NOT APPLY MANUALLY.
-- Date: 2026-06-08
--
-- !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
-- THIS FILE IS NOT APPLIED IN THIS BUILD AND MUST NOT BE APPLIED UNTIL:
--   1. The cutover-gate in scripts/migrate_jobs_v2.py has passed (exit 0).
--   2. app/config.py JOBS_TABLE is confirmed pointing to "jobs_v2".
--   3. A production backup/snapshot of jobs v1 has been taken.
-- Auto-drop fires from scripts/migrate_jobs_v2.py --apply-drop only.
-- Manual application outside that flow is prohibited (KI-cutover-gate).
-- !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
--
-- Drops the legacy jobs v1 table and its dependents after cutover-gate passes.
-- All production reads/writes will already be on jobs_v2 at that point.

-- Safety guard: refuse to drop if jobs_v2 does not exist yet.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'jobs_v2'
  ) THEN
    RAISE EXCEPTION 'jobs-v1-drop BLOCKED: jobs_v2 does not exist. Run migrate_jobs_v2.py first.';
  END IF;

  -- Secondary guard: refuse if jobs_v2 is empty (likely misconfigured cutover).
  IF (SELECT count(*) FROM public.jobs_v2) = 0 THEN
    RAISE EXCEPTION 'jobs-v1-drop BLOCKED: jobs_v2 is empty. Cutover-gate has not passed.';
  END IF;
END
$$;

-- Drop indexes first (implicit via DROP TABLE CASCADE, but listed for clarity)
DROP TABLE IF EXISTS public.jobs CASCADE;

COMMENT ON TABLE public.jobs_v2 IS
  'Agnostic job shelf (Spec 11 Weg B). Parallel to jobs v1 during dual-write phase. '
  'Read-switch in app/config.py (JOBS_TABLE env/flag) selects active shelf. '
  'Cutover gate: every v1 (source, external_id) present in v2 AND 100-row title/url/company diff = 0. '
  'profile_id omitted (agnostik invariant US-7). '
  'State: first_seen_at set on insert, last_seen_at updated on each upsert, status active|expired. '
  'v1 dropped: see migrations/jobs-v1-drop.sql (applied post-cutover-gate only).';
