# ATS CC-Enumerability Matrix

First-pass Common-Crawl enumeration of slug-based ATS via `scripts/ats_scanner.py`.
A slug-ATS hosts every customer under one shared domain, so a CDX prefix-search over
that domain returns the customer slugs directly — no per-board crawling.

## Result (2 crawls: CC-MAIN-2026-21 + 2026-17, `--no-validate`, raw distinct slugs)

| ATS | mode | CDX domain(s) | slugs | CC-enum | notes |
|-----|------|---------------|------:|:-------:|-------|
| greenhouse | path | boards / job-boards.greenhouse.io | 2785 | ✓ | global; some numeric job-id leakage (dropped on feed-validate) |
| ashby | path | jobs.ashbyhq.com | 1714 | ✓ | global; `{slug}/{jobId}` |
| breezy | subdomain | breezy.hr | 1289 | ✓ | global; feed `/json` returns 403 to bots → validate via alt endpoint |
| recruitee | subdomain | recruitee.com | 1286 | ✓ | global; big domain → CDX p0 flaky (10060 timeout, recovered on 2nd crawl) |
| personio | subdomain | jobs.personio.de | 1018 | ✓ | **DE-only TLD** → DE-leaning; reference provider |
| factorial | subdomain | factorialhr.com | 222 | ✓ | `.com` only here; add `.de`/`.es` TLDs to widen |
| **lever** | path | jobs.lever.co | **0** | **✗** | **robots-blocked** — CC indexed only `jobs.lever.co/robots.txt`, no company pages |

6/7 CC-enumerable. **lever is not** (robots.txt disallows crawling → absent from CC).
Total enumerable raw slugs: ~8300 (global, unvalidated).

## Caveats

- **Raw, unvalidated.** Counts include dead/typo slugs + path noise. Feed-validation
  (`ats_scanner.py` without `--no-validate`) confirms live boards + job counts.
- **Global ≠ DE.** Only personio (`.de`) and factorial (`.de` TLD) are CDX-filterable to
  Germany. greenhouse/ashby/breezy/recruitee slugs don't encode country → DE-relevance
  must come from a downstream filter (feed location / job content), not CDX.
- **First-pass, 2 crawls.** Widening `--crawls` raises recall; large global domains may
  also undercount due to CC index pagination (no truncation hit here — cap 30 pages).

## Still to verify (new ATS, T2 `ats-enum-matrix`)

softgarden (`{slug}.career.softgarden.de`, DE, P1), teamtailor, workable, smartrecruiters,
and custom-domain candidates (concludis, d.vinci, rexx, onlyfy/Prescreen — likely
per-customer domains → not CC-enumerable, needs CDX probe).

## Reproduce

```bash
python scripts/ats_scanner.py --all --no-validate --crawls 2 --max-pages 30
python scripts/ats_scanner.py --ats personio          # + live feed-validation
```

Outputs (gitignored): `scripts/out/{ats}-enumeration.json` + `{ats}-candidates.yaml`.
