-- Store-first raw job landing table (Spec 11 Phase 1, Card ①②).
-- Date: 2026-06-08
-- SAFETY: additive-only — no DROP, RENAME, or ALTER TYPE statements.
-- Idempotent: CREATE TABLE/INDEX IF NOT EXISTS; safe to re-run.
--
-- Holds every scraped job unmodified before the Refine pass processes it.
-- Nine normalised surface fields + raw_data (full source response) + housekeeping.
-- salary kept as raw text here; Refine splits to salary_min/salary_max on jobs-v2.

CREATE TABLE IF NOT EXISTS public.raw_jobs (
  id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),

  -- Nine normalised surface fields (align with NormalizedJob)
  title        text,
  url          text,
  company      text,
  location     text,
  description  text,
  salary       text,           -- raw string from source; Refine parses to min/max
  source       text,
  external_id  text,
  posted_at    timestamptz,

  -- Full original source response for lossless re-extraction
  raw_data     jsonb        NOT NULL DEFAULT '{}'::jsonb,

  -- Dedup fingerprint (same algorithm as jobs.content_hash)
  content_hash text,

  -- Housekeeping
  ingested_at  timestamptz  NOT NULL DEFAULT now(),

  -- State machine: Refine reads 'new', sets terminal state
  status       text         NOT NULL DEFAULT 'new'
                 CHECK (status = ANY (ARRAY['new','refined','rejected','duplicate']))
);

-- Refine hot path: reads WHERE status = 'new'
CREATE INDEX IF NOT EXISTS idx_raw_jobs_status
  ON public.raw_jobs (status)
  WHERE status = 'new';

-- Dedup lookup by content_hash
CREATE INDEX IF NOT EXISTS idx_raw_jobs_content_hash
  ON public.raw_jobs (content_hash)
  WHERE content_hash IS NOT NULL;

-- Source-level dedup and re-ingestion guard
CREATE INDEX IF NOT EXISTS idx_raw_jobs_source_external_id
  ON public.raw_jobs (source, external_id)
  WHERE external_id IS NOT NULL;

COMMENT ON TABLE public.raw_jobs IS
  'Store-first landing table: every scraped job row as received, before Refine processing. '
  'status new→refined|rejected|duplicate via Refine pipeline. '
  'raw_data holds full source response for lossless field re-extraction. '
  'salary is the raw source string; Refine splits to salary_min/salary_max on jobs-v2.';
