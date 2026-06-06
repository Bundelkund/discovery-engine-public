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

**Live (T8):** n8n cron → HTTP → discovery-engine endpoint `POST /scan/{stage}`
(`app/routes/scan.py`). The endpoint runs `ats_scanner.py` + `seed_ats_companies.py`
as a BackgroundTask subprocess and returns `202 {run_id}` immediately (scan takes
minutes); status + audit land in `public.ats_scan_runs` (`GET /scan/runs`). One run
per stage at a time → `409` otherwise (no CC overlap).

| n8n workflow | id | schedule | calls |
|--------------|----|----------|-------|
| Discovery Engine — ATS Refresh (Stage B daily) | `BFfKQKCRB8F6jt4V` | `17 4 * * *` | `POST https://discovery-engine.konektos.de/scan/revalidate` |
| Discovery Engine — ATS Discover (Stage A monthly) | `nMGEo1gpoCDSmtQB` | `23 4 1 * *` | `POST …/scan/discover` |

- Auth: header `X-API-Key` = `N8N_API_KEY` (consumer `n8n` in `config/api-keys.yaml`,
  scopes `scrape:trigger,jobs:read`). Key lives in **Coolify Env** (app helpful-hyena),
  not the gitignored `.env`.
- Optional `?ats=&limit=` narrow scope (targeted reruns / testing); n8n passes neither → full `--all`.
- **Manual:** run `ats_scanner.py --all --revalidate` then `seed_ats_companies.py`, or
  hit the endpoint directly. Both workflows also carry a Manual Trigger.
- **Activation gate:** workflows ship **inactive**. Activate only after `N8N_API_KEY` is
  set in Coolify, redeployed, and `curl https://discovery-engine.konektos.de/health` is green.

## Provider enumerability

6/7 CC-enumerable (see [ats-enumerability.md](ats-enumerability.md)). **lever is robots-blocked** → not CC-discoverable. lever's feed (`api.lever.co/v0/postings/{slug}`) works once a slug is known, so lever stays a valid source for slugs that **accrete downstream** (apply-links harvested from jobs found via other sources) or curated yaml — `source='scrape'|'manual'`, never `'cc'`. CC WAT link-graph mining is a P3 option.

## Lessons baked in

- Probe each feed endpoint empirically before encoding (breezy "403" was stale; factorial has no JSON API → sitemap.xml).
- Separate fetch-error from parse-error in counters — a swallowed `TypeError` (ashby `secondaryLocations` = list of dicts) looked like 579 rate-limit errors. Deterministic err-count = structural, not load → single-thread diagnose first.
- Capture location during validation, not in a second pass — one fetch, not two.
