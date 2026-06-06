# ATS Registry Pipeline — how it stays current

Discovers + tracks every company on slug-based ATS via Common Crawl, kept live by two
cadences, never deletes. Single fetch per board yields active + job_count + titles + `de_flag`.

## Stages

| Stage | Command | Reads | Writes | Cadence |
|-------|---------|-------|--------|---------|
| **A · Discover** | `ats_scanner.py --all --no-validate` | CC CDX (latest crawls) | new slugs → `{ats}-enumeration.json` | monthly (new CC crawl) |
| **B · Refresh** | `ats_scanner.py --all --revalidate` | prior enumeration JSON + live feeds | `active` · `job_count` · `sample_titles` · `de_flag` | daily / weekly |
| **C · Load** | `sheets_load_ats.py <sheetId>` | enumeration JSON | Google Sheet (1 tab/ATS, idempotent) | after B |
| **D · Enrich** | (T9) Hunter → `company_profiles` | registry rows w/ relevant jobs | `domain`, `enriched_at` | lazy, quota-aware |

- **A finds NEW companies** — CC publishes a crawl ~monthly; recall grows by re-running A over the newest crawl(s). Bounded by `--max-pages`; `truncated` flag if cap hit.
- **B updates STATE** — jobs open/close + boards die daily. `--revalidate` re-probes feeds without re-crawling CDX (cheap), so it can run often. Dead feed → `active=false` (row kept). **B is the recheck primitive** (T8).
- **`de_flag`** (`de` / `remote` / `foreign` / `null`) is derived in B's single fetch — no separate classification pass. Keep-for-DE = `de ∪ remote`. ISO providers (recruitee, breezy) exact; free-string (greenhouse, ashby, personio) match DE-city/Germany; factorial sitemap has no location → `null`.

## No-delete

Neither stage deletes. A only inserts new slugs; B flips `active`/sets `status='dead'` but keeps the row + its `seen_in_crawls` history. Registry only grows.

## How it is triggered

- **Now (manual):** run the commands above. B + C are a 2-liner re-runnable any time.
- **Scheduled (T8):** Windows Task Scheduler or n8n cron —
  - daily → Stage B (`--revalidate --all`) + Stage C (Sheet reload / registry upsert)
  - monthly → Stage A (`--no-validate --all`) to pull the new CC crawl, then B
- The repo already runs scheduled jobs (`.claude/routines/`, Task Scheduler). Same pattern: a routine invoking `python scripts/ats_scanner.py --all --revalidate` then the seed/upsert step.

## Provider enumerability

6/7 CC-enumerable (see [ats-enumerability.md](ats-enumerability.md)). **lever is robots-blocked** → not CC-discoverable. lever's feed (`api.lever.co/v0/postings/{slug}`) works once a slug is known, so lever stays a valid source for slugs that **accrete downstream** (apply-links harvested from jobs found via other sources) or curated yaml — `source='scrape'|'manual'`, never `'cc'`. CC WAT link-graph mining is a P3 option.

## Lessons baked in

- Probe each feed endpoint empirically before encoding (breezy "403" was stale; factorial has no JSON API → sitemap.xml).
- Separate fetch-error from parse-error in counters — a swallowed `TypeError` (ashby `secondaryLocations` = list of dicts) looked like 579 rate-limit errors. Deterministic err-count = structural, not load → single-thread diagnose first.
- Capture location during validation, not in a second pass — one fetch, not two.
