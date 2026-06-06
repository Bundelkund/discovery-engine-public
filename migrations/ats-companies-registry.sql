-- ATS Companies Registry (no-delete). Applied 2026-06-06 via supabase-MCP
-- (project guocdgjpbvsvcvchgolm, migration name: ats_companies_registry).
-- This file is the repo-of-record copy. See docs/ats-pipeline.md + tasks.yaml (registry-table).
--
-- CC-fed multi-ATS company registry, progressively enriched, periodic CC-recheck.
-- Convention mirrors cr_companies: RLS off (server-only, service-role), check-constrained.
CREATE TABLE IF NOT EXISTS public.ats_companies (
  ats               text NOT NULL,
  slug              text NOT NULL,
  company_name      text,
  careers_url       text,
  feed_url          text,
  source            text NOT NULL DEFAULT 'cc'
                      CHECK (source = ANY (ARRAY['cc','scrape','manual'])),
  seen_in_crawls    text[] NOT NULL DEFAULT '{}',
  status            text NOT NULL DEFAULT 'active'
                      CHECK (status = ANY (ARRAY['active','paused','dead'])),
  monitor           boolean NOT NULL DEFAULT true,
  domain            text,                       -- soft link -> company_profiles.domain (no FK; no-delete decoupled)
  initial_job_count integer,                    -- set once on first insert
  last_job_count    integer,
  de_flag           text CHECK (de_flag IS NULL OR de_flag = ANY (ARRAY['de','remote','foreign'])),
  location_signal   text,                       -- raw provider location blob, for audit
  sample_titles     text[] NOT NULL DEFAULT '{}',
  notes             text,
  discovered_at     timestamptz NOT NULL DEFAULT now(),
  last_checked_at   timestamptz,
  enriched_at       timestamptz,
  PRIMARY KEY (ats, slug)
);

ALTER TABLE public.ats_companies DISABLE ROW LEVEL SECURITY;

-- monitor-scan hot path: which active+monitored boards per ATS to re-probe
CREATE INDEX IF NOT EXISTS ats_companies_monitor_idx
  ON public.ats_companies (ats, status) WHERE monitor;
-- DE-filter (keep-for-DE = de ∪ remote)
CREATE INDEX IF NOT EXISTS ats_companies_de_flag_idx
  ON public.ats_companies (de_flag);
-- enrichment join target
CREATE INDEX IF NOT EXISTS ats_companies_domain_idx
  ON public.ats_companies (domain) WHERE domain IS NOT NULL;

COMMENT ON TABLE public.ats_companies IS
  'NO-DELETE registry of every company discovered on slug-based ATS via Common Crawl. '
  'Key (ats, slug). Fed by scripts/ats_scanner.py: Stage A discover (monthly CDX) inserts new slugs; '
  'Stage B refresh (daily --revalidate) updates status/last_job_count/sample_titles/de_flag in one fetch. '
  'Dead feed -> status=''dead'' (row kept, never deleted). keep-for-DE = de_flag IN (de,remote). '
  'source=cc (CDX-enumerated) | scrape (downstream apply-link, e.g. lever) | manual (curated yaml).';
COMMENT ON COLUMN public.ats_companies.de_flag IS
  'DE relevance from validate pass: de (>=1 DE job) | remote (countryless-remote, bias-include) | foreign | NULL (no location signal, e.g. factorial sitemap).';
COMMENT ON COLUMN public.ats_companies.initial_job_count IS 'Job count at first insert; never overwritten (last_job_count tracks current).';
