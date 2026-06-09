-- Drop jobs_v2.score_stage_1 — the engine is profile-agnostic (Slice 2b, Phase 2).
--
-- Per-profile scoring lives in the tenant module (tenant matching.py). The refine
-- pipeline no longer computes or stores a score, and Commit C stops writing this
-- column. It is now dead. Additive-safe + idempotent.
--
-- ORDERING (SAFETY-CRITICAL): apply this ONLY after the Commit C build is LIVE on
-- Coolify. Pre-Commit-C code still sends "score_stage_1" in the refine upsert payload;
-- dropping the column before that build is live makes every refine upsert fail
-- (PostgREST: column not found). Sequence: push Commit C -> redeploy -> THEN this DDL.
--
-- Consumer-safe (verified 2026-06-09): no consumer selects jobs_v2.score_stage_1.
--   - wonderapply reads v1 `public.jobs` with explicit column lists that never name it
--   - tenant Job.from_engine reads only id/title/company/location/description/url
--   - the /jobs API response model dropped score_stage_1 + final_score (Commit A)
--
-- v1 `public.jobs` is intentionally LEFT UNTOUCHED (frozen, kept for rollback).

ALTER TABLE public.jobs_v2 DROP COLUMN IF EXISTS score_stage_1;
