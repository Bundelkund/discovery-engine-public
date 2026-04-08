# Discovery Engine

Job discovery service — scrapes, scores, and enriches job postings from multiple sources. Designed as the intake layer for WonderApply.

## Architecture

```
n8n (cron) → /scrape/{source} → Dedup → Score Stage 1 → Store → Score Stage 2 → Enrich
```

- **Sources**: Indeed (python-jobspy), Greenhouse (API), Adzuna (API), RSS feeds
- **Scoring**: Stage 1 = keyword/archetype matching (instant), Stage 2 = embedding similarity (OpenAI)
- **Enrichment**: Domain resolution, Hunter.io company data, CVF (Culture-Values Fit via LLM)
- **Storage**: Supabase (shared DB with WonderApply + JobHunt)

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

## API Endpoints

All endpoints (except /health) require `X-Api-Key` header matching `DE_API_KEY`.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Service health + registered components |
| POST | `/scrape/{source}` | Scrape jobs from source (indeed/greenhouse/adzuna/rss) |
| POST | `/score/batch` | Score unscored jobs for a profile |
| POST | `/enrich/{domain}` | Enrich a company by domain |
| POST | `/profiles` | Create a scoring profile |
| GET | `/profiles` | List all profiles |
| GET | `/profiles/{id}` | Get profile details |
| PUT | `/profiles/{id}` | Update profile |
| DELETE | `/profiles/{id}` | Delete profile |

Swagger docs: `http://localhost:8091/docs`

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
curl http://localhost:8090/health
```
