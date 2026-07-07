-- Atomic refine batch claim + per-source scrape run claim (AUDIT-P1-04).
-- Date: 2026-07-06
-- SAFETY: additive except (a) widening the raw_jobs.status CHECK by one value
--   ('refining') and (b) closing pre-existing 'running' scrape_runs zombies so
--   the new partial unique index can build. No DROP TABLE/COLUMN, no data loss.
-- Idempotent: CREATE OR REPLACE / IF NOT EXISTS / guarded DO block — safe to re-run.
--
-- ─── DESIGN DECISION: Option B (atomic row-claim) over Option A (advisory lock) ───
-- The engine's ONLY DB access is PostgREST (supabase-py): every request runs on a
-- POOLED connection inside its own transaction. That kills Option A twice over:
-- a session advisory lock (pg_try_advisory_lock) taken in one RPC request binds to
-- whatever pooled backend connection served it and cannot be reliably released from
-- a later request (different connection) — a leaked lock would freeze refine
-- globally; a transaction advisory lock (pg_advisory_xact_lock) releases when the
-- RPC's own transaction commits, i.e. BEFORE the drain even starts — it protects
-- nothing across requests. The only construct that is genuinely atomic over
-- PostgREST is a single-statement claim: SELECT … FOR UPDATE SKIP LOCKED + UPDATE
-- status='refining' inside ONE RPC transaction. Two concurrent drains (multiple
-- uvicorn workers, replicas, POST /refine racing the scheduler) then receive
-- DISJOINT batches — true work-queue semantics, zero double-processing, and refine
-- can actually parallelize instead of being serialized. Cost: a new interim status
-- 'refining', recovered by a time-based stale reclaim (refine_runner.drain start:
-- rows claimed > 30 min ago flip back to 'new') so a crash mid-pass can never
-- strand rows (the AUDIT-P0-05 zombie-state lesson). purge_raw_jobs deletes only
-- terminal statuses, so 'refining' rows are never purged mid-flight.
--
-- DEPLOY ORDER: apply this migration BEFORE deploying the code that calls
-- claim_refine_batch (old code ignores the new status value and the new column;
-- new code hard-depends on the RPC + the widened CHECK).

-- ── (1) raw_jobs.status: allow the interim 'refining' claim state ─────────────
-- The original CHECK was inline/unnamed (auto-named raw_jobs_status_check by PG,
-- but not guaranteed); drop whichever CHECK constrains status, then re-add under
-- a stable name. Leaving the old CHECK in place would make every claim fail.
DO $$
DECLARE
  c record;
BEGIN
  FOR c IN
    SELECT conname
    FROM pg_constraint
    WHERE conrelid = 'public.raw_jobs'::regclass
      AND contype = 'c'
      AND pg_get_constraintdef(oid) ILIKE '%status%'
  LOOP
    EXECUTE format('ALTER TABLE public.raw_jobs DROP CONSTRAINT %I', c.conname);
  END LOOP;
END $$;

ALTER TABLE public.raw_jobs ADD CONSTRAINT raw_jobs_status_check
  CHECK (status = ANY (ARRAY['new', 'refining', 'refined', 'rejected', 'duplicate']));

-- ── (2) claim timestamp: drives the time-based stale reclaim ──────────────────
ALTER TABLE public.raw_jobs ADD COLUMN IF NOT EXISTS refine_claimed_at timestamptz;

COMMENT ON COLUMN public.raw_jobs.refine_claimed_at IS
  'Set by claim_refine_batch when a row is claimed (status=refining). Rows whose '
  'claim is older than the stale window are flipped back to new by the drain-start '
  'reclaim (app/services/refine_runner.py) — crash recovery, no zombie claims.';

-- ── (3) claim hot path index: status='new' filtered, ingested_at-ordered ──────
CREATE INDEX IF NOT EXISTS idx_raw_jobs_new_ingested_at
  ON public.raw_jobs (ingested_at)
  WHERE status = 'new';

-- ── (4) the claim RPC ──────────────────────────────────────────────────────────
-- Atomically claims up to p_limit status='new' rows for the refine pipeline and
-- returns them. Selection preserves fetch_new's contract exactly: 3-tier source
-- priority (p_priority_sources first, unlisted middle, p_deferred_sources last —
-- greenhouse is high-volume/low-signal and must not hog refine throughput), FIFO
-- by ingested_at within tier; a global (tier, ingested_at) sort over LIMIT is
-- equivalent to the old fill-tier-by-tier queries. FOR UPDATE SKIP LOCKED means
-- concurrent callers silently skip each other's in-flight claims instead of
-- blocking or double-claiming; under READ COMMITTED the re-check of status='new'
-- on lock acquisition excludes rows a committed concurrent claim just flipped.
-- The CTE is not inlined (it contains a locking clause), so select-lock-update
-- happens exactly once, in this statement's single transaction.
--
-- SECURITY INVOKER: the backend calls this as service_role — no escalation needed.
-- search_path pinned empty + fully-qualified references (function_search_path_mutable
-- advisor guard). No EXECUTE for anon/authenticated: internal pipeline machinery.
CREATE OR REPLACE FUNCTION public.claim_refine_batch(
  p_limit             integer,
  p_priority_sources  text[] DEFAULT '{}',
  p_deferred_sources  text[] DEFAULT '{}'
)
RETURNS SETOF public.raw_jobs
LANGUAGE sql
VOLATILE
SECURITY INVOKER
SET search_path = ''
AS $$
  WITH candidate AS (
    SELECT id
    FROM public.raw_jobs
    WHERE status = 'new'
    ORDER BY
      CASE
        WHEN source = ANY (p_priority_sources) THEN 1
        WHEN source = ANY (p_deferred_sources) THEN 3
        ELSE 2
      END,
      ingested_at,
      id
    LIMIT p_limit
    FOR UPDATE SKIP LOCKED
  ),
  claimed AS (
    UPDATE public.raw_jobs r
    SET status = 'refining',
        refine_claimed_at = now()
    FROM candidate c
    WHERE r.id = c.id
    RETURNING r.*
  )
  SELECT *
  FROM claimed
  ORDER BY
    CASE
      WHEN source = ANY (p_priority_sources) THEN 1
      WHEN source = ANY (p_deferred_sources) THEN 3
      ELSE 2
    END,
    ingested_at,
    id;
$$;

COMMENT ON FUNCTION public.claim_refine_batch(integer, text[], text[]) IS
  'AUDIT-P1-04: atomic work-queue claim for the refine pipeline. Selects up to '
  'p_limit raw_jobs(status=new) in 3-tier source-priority order (FIFO within tier), '
  'flips them to refining under FOR UPDATE SKIP LOCKED, returns the claimed rows. '
  'Concurrent callers get disjoint batches — replaces the in-process _refine_running '
  'single-flight bool. Stale refining claims are reclaimed time-based by the drain.';

REVOKE EXECUTE ON FUNCTION public.claim_refine_batch(integer, text[], text[])
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.claim_refine_batch(integer, text[], text[])
  TO service_role;

-- ── (5) scrape_runs: one 'running' row per source = cross-process scrape claim ──
-- Replaces the in-process _scrape_running bool: record_start's INSERT of the
-- 'running' row now IS the claim — the partial unique index makes a concurrent
-- second start of the same source fail with 23505, which the repository maps to
-- "claim lost, skip". Pre-existing zombies must be closed first or the index
-- build fails; a scrape genuinely in flight during migration is unaffected
-- (record_finish updates by id and overwrites the status).
UPDATE public.scrape_runs
SET status = 'failed',
    finished_at = now(),
    error = 'abandoned: closed by atomic-refine-claim migration'
WHERE status = 'running';

CREATE UNIQUE INDEX IF NOT EXISTS uq_scrape_runs_one_running_per_source
  ON public.scrape_runs (source)
  WHERE status = 'running';

COMMENT ON INDEX public.uq_scrape_runs_one_running_per_source IS
  'AUDIT-P1-04: at most one in-flight (running) scrape per source across all '
  'workers/replicas. record_start treats the 23505 as "another worker holds the '
  'claim". Stale running rows (crash orphans, older than source timeout + margin) '
  'are reclaimed time-based at each run_due cycle start.';
