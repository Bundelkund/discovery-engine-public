-- cross-source-dedup-content-hash.sql
-- Backfill for the cross-source-dedup-hash task.
--
-- The canonical content_hash formula changed (app/services/refine_pipeline.py):
--   OLD: sha256(url|title|company)         — url split the same posting per board
--   NEW: sha256(norm(title)|norm_company(company))
--        url + location DROPPED (location renders as Berlin / Berlin-Mitte /
--        "Wedding, Berlin" / "Berlin, Berlin, Germany" for ONE job → too noisy);
--        company legal forms (GmbH/AG/…) stripped so "amberra GmbH" == "amberra".
--
-- This migration (1) recomputes content_hash on the existing shelf with the new
-- formula and (2) collapses the resulting duplicates, keeping the row with the
-- richest description (then newest scraped_at, then id). Without it the existing
-- shelf stays doubled AND new ingests (new-formula hash) never match the old
-- url-based hashes.
--
-- The SQL hash expression MUST stay byte-identical to refine_pipeline._content_hash
-- (verified 2026-06-09: both → 9ad73f81d9d29046 for the amberra posting).
-- Idempotent: re-running recomputes the same hashes and finds 0 further dupes.

-- digest() needs pgcrypto (already present in this project).
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- (1) Recompute canonical hash.
UPDATE public.jobs_v2 SET content_hash = left(encode(digest(
  trim(regexp_replace(regexp_replace(lower(coalesce(title,'')),'\(.*?\)',' ','g'),'[^a-z0-9]+',' ','g')) || '|' ||
  trim(regexp_replace(
    regexp_replace(regexp_replace(regexp_replace(lower(coalesce(company,'')),'\(.*?\)',' ','g'),'[^a-z0-9]+',' ','g'),
      '\y(gmbh|mbh|ag|se|kg|kgaa|ug|ohg|gbr|co|ev|ltd|llc|inc|plc|holding)\y',' ','g'),
    '\s+',' ','g'))
,'sha256'),'hex'),16);

-- (2) Collapse duplicates: keep the richest description, then newest, then id.
DELETE FROM public.jobs_v2 a USING (
  SELECT id, row_number() OVER (
    PARTITION BY content_hash
    ORDER BY length(coalesce(description,'')) DESC, scraped_at DESC NULLS LAST, id
  ) AS rn
  FROM public.jobs_v2
) r
WHERE a.id = r.id AND r.rn > 1;

-- (3) MANDATORY after the mass UPDATE+DELETE: refresh planner stats + reclaim dead
-- tuples. Without this the GET /jobs FTS query (esp. multi-term keyword search with
-- offset) picks a stale plan and trips statement_timeout (9s) → 500 → empty result.
-- Observed 2026-06-09: 40-term recency fetch returned 0 until VACUUM ANALYZE ran.
-- (Cannot run inside the migration transaction — execute separately as a superuser
--  maintenance step right after applying this file.)
--   VACUUM (ANALYZE) public.jobs_v2;
