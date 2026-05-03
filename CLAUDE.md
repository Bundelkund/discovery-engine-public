# Discovery Engine

> **Single-tenant** job discovery service — scrapes, scores, and enriches job postings from multiple sources for **one** applicant. Primary consumer: the [Apply Skill](https://github.com/Bundlekund/apply-skill) (separate repo, Claude-Code workflow).
>
> Multi-tenant deployment ("WonderApply"-style SaaS) requires Architecture Variant C — see [ROADMAP.md](ROADMAP.md). Out of scope for this repo.

## Architecture

```
n8n (cron) -> POST /scrape/{source} -> Dedup -> Score Stage 1 -> Store -> (optional) Enrich
                                                                              |
                                              GET /jobs, /companies/{domain} <-+ (consumers read here)
```

- **Sources** (`app/sources/`): Greenhouse, Ashby, Lever, Personio (ATS APIs), Adzuna, RSS, Indeed (jobspy)
- **Scoring** (`app/scoring/`): Stage 1 keyword/archetype is the only stage currently active
- **Enrichment** (`app/enrichment/`): domain resolution, Hunter.io company data
- **Data quality** (`app/data_quality/`): MinHash near-dup, location normalization, rules engine
- **Storage**: Supabase (REST, no direct DB)
- **Consumers**: read via REST (`GET /jobs`, `GET /companies/{domain}`, `POST /profiles/sync`) — never via Supabase directly

Patterns: registry with self-registration decorators, repository pattern for DB access, YAML-driven configs.

## Build & Verify

```bash
pip install -e .
cp .env.example .env   # fill in SUPABASE_URL, SUPABASE_KEY, WA_API_KEY at minimum
uvicorn app.main:app --port 8091

# Verify
curl http://localhost:8091/health
pytest -x
```

## Auth

`X-API-Key` header on every endpoint except `/health`. Keys are defined in `config/api-keys.yaml` and resolved from env vars (`WA_API_KEY`, `JH_API_KEY`). Each consumer has a list of `scopes` — see `app/dependencies.py` for the per-route enforcement.

## Conventions

| Rule | Why |
|------|-----|
| New scrapers register via `@SourceRegistry.register("<id>")` in `app/sources/` and inherit `BaseScraper` | Auto-discovered through side-effect imports in `app/sources/__init__.py` |
| Repository methods are sync; wrap in `asyncio.to_thread` from async routes | `supabase-py` is sync; would otherwise block the FastAPI event loop |
| Configs are YAML in `config/`, loaded via cached `load_*_config()` in `app/config.py` | Avoid re-parsing per request |
| Consumer-facing response shapes live in `app/models/responses.py`, separate from pipeline models in `app/models/job.py` | Pipeline lifecycle (`RawJob` → `NormalizedJob` → `ScoredJob`) decoupled from public API |
| Tests follow source layout: `tests/<subpackage>/test_<module>.py` | One test file per module makes coverage gaps visible |
| Migrations are additive-only, idempotent (`IF NOT EXISTS`), run via Supabase Dashboard | No direct Postgres URL available; rollback documented in `migrations/README.md` |

## Where to start reading

`app/main.py` (FastAPI factory) -> `app/services/scrape_orchestrator.py` (pipeline) -> `app/repositories/jobs.py` (Supabase boundary) -> `app/routes/jobs_api.py` (consumer-facing read API).

## Companion project

[**Apply Skill**](https://github.com/Bundlekund/apply-skill) is the canonical consumer. It runs interactive onboarding (which archetypes? which company portals?) and writes the resulting `config/portals.local.yaml` + `config/scoring-profile.local.yaml` into a Discovery Engine instance, then queries `/jobs` for personalized matches and drives the application-writing workflow downstream.
