# Handover — DE-Filter Code Fix (discovery-engine) — ✅ ERLEDIGT 2026-06-11

> Ziel: monitor-Flag de_flag-aware machen, damit foreign-Firmen nicht mehr gescraped werden.
> DB-Hotfix ist erledigt (manuell), aber NICHT persistent — Code-Fix muss rein sonst Regression beim nächsten Seed-Run.

## Status: DONE
- Fix A (seed_ats_companies.py:71) + Fix B (db_slugs.py:42 + docstring) angewandt.
- Dry-run (no DB write) über scripts/out/*.json: foreign=0, null=0 monitored, de+remote alle monitored → Gate-Logik bestätigt.
- py_compile exit=0.
- Offen: Rezidiv-Schutz (#3 — `raw-jobs-inbox-dedup.sql` anwenden), raw_data-Drop (#4), search_tsv-Drop (#5), null-Sitemap reclassify (#6) + Live-Seed-Run zur Voll-Verifikation. content_hash-Follow-up als Fehldiagnose verworfen (s. #2).

## Kontext / Was passiert ist

Supabase `Konektos Database` (`guocdgjpbvsvcvchgolm`) lief auf 1733 MB (Free-Cap 500 MB → "data size exceeding usage limits").

Ursache: `raw_jobs` Staging-Tabelle übergelaufen → 107.278 Jobs (~1 GB), davon ~75k von **ausländischen Firmen** die gar nicht gescraped werden sollten.

Es gibt einen DE-Filter (`ats_companies.de_flag` mit Werten `de`/`remote`/`foreign`/`null`), aber er wird **klassifiziert, nie als Scrape-Gate angewandt**.

## Root Cause (2 Stellen)

**1. seed_ats_companies.py:71** — monitor wird de_flag-blind gesetzt:
```python
"monitor": st != "dead",  # don't daily-poll 404s; Stage A can revive
```
→ jede non-dead Firma (auch `foreign`) bekommt monitor=true.

**2. app/sources/db_slugs.py:23** — Scrape-Query filtert nur monitor, nicht de_flag:
```sql
WHERE ats = :ats AND status = 'active' AND monitor = true
```

de_flag wird in ats_scanner.py:281 `_de_flag()` / `_fold()` korrekt berechnet, landet in ats_companies.de_flag, wird dann aber nirgends als Gate genutzt.
Intent war da: migrations/ats-companies-registry.sql:37 hat Index-Kommentar `DE-filter (keep-for-DE = de ∪ remote)` — nie verdrahtet.

## Policy

keep-for-DE = `de_flag IN ('de','remote')`. foreign + null → kein Monitor.

`null` = Boards deren Feed keine Location trägt (Sitemap-Provider factorial/softgarden, ats_scanner.py:84 `"loc": None`). Können via Feed NIE klassifiziert werden → bewusst strikt ausgeschlossen (verliert auch deutsche Sitemap-Boards — siehe Follow-up).

## Fix A — seed_ats_companies.py:71

```python
"monitor": st != "dead" and v.get("de_flag") in ("de", "remote"),
```

## Fix B — app/sources/db_slugs.py:23 (defense-in-depth)

```sql
WHERE ats = :ats AND status = 'active' AND monitor = true
  AND de_flag IN ('de','remote')
```
(Optional, da Fix A monitor schon korrekt setzt. B schützt gegen Altzeilen + Direkt-Writes.)

## Verify nach Fix

```bash
# Seed dry-run / re-run, dann prüfen dass foreign monitor=false bleibt:
# SELECT de_flag, count(*) FILTER (WHERE monitor) FROM ats_companies GROUP BY de_flag;
# Erwartung: de=1166 monitored, remote=743 monitored, foreign=0, null=0
```
Vor Fix: Test schreiben der seed-Output für eine foreign-Firma prüft → monitor==false.

## Bereits erledigt (DB, manuell, reversibel)

```sql
UPDATE ats_companies SET monitor=false
WHERE monitor=true AND (de_flag IS NULL OR de_flag NOT IN ('de','remote'));
```
→ monitored 5586 → 1909. Onslaught gestoppt. ABER: nächster Seed-Run ohne Fix A überschreibt das wieder.

## DB-Size Cleanup (erledigt 2026-06-11, DB-Seite)

DB 1733 MB → **419 MB** (-76%, unter Free-Cap 500 MB, ~81 MB Puffer).

| Schritt | Aktion | DB danach |
|---|---|---|
| 1 | `TRUNCATE TABLE raw_jobs;` (107.278 rows, kein FK, 7.077 schon in jobs_v2, 100k unpromoteter foreign-Backlog) | 546 MB |
| 2 | 4 tote GIN-Indizes drop (s.u.) | 419 MB |

Gedroppte Indizes (alle scans=0):
```sql
DROP INDEX IF EXISTS idx_jobs_v2_search_tsv;        -- 34 MB, search_tsv-Ansatz tot
DROP INDEX IF EXISTS idx_jobs_search_tsv;           -- 22 MB, dito
DROP INDEX IF EXISTS idx_jobs_v2_description_trgm;  -- 65 MB, 0 scans
DROP INDEX IF EXISTS idx_jobs_v2_title_trgm;        -- 6.4 MB, 0 scans
```
Reversibel: alle via `CREATE INDEX` neu. raw_jobs via re-harvest.
**Befund**: 2 parallele FTS-Mechanismen — `search_tsv`-Spalte (vorberechnet, Indizes scans=0 auf beiden Tabellen = tot) vs. inline `to_tsvector(description)` (genutzt). trgm auf jobs_v2 ungenutzt (scans=0), auf legacy `jobs` genutzt (28).

## Follow-ups (Rezidiv-Schutz — Free-Cap ist knapp, ~81 MB Puffer)

1. ✅ ~~raw_jobs prunen~~ — erledigt via TRUNCATE (s.o.).
2. **~~content_hash-Bug~~ — FEHLDIAGNOSE, verworfen 2026-06-11 (Code-Verify).** raw_jobs.content_hash ist *by design* leer (`""` für alle Zeilen = die "1 distinct value"). `scrape_orchestrator.py:58`: Normalisation inkl. content_hash passiert erst im Refine; kein Scraper setzt ihn (grep `content_hash =` über `app/sources/*.py` leer); echter Hash → `refine_pipeline.py:138` für jobs_v2. **raw_jobs hat KEIN Hash-Dedup** — Inbox-Dedup-Key ist `(source, external_id)`. → Ingest-Hash fixen verhindert Rezidiv NICHT. Echte Ursache + Fix siehe #3.
3. ✅ **raw_jobs Rezidiv-Schutz (P1) — ERLEDIGT, verifiziert 2026-06-11 (DB + Code).** Echte Wachstums-Ursache (Chaos-Guard smoke 2026-06-09): raw_jobs hatte **kein UNIQUE constraint** auf `(source, external_id)` → Re-Scrape inserted identische Zeilen. Fix aus `migrations/raw-jobs-inbox-dedup.sql` ist **vollständig live** (Migration-Header "NOT YET APPLIED" war stale):
   - `uq_raw_jobs_source_external_id` — DB-Check: `indisvalid=true, indisunique=true`, partial `WHERE external_id <> ''`. ✅
   - `purge_raw_jobs(window_days int DEFAULT 42)` — DB-Check: existiert, Signatur/Body korrekt. ✅
   - Caller-Wiring — `refine_pipeline.py:213` ruft `supabase.rpc("purge_raw_jobs")` bei **jedem** run() (best-effort), Test `tests/test_refine_pipeline.py:211`. ✅

   → Re-Scrape-Dupes prallen ab, terminale rows purgen pro Refine (42d). **Watch:** Partial-Index nur `external_id <> ''` → Quellen mit leerer external_id werden nicht dedupliziert (dokumentierter Trade-off). pg_cron NICHT installiert — Retention läuft via App-Refine, nicht DB-Schedule.
4. **raw_data jsonb prüfen — Verify: Promoter liest es nicht.** refine_pipeline nutzt nur Surface-Spalten via `parse_raw` (title/url/company/…), null `raw_data`-Refs. Drop technisch sicher: `ALTER TABLE raw_jobs DROP COLUMN raw_data;` spart bei vollem Harvest ~500 MB. Trade-off: Schema-Zweck "lossless re-extraction" (Schema-Kommentar) stirbt — bewusste Entscheidung, nicht gratis.
5. **search_tsv-Spalte droppen** — Indizes weg, falls Spalte selbst auch ungenutzt → `ALTER TABLE jobs_v2 DROP COLUMN search_tsv;` (+ jobs) → etwas mehr Platz.
6. **null-Sitemap-Boards nachklassifizieren** — de_flag aus geScrapten raw_jobs.location ableiten statt aus Feed, damit deutsche factorial/softgarden-Boards nicht dauerhaft verloren.
7. **jobs-legacy Migration** — `jobs` (225 MB, legacy) NICHT droppen: Consumer noch dran (2.053 unique URLs nur in jobs, idx_scan 108k > jobs_v2 19k). Erst wenn Reads vollständig auf jobs_v2 umgestellt → drop spart 225 MB.

## Monitoring / Watch
- jobs_v2 trgm-Index weg → falls App `ILIKE '%x%'` auf `jobs_v2.description` macht → seq scan langsam. Aktuell scans=0, kein Problem. Bei Slow-Query → `CREATE INDEX ... USING gin (description gin_trgm_ops)` neu.
- DB-Size beobachten: `SELECT pg_size_pretty(pg_database_size(current_database()));` — bei >480 MB eingreifen.

## Dateien

- `scripts/seed_ats_companies.py:71` — Fix A
- `app/sources/db_slugs.py:23` — Fix B
- `scripts/ats_scanner.py:234-281` — de_flag-Klassifizierung (_fold/_de_flag), nur lesen
- `migrations/ats-companies-registry.sql:18,37` — monitor default + de_flag Index
