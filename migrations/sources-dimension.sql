-- sources dimension (canonical provider registry). See docs/adr/sources-dimension.md.
-- Metadata-bearing lookup (type + URL templates), NOT storage-dedup. FK target for
-- ats_companies.source only (Kimball: hot fact tables jobs/jobs_v2/raw_jobs stay text).
-- Applied via supabase apply_migration (project guocdgjpbvsvcvchgolm). Repo-of-record copy.

CREATE TABLE IF NOT EXISTS public.sources (
  code              text PRIMARY KEY,
  label             text NOT NULL,
  type              text NOT NULL CHECK (type IN ('ats','aggregator','feed','internal')),
  base_url_template text,                         -- human board URL, {slug}-templated
  feed_url_template text,                         -- machine feed URL, {slug}-templated
  is_active         boolean NOT NULL DEFAULT true,
  notes             text
);

ALTER TABLE public.sources DISABLE ROW LEVEL SECURITY;  -- server-only, mirrors ats_companies

COMMENT ON TABLE public.sources IS
  'Canonical provider registry. type: ats (per-slug company feed) | aggregator (keyword search API) '
  '| feed (generic RSS) | internal (own discovery). FK target for ats_companies.source; jobs/jobs_v2/'
  'raw_jobs.source stay denormalized text (Kimball, no per-insert FK lock). See docs/adr/sources-dimension.md.';

INSERT INTO public.sources (code, label, type, base_url_template, feed_url_template) VALUES
  ('greenhouse', 'Greenhouse', 'ats', 'https://boards.greenhouse.io/{slug}',        'https://boards-api.greenhouse.io/v1/boards/{slug}/jobs'),
  ('ashby',      'Ashby',      'ats', 'https://jobs.ashbyhq.com/{slug}',            'https://api.ashbyhq.com/posting-api/job-board/{slug}'),
  ('lever',      'Lever',      'ats', 'https://jobs.lever.co/{slug}',               'https://api.lever.co/v0/postings/{slug}?mode=json'),
  ('personio',   'Personio',   'ats', 'https://{slug}.jobs.personio.de',            'https://{slug}.jobs.personio.de/xml'),
  ('recruitee',  'Recruitee',  'ats', 'https://{slug}.recruitee.com',               'https://{slug}.recruitee.com/api/offers/'),
  ('breezy',     'Breezy',     'ats', 'https://{slug}.breezy.hr',                   'https://{slug}.breezy.hr/json'),
  ('factorial',  'Factorial',  'ats', 'https://{slug}.factorialhr.com',             'https://{slug}.factorialhr.com/sitemap.xml'),
  ('softgarden', 'Softgarden', 'ats', 'https://{slug}.career.softgarden.de',        'https://{slug}.career.softgarden.de/jobs.json'),
  ('indeed',         'Indeed',                    'aggregator', NULL, NULL),
  ('adzuna',         'Adzuna',                    'aggregator', NULL, NULL),
  ('linkedin',       'LinkedIn',                  'aggregator', NULL, NULL),
  ('careerjet',      'Careerjet',                 'aggregator', NULL, NULL),
  ('jooble',         'Jooble',                    'aggregator', NULL, NULL),
  ('arbeitsagentur', 'Bundesagentur fuer Arbeit', 'aggregator', NULL, NULL),
  ('rss',                   'Generic RSS',             'feed', NULL, NULL),
  ('rss_berlinstartupjobs', 'Berlin Startup Jobs RSS', 'feed', NULL, NULL),
  ('themuse',               'The Muse',                'feed', NULL, NULL),
  ('company_radar', 'Company Radar (internal)', 'internal', NULL, NULL)
ON CONFLICT (code) DO NOTHING;
