-- Autonomous scrape-scheduler audit + daily-cadence gate (scrape_runner).
-- Applied via supabase-MCP (same project as ats_scan_runs, migration name: scrape_runs).
-- One row per scrape trigger PER SOURCE. The in-engine scheduler
-- (app/services/scrape_runner.py) writes 'running' on start and 'done'|'failed' on
-- finish. The latest 'done' row per source is the 24h cadence gate: a redeploy must
-- NOT re-scrape sources that already ran today (external/paid APIs). Mirrors
-- ats_scan_runs (per-stage) but keyed per source. RLS off (server-only, like cr_* /
-- ats_companies / ats_scan_runs).
CREATE TABLE IF NOT EXISTS public.scrape_runs (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source      text NOT NULL,
  status      text NOT NULL DEFAULT 'running'
                CHECK (status = ANY (ARRAY['running','done','failed'])),
  started_at  timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  stats       jsonb,          -- {jobs_found, jobs_stored, duration_ms}
  error       text
);

ALTER TABLE public.scrape_runs DISABLE ROW LEVEL SECURITY;

-- latest-run lookup per source (cadence gate + GET /health last_scrape)
CREATE INDEX IF NOT EXISTS scrape_runs_source_started_idx
  ON public.scrape_runs (source, started_at DESC);

COMMENT ON TABLE public.scrape_runs IS
  'Audit + status for in-engine scrape-scheduler runs (app/services/scrape_runner.py). '
  'One row per source per trigger. Latest done.finished_at per source is the 24h cadence '
  'gate so a redeploy does not re-hit external APIs. Surfaced on GET /health.last_scrape.';
