-- P1 Flow Diagnostics (Commit 1): refine_runs — timeline of drain() cycles.
--
-- Why: measurement BEFORE the WIP gate (Commit 3). Each drain cycle writes one
-- row with its terminal-state counts + WIP snapshots, so /health can expose
-- throughput vs. arrivals vs. WIP (CFD inputs) from real data.
--
-- Spec: .specs/p1-flow-diagnostics.md. Idempotent (IF NOT EXISTS / unschedule
-- guard), additive-only, per migrations/README.md conventions.

-- Create refine_runs table (timeline of drain cycles)
CREATE TABLE IF NOT EXISTS refine_runs (
  id BIGSERIAL PRIMARY KEY,
  started_at TIMESTAMP WITH TIME ZONE NOT NULL,
  finished_at TIMESTAMP WITH TIME ZONE NOT NULL,
  stats JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

-- Index for range queries (CFD lookups)
CREATE INDEX IF NOT EXISTS idx_refine_runs_finished_at ON refine_runs(finished_at DESC);

-- Retention: Keep 30 days (matches raw_jobs 42d + dedup_memory 21d for overlap)
-- Run daily at 03:30 (before raw_jobs 42d purge at 03:42, per migrations/raw-jobs-inbox-dedup.sql:47)
-- Idempotent: unschedule before schedule (mirrors jobs-v2-retention.sql).
SELECT cron.unschedule('refine-runs-retention-30d')
WHERE EXISTS (SELECT 1 FROM cron.job WHERE jobname = 'refine-runs-retention-30d');

SELECT cron.schedule(
  'refine-runs-retention-30d',
  '30 3 * * *',
  'DELETE FROM refine_runs WHERE finished_at < now() - INTERVAL ''30 days'''
);

-- RLS: Enable, deny all by default (internal table, no tenant concept)
ALTER TABLE refine_runs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS refine_runs_deny_all ON refine_runs;
CREATE POLICY refine_runs_deny_all ON refine_runs USING (false);
-- service_role bypasses; used in app/routes/health.py

COMMENT ON TABLE refine_runs IS
  'Timeline of drain() cycles. stats = {fetched, refined, rejected, duplicate, errors, passes, wip_before, wip_after, oldest_new_age_seconds}. No tenant_id — engine-internal only.';
