# Discovery Engine

> **Single-tenant** job discovery service — scrapes, scores, and enriches job postings from multiple sources for **one** applicant. Primary consumer: the [Apply Skill](https://github.com/Bundelkund/apply-skill) (separate repo, Claude-Code workflow).
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

## Production (Coolify) — READ BEFORE ADDING SECRETS/SOURCES

Production runs on **Coolify**, app `helpful-hyena-dnvav57trw1nkmsjq2oh4o2w` (under project `wonderapply`, but a **separate app** from WonderApply itself), built from **`private/main`** of this repo. Host `204.168.134.173`, public `https://discovery-engine.konektos.de`.

**`.env` is gitignored → it is NOT in the image and NOT read in production.** It only works for local `docker compose up` / `uvicorn`. Production env comes solely from Coolify Environment Variables. So:

| Task | Local-only? | Production also needs |
|------|-------------|----------------------|
| New API key (Adzuna, Careerjet, Jooble, …) | edit `.env` | **Coolify → app `helpful-hyena` → Environment Variables → add key → Redeploy** |
| New source adapter / config change | commit | **`git push private main`** (Coolify builds from `private/main`, not your local tree) |

**Checklist when adding a source or secret (all steps, in order):**
1. `git push private main` — else Coolify build won't contain the code.
2. Coolify → app **`helpful-hyena`** (NOT the WonderApply app) → Environment Variables → set keys (values from local `.env`).
3. Redeploy the **DE app** (`helpful-hyena`). Redeploying WonderApply does nothing for DE.
4. Verify on the live container: `ssh root@204.168.134.173` → `docker exec <de-container> sh -c 'printenv KEY; ls /app/app/sources/'`.

⚠️ Two distinct Coolify apps share the `wonderapply` project. Deploying/setting env on the WonderApply app does NOT affect Discovery Engine. Always target `helpful-hyena-...`.

Full env table + persistent-file-volume (`portals.local.yaml`) details: florian-knowledge `dev/projects/discovery-engine/INDEX.md` §"Coolify Environment Variables".

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

[**Apply Skill**](https://github.com/Bundelkund/apply-skill) is the canonical consumer. It runs interactive onboarding (which archetypes? which company portals?) and writes the resulting `config/portals.local.yaml` + `config/scoring-profile.local.yaml` into a Discovery Engine instance, then queries `/jobs` for personalized matches and drives the application-writing workflow downstream.
