-- ATS scan-run audit + status (T8 cc-recheck-cron). Applied via supabase-MCP
-- (project guocdgjpbvsvcvchgolm, migration name: ats_scan_runs).
-- One row per /scan/{stage} trigger. n8n fires fire-and-forget; this table is the
-- pollable status + audit trail. RLS off (server-only, mirrors cr_* / ats_companies).
CREATE TABLE IF NOT EXISTS public.ats_scan_runs (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  stage       text NOT NULL CHECK (stage = ANY (ARRAY['revalidate','discover','discover-lever'])),
  status      text NOT NULL DEFAULT 'running'
                CHECK (status = ANY (ARRAY['running','done','failed'])),
  started_at  timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  stats       jsonb,          -- {scanner_rc, seed_rc, seed_summary, log_path}
  error       text
);

ALTER TABLE public.ats_scan_runs DISABLE ROW LEVEL SECURITY;

-- latest-run lookup per stage (overlap guard + GET /scan/runs)
CREATE INDEX IF NOT EXISTS ats_scan_runs_stage_started_idx
  ON public.ats_scan_runs (stage, started_at DESC);

COMMENT ON TABLE public.ats_scan_runs IS
  'Audit + status for ATS-scanner runs triggered via POST /scan/{stage} (T8). '
  'Stage B revalidate (daily) / Stage A discover (monthly). n8n triggers fire-and-forget; '
  'status running->done|failed set by the engine BackgroundTask. Feeds docs/ats-pipeline.md.';
