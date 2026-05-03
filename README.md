# Discovery Engine

Job discovery service — scrapes, scores, and enriches job postings from multiple sources. Designed as the intake layer for WonderApply.

## Architecture

```
n8n (cron) → /scrape/{source} → Dedup → Score Stage 1 → Store → Score Stage 2 → Enrich
```

- **Sources**: Greenhouse, Ashby, Lever, Personio (ATS APIs), Adzuna, RSS, Google Jobs (Tavily), BA Jobboerse
- **Scoring**: Stage 1 = keyword/archetype (instant), Stage 2 = embedding (OpenAI), Stage 3 = LLM role analysis (Claude Haiku)
- **Enrichment**: Domain resolution, Hunter.io company data, CVF (Culture-Values Fit via LLM), Kununu, Tavily Signals
- **Storage**: Supabase (shared DB with WonderApply + JobHunt)
- **Consumers**: WonderApply (via REST API), Apply Skill (CLI)

Key patterns: Registry with self-registration decorators, config-driven pipelines (YAML), repository pattern for DB access.

## Setup

```bash
# Install
pip install -e .

# Configure
cp .env.example .env
# Fill in: SUPABASE_URL, SUPABASE_KEY, DE_API_KEY (self-chosen)
# Optional: HUNTER_API_KEY, OPENAI_API_KEY (for Stage 2 scoring)

# Run
uvicorn app.main:app --port 8091
```

## Authentication (Phase 5+)

The legacy shared `DE_API_KEY` is replaced by per-consumer keys defined in `config/api-keys.yaml`:

| Consumer | Env var | Scopes | Active |
|---|---|---|---|
| WonderApply | `WA_API_KEY` | `jobs:read`, `scrape:trigger` | yes |
| JobHunt | `JH_API_KEY` | `jobs:read` | no |

Send `X-Api-Key: <consumer-key>` in every request. n8n workflows that call `/scrape/{source}` use `WA_API_KEY` (it has the `scrape:trigger` scope).

## API Endpoints

All endpoints (except /health) require `X-Api-Key` header matching one of the consumer keys above.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Service health + registered components |
| POST | `/scrape/{source}` | Scrape jobs from source (greenhouse/ashby/lever/personio/rss) |
| POST | `/score/batch` | Score unscored jobs for a profile |
| POST | `/enrich/{domain}` | Enrich a company by domain |
| POST | `/discover/opportunities` | Proactive company recommendations |
| **GET** | **`/jobs`** | **Paginated job list with scores (for WonderApply)** |
| **GET** | **`/jobs/{id}`** | **Job detail with all score fields** |
| **GET** | **`/companies/{domain}`** | **Company profile with Hunter + CVF + signals** |
| POST | `/profiles` | Create a scoring profile |
| GET | `/profiles` | List all profiles |
| GET | `/profiles/{id}` | Get profile details |
| PUT | `/profiles/{id}` | Update profile |
| DELETE | `/profiles/{id}` | Delete profile |
| **POST** | **`/profiles/sync`** | **Sync WonderApply profile for scoring** |

Swagger docs: `http://localhost:8091/docs`

### WonderApply Provider API

The bold endpoints above form the **Provider API** — WonderApply consumes jobs, scores, and company data exclusively through these REST endpoints instead of querying the shared Supabase directly.

```
WonderApply ──GET /jobs──→ Discovery Engine ──SELECT──→ Supabase
WonderApply ──GET /companies/{domain}──→ Discovery Engine
WonderApply ──POST /profiles/sync──→ Discovery Engine (on profile update)
```

## Configuration

YAML configs in `config/`:
- `sources.yaml` — Source-specific settings (search terms, limits)
- `scoring.yaml` — Scorer weights, thresholds, stage gates
- `enrichment.yaml` — Enricher pipeline steps and dependencies
- `archetypes.yaml` — Job archetype definitions (keywords DE/EN)
- `portals.yaml` — Tracked companies for Greenhouse scraping

## Docker

```bash
docker compose up -d
curl http://localhost:8091/health
```
