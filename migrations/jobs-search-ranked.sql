-- Ranked keyword search RPC for GET /jobs — closes recall blocker E1 + phrase bug E2.
-- Date: 2026-06-08
-- SAFETY: additive-only (CREATE OR REPLACE FUNCTION). No table/column changes.
--
-- PERF (why rank on title only): the filter (title @@ q OR description @@ q) uses the
-- two expression GIN indexes -> fast BitmapOr (~250ms). Ranking is the cost: an early
-- cut ranked ts_rank(to_tsvector('simple', title||description), q), which re-tokenizes
-- ~5k full DESCRIPTIONS per query -> 7-9s warm, near the 8s statement_timeout. Ranking
-- on title ONLY (ts_rank(to_tsvector('simple', title), q)) tokenizes only short titles
-- -> sub-second, and is MORE relevant: florians terms are job titles ('Agile Coach',
-- 'Scrum Master'), so a title hit outranks an incidental description mention. Rows that
-- match only in description rank rank=0 and fall back to scraped_at DESC — still
-- returned (recall unchanged), just lower. No stored tsvector column needed (a GENERATED
-- STORED column would rewrite the 10.5k-row table, exceeding the MCP apply timeout).
--
-- Problem (tenant live-test 2026-06-08): /jobs keyword search returned an UNRANKED
-- slice. The OR-match over 4 multi-word terms (incl. low-selectivity 'new work' ->
-- 'new' & 'work') matched thousands of rows; without ORDER BY ts_rank the selective
-- jobs (florians 86 Agile-Coach) sorted past position 500 -> never reached the scorer
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
-- pre-pagination COUNT(*) OVER()). Caller reads total from any row (0 rows -> 0).

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

  RETURN QUERY EXECUTE format($q$
    SELECT to_jsonb(t.*) AS result,
           count(*) OVER() AS total_count
    FROM %I AS t
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
    ORDER BY ts_rank(to_tsvector('simple', t.title), $1) DESC,
             t.scraped_at DESC
    LIMIT $11 OFFSET $12
  $q$, p_table)
  USING v_tsq, p_keywords_negative, p_location, p_max_age_days,
        p_exclude_domain, p_source, p_company_domain, p_seniority_terms,
        p_min_salary, p_max_salary, p_limit, p_offset;
END;
$$;

COMMENT ON FUNCTION public.search_jobs_ranked IS
  'Ranked keyword search for GET /jobs (E1/E2 fix, 2026-06-08). ORDER BY ts_rank DESC '
  'so selective terms outrank high-frequency token floods (new/work/senior). Builds '
  'tsquery server-side (websearch_to_tsquery, phrase-aware). p_table allowlisted to '
  'jobs|jobs_v2. Filter parity with JobRepository.query() except max_distance_km '
  '(caller falls back to PostgREST when distance+keywords combine). Returns '
  '(result jsonb, total_count bigint = COUNT(*) OVER()).';
