-- Parallel agnostic jobs shelf (Spec 11 Phase 4, Card ④ — Weg B).
-- Date: 2026-06-08
-- SAFETY: additive-only — no DROP, RENAME, or ALTER TYPE statements.
-- Idempotent: CREATE TABLE/INDEX IF NOT EXISTS; safe to re-run.
--
-- Runs PARALLEL to live jobs (v1). Read-switch in app/config.py picks the
-- active shelf; cutover fires automatically once compare-gate passes.
-- jobs v1 is NOT touched here. Drop is deferred to migrations/jobs-v1-drop.sql
-- (NOT applied in this build — fires only after cutover-gate in a later run).
--
-- Schema = all live jobs v1 columns EXCEPT profile_id (agnostik invariant),
-- PLUS state columns: first_seen_at, last_seen_at, status (active|expired).
-- Upsert key: UNIQUE (source, external_id) — both NOT NULL (evidence: 0 NULL
-- external_ids in v1, 10,513 distinct pairs == 10,513 rows; content_hash has
-- 380 NULLs so cannot be sole key).

CREATE TABLE IF NOT EXISTS public.jobs_v2 (
  -- Primary key
  id                  uuid          PRIMARY KEY DEFAULT gen_random_uuid(),

  -- Upsert key (ON CONFLICT (source, external_id) DO UPDATE)
  source              text          NOT NULL,
  external_id         text          NOT NULL,

  -- Core fields (replicate v1 exactly — JobRepository.query() reads all of these)
  title               text          NOT NULL,
  company             text,
  location            text,
  remote              boolean       NOT NULL DEFAULT false,
  description         text,
  salary_min          integer,
  salary_max          integer,
  url                 text          NOT NULL,
  keywords            text[],
  scraped_at          timestamptz   NOT NULL DEFAULT now(),
  metadata            jsonb,
  job_type            text,
  content_hash        text,                       -- indexed, NOT unique (dedup fallback + compare)
  score_stage_1       integer       NOT NULL DEFAULT 0,
  score_stage_2       double precision,
  archetype           text,
  company_domain      text,
  score_stage_3       double precision,
  match_reasoning     text,
  match_highlights    text[],
  match_pitch         text,

  -- Bundle-B location/remote columns
  location_normalized text,
  location_lat        double precision,
  location_lon        double precision,
  is_remote           boolean       NOT NULL DEFAULT false,
  is_hybrid           boolean       NOT NULL DEFAULT false,

  -- Data-quality flags
  dq_flags            jsonb         NOT NULL DEFAULT '{}'::jsonb,

  -- State columns (US-4 — Job = Zustand, Expiry-Erkennung)
  first_seen_at       timestamptz   NOT NULL DEFAULT now(),
  last_seen_at        timestamptz   NOT NULL DEFAULT now(),
  status              text          NOT NULL DEFAULT 'active'
                        CHECK (status = ANY (ARRAY['active','expired'])),

  -- Upsert uniqueness constraint
  UNIQUE (source, external_id)
);

-- Consumer hot path: GET /jobs default sort = recency
CREATE INDEX IF NOT EXISTS idx_jobs_v2_scraped_at
  ON public.jobs_v2 (scraped_at DESC);

-- Expiry queries + GET /jobs?status= filter (US-4)
CREATE INDEX IF NOT EXISTS idx_jobs_v2_status
  ON public.jobs_v2 (status)
  WHERE status = 'active';

-- last_seen_at: expiry sweep ("not seen in N days → expired")
CREATE INDEX IF NOT EXISTS idx_jobs_v2_last_seen_at
  ON public.jobs_v2 (last_seen_at DESC);

-- Dedup 3-tier fallback + cutover compare
CREATE INDEX IF NOT EXISTS idx_jobs_v2_content_hash
  ON public.jobs_v2 (content_hash)
  WHERE content_hash IS NOT NULL;

-- Coverage metrics query (get_coverage_metrics uses location_normalized IS NOT NULL)
CREATE INDEX IF NOT EXISTS idx_jobs_v2_location_normalized
  ON public.jobs_v2 (location_normalized)
  WHERE location_normalized IS NOT NULL;

-- Source filter (GET /jobs?source=...)
CREATE INDEX IF NOT EXISTS idx_jobs_v2_source
  ON public.jobs_v2 (source);

-- Company domain filter (GET /jobs?company_domain=... / exclude_domain)
CREATE INDEX IF NOT EXISTS idx_jobs_v2_company_domain
  ON public.jobs_v2 (company_domain)
  WHERE company_domain IS NOT NULL;

-- Geospatial bounding-box prefilter (query() bbox path)
CREATE INDEX IF NOT EXISTS idx_jobs_v2_location_coords
  ON public.jobs_v2 (location_lat, location_lon)
  WHERE location_lat IS NOT NULL;

COMMENT ON TABLE public.jobs_v2 IS
  'Agnostic job shelf (Spec 11 Weg B). Parallel to jobs v1 during dual-write phase. '
  'Read-switch in app/config.py (JOBS_TABLE env/flag) selects active shelf. '
  'Cutover gate: every v1 (source, external_id) present in v2 AND 100-row title/url/company diff = 0. '
  'profile_id omitted (agnostik invariant US-7). '
  'State: first_seen_at set on insert, last_seen_at updated on each upsert, status active|expired.';
COMMENT ON COLUMN public.jobs_v2.content_hash IS
  'SHA fingerprint — non-unique; used for near-dup dedup_memory lookup and cutover compare. '
  'Upsert key is (source, external_id), not content_hash.';
COMMENT ON COLUMN public.jobs_v2.first_seen_at IS
  'Set once on INSERT; never overwritten by upsert DO UPDATE clause.';
COMMENT ON COLUMN public.jobs_v2.last_seen_at IS
  'Updated to now() on every upsert. Job not re-seen within expiry threshold → status=expired.';
