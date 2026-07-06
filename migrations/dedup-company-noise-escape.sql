-- dedup-company-noise-escape.sql
-- Backfill for the dedup-company-noise-escape task (P1).
--
-- The canonical content_hash formula changed (app/services/refine_pipeline.py):
--   OLD: sha256(norm(title)|norm_company(company))
--   NEW: sha256(stem(title,company)|norm_company(company)) where
--        - norm_company treats the adzuna apply-page label ("Bewerbung als",
--          "Bewerbung als <role>") as an EMPTY company (a CTA, not an employer);
--        - stem strips a trailing company echo from the normalised title
--          ("Title - Capco" with company="Capco" -> "Title").
--   Deliberately NOT in the hash: trailing-truncation tolerance — a per-row hash
--   can only absorb truncation by capping the stem, which over-merges genuinely
--   different postings (observed: 50 per-city "…- 1KOMMA5° <Stadt>" rows differ
--   only after char ~48). Truncation is handled as a COMPARISON: at ingest by
--   DeduplicationService Tier 3b, on the existing shelf by step (3) below.
--
-- Observed escape (prod, 2026-07): ONE Capco posting as FOUR jobs_v2 rows
--   17e26877  title "...Asset Managemen"           company "Bewerbung als"
--   76a19d40  title "...Asset Management"          company "Bewerbung als"
--   4d8e19d2  title "...Asset Management - Capco"  company "Capco"
--   8e7b1b57  title "...Asset Managemen - Capco"   company "Capco"
-- Adzuna cuts raw titles at exactly 64 chars, so len(echo-stripped title) == 64
-- is the truncation signature; the apply-label company normalises to ''.
--
-- Steps:
--   (1) recompute content_hash on the whole shelf with the new formula
--       (MUST stay byte-identical to refine_pipeline._content_hash — verified
--       2026-07-06 against prod: rows above -> 0e33d33ec7ec5423 / 7d5c178dbbe01389
--       identical in Python and SQL);
--   (2) collapse hash-duplicates (keep richest description, newest, id);
--   (3) collapse truncation/garbage-company clusters: group rows whose
--       echo-stripped RAW title is >= 64 chars by its first 64 chars; within a
--       cluster keep the best row (untruncated + real company first), delete
--       only members that are truncation suspects (len == 64) or company-less
--       AND company-compatible with the keeper. Untruncated real-company
--       members (per-city postings) are never deleted.
--
-- Impact (read-only simulation against prod, 2026-07-06, 28987 rows):
--   step (2) deletes 517 rows — 516 of them are latent duplicates ALREADY under
--   the OLD formula (shelf drift accumulated since the 2026-06-09 backfill; the
--   new formula itself adds exactly 1). step (3) deletes 9 rows: the 3 Capco
--   dupes (survivor 4d8e19d2), 4 company-less aggregator variants with a
--   real-company sibling (careerjet/jooble), 2 truncated same-company variants
--   (IU/ELFIN). 28987 -> 28461 rows.
--
-- Idempotent: re-running recomputes the same hashes and finds 0 further dupes.
-- NOT applied automatically — destructive DELETE on the shared prod shelf.
-- Review + apply manually; run VACUUM (ANALYZE) afterwards (see bottom).

-- digest() needs pgcrypto (already present in this project).
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- (1) Recompute canonical hash: stem(title,company)|norm_company(company).
UPDATE public.jobs_v2 j SET content_hash = (
  SELECT left(encode(digest(
    CASE WHEN n.comp <> '' AND n.stem0 LIKE '% ' || n.comp
         THEN left(n.stem0, length(n.stem0) - length(n.comp) - 1)
         ELSE n.stem0 END
    || '|' || n.comp
  , 'sha256'), 'hex'), 16)
  FROM (
    SELECT
      trim(regexp_replace(regexp_replace(lower(coalesce(j.title,'')),'\(.*?\)',' ','g'),'[^a-z0-9]+',' ','g')) AS stem0,
      CASE WHEN trim(regexp_replace(regexp_replace(lower(coalesce(j.company,'')),'\(.*?\)',' ','g'),'[^a-z0-9]+',' ','g')) = 'bewerbung als'
             OR trim(regexp_replace(regexp_replace(lower(coalesce(j.company,'')),'\(.*?\)',' ','g'),'[^a-z0-9]+',' ','g')) LIKE 'bewerbung als %'
           THEN ''
           ELSE trim(regexp_replace(regexp_replace(
                  regexp_replace(regexp_replace(lower(coalesce(j.company,'')),'\(.*?\)',' ','g'),'[^a-z0-9]+',' ','g'),
                  '\y(gmbh|mbh|ag|se|kg|kgaa|ug|ohg|gbr|co|ev|ltd|llc|inc|plc|holding)\y',' ','g'),
                  '\s+',' ','g'))
      END AS comp
  ) n
);

-- (2) Collapse hash-duplicates: keep the richest description, then newest, then id.
DELETE FROM public.jobs_v2 a USING (
  SELECT id, row_number() OVER (
    PARTITION BY content_hash
    ORDER BY length(coalesce(description,'')) DESC, scraped_at DESC NULLS LAST, id
  ) AS rn
  FROM public.jobs_v2
) r
WHERE a.id = r.id AND r.rn > 1;

-- (3) Truncation/garbage-company cluster collapse (mirrors dedup Tier 3b).
WITH prep AS (
  SELECT j.id, j.scraped_at,
         length(coalesce(j.description,'')) AS dlen,
         -- echo-stripped RAW title: "Title - Capco" -> "Title" (regexp-escaped company)
         CASE WHEN coalesce(j.company,'') <> ''
              THEN regexp_replace(
                     j.title,
                     '\s*[-–—|:]\s*' ||
                     regexp_replace(j.company, '([\^$.|?*+()\[\]{}\\])', '\\\1', 'g') ||
                     '\s*$', '', 'i')
              ELSE j.title END AS stripped,
         CASE WHEN trim(regexp_replace(regexp_replace(lower(coalesce(j.company,'')),'\(.*?\)',' ','g'),'[^a-z0-9]+',' ','g')) = 'bewerbung als'
                OR trim(regexp_replace(regexp_replace(lower(coalesce(j.company,'')),'\(.*?\)',' ','g'),'[^a-z0-9]+',' ','g')) LIKE 'bewerbung als %'
              THEN ''
              ELSE trim(regexp_replace(regexp_replace(
                     regexp_replace(regexp_replace(lower(coalesce(j.company,'')),'\(.*?\)',' ','g'),'[^a-z0-9]+',' ','g'),
                     '\y(gmbh|mbh|ag|se|kg|kgaa|ug|ohg|gbr|co|ev|ltd|llc|inc|plc|holding)\y',' ','g'),
                     '\s+',' ','g'))
         END AS comp
  FROM public.jobs_v2 j
),
clusters AS (
  SELECT id, comp,
         (length(stripped) = 64 OR comp = '') AS eligible,
         first_value(comp) OVER w AS keeper_comp,
         row_number()      OVER w AS rn
  FROM prep
  WHERE length(stripped) >= 64
  WINDOW w AS (
    PARTITION BY left(stripped, 64)
    ORDER BY (length(stripped) = 64 OR comp = '') ASC,  -- keeper: untruncated + real company first
             length(stripped) DESC, dlen DESC, scraped_at DESC NULLS LAST, id
    ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
  )
)
DELETE FROM public.jobs_v2 WHERE id IN (
  SELECT id FROM clusters
  WHERE rn > 1
    AND eligible                                   -- never delete untruncated real-company rows
    AND (comp = '' OR comp = keeper_comp)          -- company guard: no cross-company merge
);

-- (4) MANDATORY after the mass UPDATE+DELETE: refresh planner stats + reclaim dead
-- tuples (same lesson as cross-source-dedup-content-hash.sql, observed 2026-06-09:
-- stale plan tripped statement_timeout on GET /jobs until VACUUM ANALYZE ran).
-- (Cannot run inside the migration transaction — execute separately right after:)
--   VACUUM (ANALYZE) public.jobs_v2;
