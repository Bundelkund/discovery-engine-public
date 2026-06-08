-- Persistent dedup memory index (Spec 11 Phase 3, Card ③ = A+D).
-- Date: 2026-06-08
-- SAFETY: additive-only — no DROP, RENAME, or ALTER TYPE statements.
-- Idempotent: CREATE TABLE/INDEX IF NOT EXISTS; safe to re-run.
--
-- Replaces the in-memory MinHashLSH (self._lsh) with a DB-backed lookup that
-- survives restarts and is consistent across parallel Refine runs.
--
-- Design: store each band hash produced by the MinHash banding technique.
-- MinHash with num_perm=128 and band_width=4 → 32 bands per document.
-- A pair is near-duplicate when ANY band_hash matches (same as LSH bucket hit).
-- Retention window (default 42 days) configured in config/data-quality.yaml.

CREATE TABLE IF NOT EXISTS public.dedup_memory (
  id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),

  -- Canonical job fingerprint (SHA-256 hex of normalised title+url+description)
  content_hash text        NOT NULL,

  -- MinHash band key: "band_{n}:{hash_of_band_n_shingles}"
  -- One row per band per document; near-dup detected on band_hash collision.
  band_hash    text        NOT NULL,

  -- Retention: entries older than dedup_window_days are purged by Refine pass
  seen_at      timestamptz NOT NULL DEFAULT now()
);

-- Near-dup lookup: find matching band → near-dup exists
CREATE INDEX IF NOT EXISTS idx_dedup_memory_band_hash
  ON public.dedup_memory (band_hash);

-- Exact-dup lookup: seen this content_hash before?
CREATE INDEX IF NOT EXISTS idx_dedup_memory_content_hash
  ON public.dedup_memory (content_hash);

-- Retention window: partial index on recent rows; used by purge query.
-- "recent" = seen_at > now() - interval; older rows are candidates for deletion.
-- The index accelerates the WHERE seen_at < $cutoff DELETE pass.
CREATE INDEX IF NOT EXISTS idx_dedup_memory_seen_at
  ON public.dedup_memory (seen_at DESC);

-- Uniqueness: one band row per (content_hash, band_hash) pair — safe for parallel upserts
CREATE UNIQUE INDEX IF NOT EXISTS idx_dedup_memory_hash_band_unique
  ON public.dedup_memory (content_hash, band_hash);

COMMENT ON TABLE public.dedup_memory IS
  'Persistent MinHash LSH band index for near-duplicate detection. '
  'One row per (document, band). Near-dup = any band_hash collision within retention window. '
  'Replaces in-memory MinHashLSH (self._lsh) — survives restarts, consistent across parallel runs. '
  'Retention window (default 42 days) in config/data-quality.yaml dedup.window_days. '
  'Purge pass: DELETE FROM dedup_memory WHERE seen_at < now() - interval.';
COMMENT ON COLUMN public.dedup_memory.band_hash IS
  'Format: "band_{n}:{hex_digest}" — band index n (0..31) + SHA hex of that band''s shingle hashes.';
