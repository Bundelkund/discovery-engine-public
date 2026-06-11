-- Drop redundant URL columns from ats_companies. See docs/adr/sources-dimension.md.
-- feed_url: fully populated but NEVER read from the DB by app/scripts (source modules read
--   yaml config; sheets mirror reads enumeration JSON; scanner renders prov["feed"].format).
--   Now derivable from sources.feed_url_template -> the DB copy is dead. seed.row_from no
--   longer writes it (same commit).
-- careers_url: 100% empty (0/8467), never written by seed.
-- Applied via supabase apply_migration (project guocdgjpbvsvcvchgolm). Repo-of-record copy.

ALTER TABLE public.ats_companies DROP COLUMN feed_url;
ALTER TABLE public.ats_companies DROP COLUMN careers_url;
