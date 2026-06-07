# ATS CC-Enumerability Matrix

First-pass Common-Crawl enumeration of slug-based ATS via `scripts/ats_scanner.py`.
A slug-ATS hosts every customer under one shared domain, so a CDX prefix-search over
that domain returns the customer slugs directly — no per-board crawling.

## Result (CDX: 2 crawls CC-MAIN-2026-21 + 2026-17 · feed-validated)

`raw` = distinct CDX slugs; `active` = live feed returning ≥1 parseable job.

| ATS | mode | CDX domain(s) | raw | active | feed endpoint | notes |
|-----|------|---------------|----:|------:|---------------|-------|
| greenhouse | path | boards / job-boards.greenhouse.io | 2785 | 2045 | `boards-api.../v1/boards/{slug}/jobs` (json `jobs`) | global; numeric job-id leakage dropped on validate |
| ashby | path | jobs.ashbyhq.com | 1714 | 1411 | `api.ashbyhq.com/posting-api/job-board/{slug}` (json `jobs`) | global; `{slug}/{jobId}` |
| recruitee | subdomain | recruitee.com | 1286 | 902 | `{slug}.recruitee.com/api/offers/` (json `offers`) | global; CDX p0 flaky (10060, recovered on 2nd crawl) |
| breezy | subdomain | breezy.hr | 1289 | 756 | `{slug}.breezy.hr/json` (json list) | global; feed serves bots fine — empty `[]` = no open jobs (inactive) |
| personio | subdomain | jobs.personio.de | 1018 | 660 | `{slug}.jobs.personio.de/xml` (`<position>`) | **DE-only TLD** → DE-leaning; reference provider |
| factorial | subdomain | factorialhr.com | 222 | 141 | `{slug}.factorialhr.com/sitemap.xml` (`/job_posting/` locs) | `.com` only; **no JSON API → sitemap.xml is the feed** |
| softgarden | subdomain | career.softgarden.de | 104 | 98 | `{slug}.career.softgarden.de/jobs.json` (schema.org `DataFeed`) | **DE-only TLD** → DE-leaning; jobs under `dataFeedElement[].item` |
| **lever** | path | jobs.lever.co | **0** | **0** | — | **robots-blocked** — CC indexed only `jobs.lever.co/robots.txt` |

7/8 CC-enumerable; lever is not. **~8400 raw slugs → 6013 active feeds.**

### DE classification (`de_flag`, derived in the validate pass — see [ats-pipeline.md](ats-pipeline.md))

`keep-for-DE = de ∪ remote` (remote = countryless-remote, could be German; bias to include).

| ATS | active | de | remote-nc | **keep** | foreign |
|-----|------:|---:|------:|-----:|------:|
| greenhouse | 2045 | 244 | 569 | 813 | 1232 |
| ashby | 1411 | 129 | 142 | 271 | 1140 |
| recruitee | 902 | 269 | 0 | 269 | 633 |
| personio | 660 | 497 | 33 | 530 | 130 |
| breezy | 756 | 23 | 0 | 23 | 733 |
| softgarden | 98 | 95 | 0 | 95 | 3 |
| factorial | 141 | — | — | — | — |
| **TOTAL** | **6013** | **1257** | **744** | **2001** | **3871** |

ISO providers (recruitee, breezy) → exact country code, remote-nc≈0. factorial sitemap
carries no location → `de_flag` null (unknown via feed). True DE ∈ [1257 strict, 2001 incl. remote].

### Feed-endpoint corrections (validation pass)

- **breezy**: earlier "403 to bots" was stale — `/json` returns 200 to a plain UA. An empty
  `[]` body just means no open positions, so those slugs are (correctly) inactive, not blocked.
- **factorial**: exposes no public jobs JSON (`/job_posting`, `/api/*` → 404). `sitemap.xml`
  (200, bot-friendly) lists every `/job_posting/<title>-<id>` URL → job count + titles parsed from there.

## Caveats

- **active vs raw.** raw CDX counts include dead/typo slugs + path noise; `active` is the
  feed-validated subset (live board, ≥1 parseable job). Re-probe without re-crawling via
  `--revalidate` (reads the prior enumeration JSON) — this is the periodic-recheck primitive.
- **Global ≠ DE.** Only personio (`.de`) and factorial (`.de` TLD) are CDX-filterable to
  Germany. greenhouse/ashby/breezy/recruitee slugs don't encode country → DE-relevance
  must come from a downstream filter (feed location / job content), not CDX.
- **First-pass, 2 crawls.** Widening `--crawls` raises recall; large global domains may
  also undercount due to CC index pagination (no truncation hit here — cap 30 pages).

## Still to verify (new ATS, T2 `ats-enum-matrix`)

teamtailor, workable, smartrecruiters, and custom-domain candidates (concludis, d.vinci,
rexx, onlyfy/Prescreen — likely per-customer domains → not CC-enumerable, needs CDX probe).

**softgarden — verified** (added above): `career.softgarden.de` is CC-enumerable (subdomain
mode); `/jobs.json` serves a schema.org `DataFeed` (200, bot-friendly) with full `JobPosting`
items (title, url, description, `jobLocation.address`). DE-only TLD → 95/98 active boards
classify `de` directly off `addressCountry`.

## Reproduce

```bash
python scripts/ats_scanner.py --all --no-validate --crawls 2 --max-pages 30  # CDX only
python scripts/ats_scanner.py --ats personio                                 # CDX + feed-validate
python scripts/ats_scanner.py --all --revalidate --workers 16                # re-probe feeds, no CDX
```

Outputs (gitignored): `scripts/out/{ats}-enumeration.json` + `{ats}-candidates.yaml`.
