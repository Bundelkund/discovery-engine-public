# ADR: `sources`-Dimension + kanonischer `source`-Spaltenname

**Status:** accepted · **Scope:** Supabase `guocdgjpbvsvcvchgolm` (public) · discovery-engine

## Kontext

Job-Quellen leben heute als freie Text-Strings unter zwei Namen: `ats_companies.ats` (Provider) und `jobs`/`jobs_v2`/`raw_jobs.source` (Provider). Der schema-audit-Detector (jaccard 0.389, 7 shared) belegte das als 4-seitiges Synonym. Zusätzlich existiert keine Daten-Spalte, die ATS (Firmen-Feed pro slug) von Aggregator (Keyword-API wie indeed) trennt — die Unterscheidung steckt nur in getrennten `@SourceRegistry.register(...)`-Klassen. Drittes Problem: `ats_companies.source` bezeichnet etwas anderes (Herkunft `cc`/`scrape`/`manual`).

## Entscheidung

1. Neue Dimensionstabelle `sources` (PK `code`, plus `label`, `type CHECK IN ('ats','aggregator','feed','internal')`, `base_url_template`, `feed_url_template`, `is_active`, `notes`). Lookup ist gerechtfertigt, weil sie **Metadaten trägt** (Template + Typ), nicht zur Storage-Dedup (db-design-rules §3).
2. Kanonischer Spaltenname **`source`** über alle Tabellen. `ats_companies.source`(Herkunft)→`origin` macht den Namen frei; `ats_companies.ats`→`source`. Fact-Tables heißen bereits `source` → unangetastet.
3. FK **nur** `ats_companies.source → sources.code` (low-churn). `jobs`/`jobs_v2`/`raw_jobs` (hot fact) bleiben denormalisierter Text ohne FK — Kimball, kein `FOR KEY SHARE`-Lock pro Insert.

## Begründung der Namenswahl (`source`, nicht `ats_source`)

Der Audit schlug `ats_source` vor. Verworfen: die Fact-Tables tragen Aggregator-Zeilen (indeed/linkedin/jooble), die **kein ATS** sind — `ats_source` würde auf diesen Zeilen lügen. `source` ist der korrekte Oberbegriff (ats+aggregator+feed+internal) und zugleich der kleinste Blast-Radius (nur eine Tabelle umbenannt). → Audit-Befund damit *resolved, abweichend begründet*; kein erneutes WARN.

## Trade-off / Reversibilität

Hart-reversibel: Rename + FK + späterer `feed_url`/`careers_url`-Drop. Migration und Code (`on_conflict`-Key, `row_from`-Key-Swap) **müssen derselbe Deploy** sein, sonst zeigt der Seed-Upsert auf eine weggefallene Spalte. Solo-Dev/Single-Deploy → akzeptabel. Storage-neutral (kein Cap-Fix; DB-Größe war bereits gelöst).
