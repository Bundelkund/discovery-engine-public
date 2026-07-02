-- Ranked keyword search RPC for GET /jobs — closes recall blocker E1 + phrase bug E2.
-- Date: 2026-06-08 · Perf restructure: 2026-07-01
-- SAFETY: additive-only (CREATE OR REPLACE FUNCTION). No table/column changes.
--
-- PERF (two-phase, why the wide-window backfill used to 500): the filter
-- (title @@ q OR description @@ q) uses the two expression GIN indexes -> fast BitmapOr.
-- The cost was the SORT: a broad 40-term OR over jobs_v2 (60k+) matches ~39k rows
-- (mostly via DESCRIPTION — single words like 'coach'/'innovation'/'transformation'
-- appear in tens of thousands of descriptions), and ORDER BY ts_rank(title, q) then
-- re-tokenizes to_tsvector(title) for ALL ~39k rows before LIMIT -> ~74s, far past the
-- statement_timeout. florians terms ARE job titles, so only TITLE hits (~1.4k) are
-- relevant; description-only hits rank 0 and are pure tie-break-by-recency tail.
-- FIX: rank ONLY the title-hit set (small), and append description-only hits UNRANKED,
-- ordered by recency and capped to what the page can consume ($limit+$offset). The
-- description branch excludes title hits via an ANTI-JOIN against title_hits (~1.4k ids,
-- hash probe) — NOT `NOT (to_tsvector(title) @@ q)`, which re-tokenized 40k titles and
-- alone cost 3s. Same rows, same order (title hits rank>0 always precede rank-0 desc
-- hits), same total_count (full title-OR-desc match set) — just ~74s -> ~0.3s. Measured
-- 2026-07-01 prod: 40 terms, max_age_days=30, limit=500 -> 0.32s (offset 0).
--
-- Problem (tenant live-test 2026-06-08): /jobs keyword search returned an UNRANKED
-- slice. The OR-match over multi-word terms (incl. low-selectivity 'new work' ->
-- 'new' & 'work') matched thousands of rows; without ORDER BY ts_rank the selective
-- jobs (florians Agile-Coach roles) sorted past position 500 -> never reached the scorer
-- (matches_upserted=0 even at limit=500). PostgREST `.order()` cannot ORDER BY a
-- ts_rank() expression, so the ranked path lives here as an RPC.
--
-- E2 fix bundled: this function builds the tsquery server-side via
-- websearch_to_tsquery, so phrase quoting ("New Work" -> new <-> work) is honored
-- — the PostgREST `wfts` path silently stripped quotes.
--
-- Generic over the shelf: p_table in ('jobs','jobs_v2') (allowlisted; jobs_v2 is the
-- post-cutover target — same function serves it, no second migration needed).
--
-- Filter parity with JobRepository.query(): keywords_positive (ranked tsquery OR),
-- keywords_negative (substring ILIKE NOT, AND-combined), location, max_age_days,
-- exclude_domain, source, company_domain, seniority (pre-expanded term list from
-- Python), min/max_salary. DEFERRED: max_distance_km/bbox (caller keeps the legacy
-- PostgREST path when a distance filter is combined with keywords).
--
-- Returns one row per match: (result jsonb = full job row, total_count bigint =
-- pre-pagination count over the full title-OR-desc match set). Caller reads total from
-- any row (0 rows -> 0).

CREATE OR REPLACE FUNCTION public.search_jobs_ranked(
  p_table              text,
  p_keywords_positive  text[],
  p_keywords_negative  text[]   DEFAULT NULL,
  p_location           text     DEFAULT NULL,
  p_max_age_days       integer  DEFAULT NULL,
  p_exclude_domain     text[]   DEFAULT NULL,
  p_source             text[]   DEFAULT NULL,
  p_company_domain     text[]   DEFAULT NULL,
  p_seniority_terms    text[]   DEFAULT NULL,
  p_min_salary         integer  DEFAULT NULL,
  p_max_salary         integer  DEFAULT NULL,
  p_limit              integer  DEFAULT 50,
  p_offset             integer  DEFAULT 0
)
RETURNS TABLE (result jsonb, total_count bigint)
LANGUAGE plpgsql
STABLE
SET search_path = public, pg_temp
AS $$
DECLARE
  v_tsq  tsquery;
  v_term text;
BEGIN
  -- Allowlist the shelf (prevents arbitrary-table injection via %I).
  IF p_table IS NULL OR p_table NOT IN ('jobs', 'jobs_v2') THEN
    RAISE EXCEPTION 'search_jobs_ranked: p_table must be ''jobs'' or ''jobs_v2'', got %', p_table;
  END IF;

  -- Build the positive tsquery: each term OR'd (||). websearch_to_tsquery makes the
  -- words WITHIN a term AND ('agile coach' -> agile & coach) and honors "..." phrases.
  IF p_keywords_positive IS NOT NULL THEN
    FOREACH v_term IN ARRAY p_keywords_positive LOOP
      IF length(trim(v_term)) > 0 THEN
        IF v_tsq IS NULL THEN
          v_tsq := websearch_to_tsquery('simple', v_term);
        ELSE
          v_tsq := v_tsq || websearch_to_tsquery('simple', v_term);
        END IF;
      END IF;
    END LOOP;
  END IF;

  -- No usable positive terms -> empty result (caller only invokes this with terms).
  IF v_tsq IS NULL THEN
    RETURN;
  END IF;

  -- Two-phase ranked page (see PERF header). The shared non-FTS filter block is repeated
  -- verbatim across title_hits / desc_hits / cnt so all three see the exact same rows;
  -- only the FTS predicate differs. %1$I = p_table (allowlisted above). $1 = v_tsq.
  RETURN QUERY EXECUTE format($q$
    WITH title_hits AS (
      SELECT t.id,
             ts_rank(to_tsvector('simple', t.title), $1) AS r,
             t.scraped_at
      FROM %1$I AS t
      WHERE to_tsvector('simple', t.title) @@ $1
        AND ($2 IS NULL OR NOT EXISTS (
              SELECT 1 FROM unnest($2) AS n
              WHERE t.title ILIKE '%%' || n || '%%'
                 OR t.description ILIKE '%%' || n || '%%'))
        AND ($3 IS NULL OR t.location ILIKE '%%' || $3 || '%%')
        AND ($4 IS NULL OR t.scraped_at >= now() - ($4 || ' days')::interval)
        AND ($5 IS NULL OR t.company_domain IS NULL OR NOT (t.company_domain = ANY($5)))
        AND ($6 IS NULL OR t.source = ANY($6))
        AND ($7 IS NULL OR t.company_domain = ANY($7))
        AND ($8 IS NULL OR EXISTS (
              SELECT 1 FROM unnest($8) AS s WHERE t.title ILIKE '%%' || s || '%%'))
        AND ($9 IS NULL OR (t.salary_min IS NOT NULL AND t.salary_min >= $9))
        AND ($10 IS NULL OR (t.salary_max IS NOT NULL AND t.salary_max <= $10))
    ),
    desc_hits AS (
      SELECT t.id,
             0::real AS r,
             t.scraped_at
      FROM %1$I AS t
      WHERE to_tsvector('simple', t.description) @@ $1
        -- Exclude rows already in title_hits via anti-join against its ~1.4k ids
        -- (hash probe), NOT `NOT (to_tsvector(title) @@ $1)` which re-tokenized the
        -- title of every one of the ~40k description hits -> was 3s of the 4.8s total.
        AND NOT EXISTS (SELECT 1 FROM title_hits th WHERE th.id = t.id)
        AND ($2 IS NULL OR NOT EXISTS (
              SELECT 1 FROM unnest($2) AS n
              WHERE t.title ILIKE '%%' || n || '%%'
                 OR t.description ILIKE '%%' || n || '%%'))
        AND ($3 IS NULL OR t.location ILIKE '%%' || $3 || '%%')
        AND ($4 IS NULL OR t.scraped_at >= now() - ($4 || ' days')::interval)
        AND ($5 IS NULL OR t.company_domain IS NULL OR NOT (t.company_domain = ANY($5)))
        AND ($6 IS NULL OR t.source = ANY($6))
        AND ($7 IS NULL OR t.company_domain = ANY($7))
        AND ($8 IS NULL OR EXISTS (
              SELECT 1 FROM unnest($8) AS s WHERE t.title ILIKE '%%' || s || '%%'))
        AND ($9 IS NULL OR (t.salary_min IS NOT NULL AND t.salary_min >= $9))
        AND ($10 IS NULL OR (t.salary_max IS NOT NULL AND t.salary_max <= $10))
      -- Only the newest ($offset+$limit) desc-only rows can surface on this page; the
      -- rest never reach the LIMIT window. Capping here is what keeps the sort cheap.
      ORDER BY t.scraped_at DESC
      LIMIT (COALESCE($11, 50) + COALESCE($12, 0))
    ),
    cnt AS (
      -- Full pre-pagination count over the SAME match set the original used
      -- (title OR desc, all filters). Cheap: bare GIN bitmap count, no ranking.
      SELECT count(*) AS total FROM %1$I AS t
      WHERE (to_tsvector('simple', t.title) @@ $1
             OR to_tsvector('simple', t.description) @@ $1)
        AND ($2 IS NULL OR NOT EXISTS (
              SELECT 1 FROM unnest($2) AS n
              WHERE t.title ILIKE '%%' || n || '%%'
                 OR t.description ILIKE '%%' || n || '%%'))
        AND ($3 IS NULL OR t.location ILIKE '%%' || $3 || '%%')
        AND ($4 IS NULL OR t.scraped_at >= now() - ($4 || ' days')::interval)
        AND ($5 IS NULL OR t.company_domain IS NULL OR NOT (t.company_domain = ANY($5)))
        AND ($6 IS NULL OR t.source = ANY($6))
        AND ($7 IS NULL OR t.company_domain = ANY($7))
        AND ($8 IS NULL OR EXISTS (
              SELECT 1 FROM unnest($8) AS s WHERE t.title ILIKE '%%' || s || '%%'))
        AND ($9 IS NULL OR (t.salary_min IS NOT NULL AND t.salary_min >= $9))
        AND ($10 IS NULL OR (t.salary_max IS NOT NULL AND t.salary_max <= $10))
    ),
    page AS (
      SELECT id, r, scraped_at FROM title_hits
      UNION ALL
      SELECT id, r, scraped_at FROM desc_hits
      ORDER BY r DESC, scraped_at DESC
      LIMIT $11 OFFSET $12
    )
    SELECT to_jsonb(t.*) AS result,
           (SELECT total FROM cnt) AS total_count
    FROM page AS p
    JOIN %1$I AS t ON t.id = p.id
    ORDER BY p.r DESC, p.scraped_at DESC
  $q$, p_table)
  USING v_tsq, p_keywords_negative, p_location, p_max_age_days,
        p_exclude_domain, p_source, p_company_domain, p_seniority_terms,
        p_min_salary, p_max_salary, p_limit, p_offset;
END;
$$;

COMMENT ON FUNCTION public.search_jobs_ranked IS
  'Ranked keyword search for GET /jobs (E1/E2 fix 2026-06-08; two-phase perf restructure '
  '2026-07-01). Ranks only TITLE hits (small set), appends description-only hits unranked '
  'by recency (capped to offset+limit) -> wide-window backlog query 74s->4s. Same rows, '
  'order, and total_count (full title-OR-desc match count) as the pre-restructure version. '
  'Builds tsquery server-side (websearch_to_tsquery, phrase-aware). p_table allowlisted to '
  'jobs|jobs_v2. Filter parity with JobRepository.query() except max_distance_km. '
  'Returns (result jsonb, total_count bigint).';
