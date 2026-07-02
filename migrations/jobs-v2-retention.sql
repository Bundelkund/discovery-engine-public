-- jobs_v2 Retention via pg_cron (2026-07-02). Bounded-Active-Market-Strategie.
--
-- Why: jobs_v2 wuchs unbegrenzt (kein Retention-Mechanismus) — der Grund, warum der
-- 500-MB-Free-Tier ueberlief (greenhouse-Erstlauf-Spike 2026-07-02). Diese Retention
-- deckelt den Bestand auf den *aktiven Stellenmarkt*: Jobs, die seit >21 Tagen auf keinem
-- Board mehr gesehen wurden (last_seen_at), werden geloescht. Spiegelt die bestehenden
-- pg_cron-Jobs 'dedup-retention-10d' (03:12) und 'rawjobs-purge' (03:27).
--
-- Applied-Schutz: Jobs bei Firmen, mit denen Florian schon arbeitet (Eintrag in
-- public.applyskill_tracker, Status 'created'/'paused' — die real existierenden Werte,
-- NICHT 'applied/interview/offer'), werden NIE geloescht. Match ueber (company), weil
-- applyskill_tracker.job_id (FK -> alte public.jobs) fuer jobs_v2-IDs unzuverlaessig ist.
--
-- Retention lief die ATS-Cleanup-Rolle vorher nominell im n8n-Workflow
-- 'JobHunt - Data Retention Cleanup' (QU1twU1puj0YrxjQ) — der aber die alten v1-Tabellen
-- public.jobs/user_job_data putzte (nicht jobs_v2). Dieser Workflow wurde am 2026-07-02
-- deaktiviert; jobs_v2-Retention lebt jetzt an GENAU DIESER Stelle.
--
-- Fenster: 21 Tage (Stellschraube: 14 enger / 30 grosszuegiger). Taeglicher DELETE haelt
-- ein Plateau (autovacuum reclaimt reusable-space); der einmalige Reclaim + VACUUM FULL
-- lief separat am 2026-07-02 (276 -> 247 MB).
--
-- Apply via Supabase MCP (project guocdgjpbvsvcvchgolm). Idempotent: unschedule vor schedule.

SELECT cron.unschedule('jobsv2-retention-21d')
WHERE EXISTS (SELECT 1 FROM cron.job WHERE jobname = 'jobsv2-retention-21d');

SELECT cron.schedule(
  'jobsv2-retention-21d',
  '42 3 * * *',
  $$
    DELETE FROM public.jobs_v2 j
    WHERE j.last_seen_at < now() - interval '21 days'
      AND NOT EXISTS (
        SELECT 1 FROM public.applyskill_tracker t
        WHERE lower(t.company) = lower(j.company)
      )
  $$
);
