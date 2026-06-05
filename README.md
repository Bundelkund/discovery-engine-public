# Discovery Engine

> **Single-tenant** job discovery service — scrapes, scores, and aggregates job postings from multiple ATS sources for **one** applicant. Designed to feed the [Apply Skill](https://github.com/Bundelkund/apply-skill) (a Claude-Code workflow for personalized job applications).

This is an **open-source intake layer**. It is **not** a multi-tenant SaaS — see [ROADMAP.md](ROADMAP.md) if you need multi-user support.

## What it does

```
n8n (cron) ──▶ POST /scrape/{source} ──▶ Dedup ──▶ Resolve ──▶ Stage-1 Score ──▶ Supabase
                                                                          │
                                       Apply Skill / your tooling ◀───────┘
                                       GET /jobs?keywords_positive=…
                                       GET /companies/{domain}
```

- **Sources** (`app/sources/`):
  - *ATS APIs* — Greenhouse, Ashby, Lever, Personio, Recruitee, Breezy, Factorial
  - *Aggregators / job boards* — Adzuna, Jooble, Careerjet, TheMuse, LinkedIn, Indeed (jobspy), Arbeitsagentur (BA Jobsuche-API)
  - *Feeds* — RSS
- **Resolution** (`app/resolution/`): backfills thin job descriptions from the posting origin (ATS / career page) before scoring, so metadata-only sources don't starve keyword matching. Skips tracker hosts (`config/resolution.yaml` `blocked_hosts`) and rejects anti-bot / captcha interstitials via an output-quality gate.
- **Scoring** (`app/scoring/`): one stage — keyword + archetype matching against a *single* configured profile (Stage 2 LLM/embedding scoring was removed; see ROADMAP.md if reintroducing)
- **Enrichment** (`app/enrichment/`): company-domain resolution, Hunter.io company data
- **Storage**: Supabase (REST only — no direct DB access required by consumers)
- **Consumer pattern**: REST. Consumers identify themselves via `X-API-Key` (per-consumer key in `config/api-keys.yaml`).

## Setup

```bash
pip install -e .
cp .env.example .env
#  fill in: SUPABASE_URL, SUPABASE_KEY, APPLY_API_KEY (any string you choose)
#  optional: HUNTER_API_KEY, OPENAI_API_KEY (currently unused — reserved for Stage-2)

uvicorn app.main:app --port 8091
curl http://localhost:8091/health
```

For the full schema, install [Supabase CLI](https://supabase.com/docs/guides/cli) and run the migration in `migrations/bundle-b-additive.sql`. See `migrations/README.md` for hosted-Supabase instructions.

## Personalize it (single-user setup)

Discovery Engine ships with a **demo configuration** so it boots out-of-the-box. To make it useful for *your* job search, override two YAMLs locally — they are gitignored:

| Demo file (committed) | Your private override (gitignored) | What it controls |
|---|---|---|
| `config/portals.yaml` | `config/portals.local.yaml` | Which company ATS boards to scrape |
| `config/archetypes.yaml` | `config/archetypes.local.yaml` *(optional)* | Catalog of role archetypes + keywords |

The recommended way to generate these from an interactive flow ("which roles do you want, which companies to track?") is the **Apply Skill** — a Claude-Code skill that walks you through onboarding and writes the local overrides for you. See:

→ **[github.com/Bundelkund/apply-skill](https://github.com/Bundelkund/apply-skill)** *(separate repo, also open-source)*

You can absolutely run Discovery Engine without the Apply Skill — just edit `config/portals.local.yaml` by hand.

## API

`X-API-Key` is required on every endpoint except `/health`. Keys are defined in `config/api-keys.yaml` and resolved from env vars. Each consumer has a list of `scopes` enforced per route.

| Method | Path | Scope | Description |
|--------|------|-------|-------------|
| GET | `/health` | — | Service health + registered components |
| POST | `/scrape/{source}` | `scrape:trigger` | Scrape jobs from one source (greenhouse, ashby, lever, personio, recruitee, breezy, factorial, linkedin, indeed, adzuna, jooble, careerjet, themuse, arbeitsagentur, rss). Live list in `/health.sources`. Response counts include `descriptions_resolved` (jobs backfilled by the resolution step). |
| POST | `/enrich/{domain}` | `scrape:trigger` | Run enrichment pipeline against one domain |
| GET | `/jobs` | `jobs:read` | Paginated job list with filters (keywords, location, distance, source, seniority, salary, …) |
| GET | `/jobs/{id}` | `jobs:read` | Job detail |
| GET | `/companies/{domain}` | `jobs:read` | Company profile (Hunter.io + watchlist signals) |

Swagger docs at `http://localhost:8091/docs` once running.

### Filtering example

```
GET /jobs?keywords_positive=Agile&keywords_positive=Coach
        &keywords_negative=Junior
        &location=Berlin&max_distance_km=50
        &max_age_days=14
        &source=greenhouse&source=ashby
        &exclude_domain=spam-staffing.de
        &sort=score_keyword&limit=50
X-API-Key: <APPLY_API_KEY>
```

## Configuration

| File | Purpose |
|---|---|
| `config/sources.yaml` | Per-source settings (search terms, limits, country) |
| `config/resolution.yaml` | Description-resolution gates, `blocked_hosts`, anti-bot markers |
| `config/scoring.yaml` | Scorer weights, thresholds, store-threshold gate |
| `config/enrichment.yaml` | Enricher pipeline order and dependencies |
| `config/archetypes.yaml` | Role-archetype catalog (keywords DE/EN) |
| `config/portals.yaml` | Tracked companies for ATS scrapers |
| `config/data-quality.yaml` | MinHash thresholds + DQ rules |
| `config/api-keys.yaml` | Per-consumer API key definitions |

`*.local.yaml` overrides are loaded first if present (see `app/config.resolve_local_override`).

## Docker

```bash
docker compose up -d
curl http://localhost:8091/health
```

## Architecture & contributing

- [CLAUDE.md](CLAUDE.md) — project conventions for AI-assisted development
- [ROADMAP.md](ROADMAP.md) — multi-tenant evolution path (Variant C, on hold pending consumer alignment)
- [docs/SCORING.md](docs/SCORING.md) — scoring deep dive
- [docs/audits/](docs/audits/) — recent system-architecture and publication-readiness audits

## License

[MIT](LICENSE)
