-- Bundle B: Additive Schema Migration
-- Project: discovery-engine (REDACTED)
-- Date: 2026-04-22
-- SAFETY: additive-only — no DROP, RENAME, or ALTER TYPE statements.
-- Protected: jobs table (shared with JobHunt — additive-only per AD-8).

-- ---------------------------------------------------------------------------
-- New columns
-- ---------------------------------------------------------------------------

ALTER TABLE jobs
  ADD COLUMN IF NOT EXISTS location_normalized text NULL,
  ADD COLUMN IF NOT EXISTS location_lat        double precision NULL,
  ADD COLUMN IF NOT EXISTS location_lon        double precision NULL,
  ADD COLUMN IF NOT EXISTS is_remote           boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS is_hybrid           boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS dq_flags            jsonb   NOT NULL DEFAULT '{}'::jsonb;

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_jobs_location_normalized
  ON jobs(location_normalized)
  WHERE location_normalized IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_jobs_is_remote
  ON jobs(is_remote)
  WHERE is_remote = true;

CREATE INDEX IF NOT EXISTS idx_jobs_location_coords
  ON jobs(location_lat, location_lon)
  WHERE location_lat IS NOT NULL;
