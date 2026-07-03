-- jobs_v2 Lifecycle: 'expired'-Markierung via pg_cron (2026-07-03). Ergänzt die
-- 21-Tage-Retention (jobs-v2-retention.sql) um einen weicheren Zwischenschritt.
--
-- Why: Alle 28.135 jobs_v2-Zeilen standen dauerhaft auf status='active' — die
-- mark_expired()-Methode (app/repositories/jobs.py) existierte, wurde aber nie
-- aufgerufen. Consumer konnten einen heute gesehenen Job nicht von einem seit Wochen
-- nicht mehr gesehenen unterscheiden. Diese Migration verdrahtet den Übergang als
-- pg_cron-SQL (analog zur bestehenden Retention), ohne App-Deploy.
--
-- Semantik: expired = "seit >14 Tagen auf keinem Board mehr gesehen" (last_seen_at).
-- Selbstkorrigierend: der Upsert (jobs.py) setzt bei Wiedersichtung last_seen_at=now()
-- UND status='active' zurück — ein wieder auftauchender Job wird automatisch reaktiviert.
--
-- Fenster: 14 Tage — bewusst < 21d-Delete, sodass ein 14–21d "stale"-Band entsteht,
-- das expired markiert, aber noch auf dem Shelf sichtbar ist, bevor es gelöscht wird.
--
-- KEIN applyskill_tracker-Schutz hier: der Schutz verhindert nur das LÖSCHEN. Ein
-- beworbener, aber seit 14 Tagen nicht gesehener Job IST stale → korrekt expired; er
-- bleibt durch die Retention trotzdem vor dem Delete geschützt (Datensatz bleibt).
--
-- Consumer-Gate geprüft (2026-07-03): der Read-/Query-Pfad (GET /jobs, repositories.query)
-- filtert NICHT nach status → expired schrumpft den Consumer-/Match-Feed nicht. Reine,
-- additive Annotation.
--
-- Läuft 03:37 UTC, 5 Min VOR dem 21d-Delete (03:42), damit Markierung und Löschung
-- konsistent aufeinander folgen.
--
-- Apply via Supabase MCP (project guocdgjpbvsvcvchgolm). Idempotent: unschedule vor schedule.

SELECT cron.unschedule('jobsv2-mark-expired-14d')
WHERE EXISTS (SELECT 1 FROM cron.job WHERE jobname = 'jobsv2-mark-expired-14d');

SELECT cron.schedule(
  'jobsv2-mark-expired-14d',
  '37 3 * * *',
  $$
    UPDATE public.jobs_v2
    SET status = 'expired'
    WHERE status = 'active'
      AND last_seen_at < now() - interval '14 days'
  $$
);

-- Einmaliger Initial-Lauf für den Bestand (der Cron feuert erst morgen 03:37).
UPDATE public.jobs_v2
SET status = 'expired'
WHERE status = 'active'
  AND last_seen_at < now() - interval '14 days';
