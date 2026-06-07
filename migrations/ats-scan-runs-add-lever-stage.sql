-- Add 'discover-lever' to the ats_scan_runs.stage CHECK (lever-discover-cron).
-- The base ats-scan-runs.sql allowed only revalidate|discover; the new TheirStack
-- lever-discovery stage (POST /scan/discover-lever) needs its own audit stage.
-- Apply via Supabase Dashboard SQL Editor (no exec_sql RPC; see migrations/README.md).
-- Idempotent: drop-if-exists + re-add.

ALTER TABLE public.ats_scan_runs DROP CONSTRAINT IF EXISTS ats_scan_runs_stage_check;

ALTER TABLE public.ats_scan_runs
  ADD CONSTRAINT ats_scan_runs_stage_check
  CHECK (stage = ANY (ARRAY['revalidate','discover','discover-lever']));

-- Verify:
--   SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
--   WHERE conrelid = 'public.ats_scan_runs'::regclass AND conname = 'ats_scan_runs_stage_check';
