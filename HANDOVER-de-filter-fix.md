# Handover — DE-Filter Code Fix (discovery-engine) — ✅ ERLEDIGT 2026-06-11

> Ziel: monitor-Flag de_flag-aware machen, damit foreign-Firmen nicht mehr gescraped werden.
> DB-Hotfix ist erledigt (manuell), aber NICHT persistent — Code-Fix muss rein sonst Regression beim nächsten Seed-Run.

## Status: DONE
- Fix A (seed_ats_companies.py:71) + Fix B (db_slugs.py:42 + docstring) angewandt.
- Dry-run (no DB write) über scripts/out/*.json: foreign=0, null=0 monitored, de+remote alle monitored → Gate-Logik bestätigt.
- py_compile exit=0.
- Offen bleiben nur die 3 Follow-ups unten (raw_jobs prune, content_hash-Bug, null-Sitemap reclassify) + ein echter Live-Seed-Run zur Voll-Verifikation.

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

## Follow-ups (separat, nicht Teil dieses Fixes)

1. **raw_jobs prunen** — 107k rows / ~1 GB. DB-Hotfix entfernt foreign nicht aus Staging. Nach Code-Fix: foreign raw_jobs löschen + `VACUUM FULL raw_jobs`. raw_data jsonb (~500 MB) dupliziert description → droppen prüfen.
2. **content_hash-Bug** — alle 107.278 raw_jobs haben `content_hash` = 1 distinct value (konstant) → Dedup-by-hash tot → Staging wächst unbounded. Hash-Berechnung im Ingest suchen/fixen.
3. **null-Sitemap-Boards nachklassifizieren** — de_flag aus geScrapten raw_jobs.location ableiten statt aus Feed, damit deutsche factorial/softgarden-Boards nicht dauerhaft verloren.

## Dateien

- `scripts/seed_ats_companies.py:71` — Fix A
- `app/sources/db_slugs.py:23` — Fix B
- `scripts/ats_scanner.py:234-281` — de_flag-Klassifizierung (_fold/_de_flag), nur lesen
- `migrations/ats-companies-registry.sql:18,37` — monitor default + de_flag Index
