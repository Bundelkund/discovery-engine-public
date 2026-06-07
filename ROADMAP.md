# Discovery Engine — Roadmap

## Where we are

Discovery Engine is currently a **single-tenant** intake service. One configured profile (`config/archetypes.yaml` + `config/portals.yaml` plus optional `*.local.yaml` overrides) drives all scraping and scoring. All consumers reading from `/jobs` see the same shared pool of scored rows.

This works well for:
- **Personal use** — one applicant, one taste profile, one set of tracked companies
- **Single-team usage** — a small team with a shared role archetype

This explicitly does **not** work for:
- Multi-user SaaS consumers where each end-user has different keywords, archetypes, or tracked companies
- Tenants that need data isolation between users

## The next step — Variant C (multi-tenant)

To support a multi-tenant consumer (e.g. a SaaS like WonderApply), Discovery Engine would need to evolve. The work is intentionally tracked here, not in Linear, because **it requires alignment with the actual consumer team before it makes sense to start.** Implementing it in isolation will hit integration issues.

### Required changes

1. **Data model** — `tenants` and `end_users` tables; `user_archetypes`, `user_portals`, `user_filters` tables; `jobs.user_id` (or junction `job_user_scores`) for per-user scoring; Supabase RLS for isolation.
2. **Auth** — JWT or token-with-claims (`consumer_id` + `user_id`) inside the `X-API-Key` flow; scope model extended with per-user scopes.
3. **Profile API** — `POST /profiles/sync`, `GET/PUT /profiles/{user_id}`, admin endpoints for catalog management. (Note: `POST /profiles/sync` was removed in Phase 1 of Bundle B — would need to come back.)
4. **Orchestrator** — load per-user profile in `ScrapeOrchestrator.run`; either scrape-per-user (N×M traffic) or scrape-once-score-N-times (cheaper but more complex); per-user `insert_batch` with `user_id` tagging.
5. **n8n workflows** — iterate active users instead of one global cron; quotas; failure isolation per user.
6. **Admin UI** — self-service onboarding so users can register portals and pick archetype weights without server-side YAML edits.
7. **Tests** — full multi-tenant test suite, RLS-isolation tests, quota tests.
8. **Migration path** — move existing single-tenant data into the first tenant.

### Effort estimate

Realistically **6-8 weeks** of solo full-time work for someone with domain knowledge; **1.5-2x** for an external developer without prior context. This is not an incremental sprint — it is a tier-1 product expansion that turns Discovery Engine from "intake layer" into a SaaS with admin API, onboarding flow, and quota management.

### Status

**On hold.** Variant C is only worth starting once a multi-user consumer (e.g. WonderApply) commits to the integration in detail and the API contract is co-designed. Until then the single-tenant model is the supported architecture.

If you are evaluating Discovery Engine for a multi-tenant use case and want to discuss this, open an issue.

## Recently shipped (single-tenant)

- **Description resolution (Slice B)** — orchestrator step 4a backfills thin descriptions from the posting origin before MinHash + Stage-1 scoring, so metadata-only sources (adzuna/jooble/careerjet) no longer starve keyword matching. Hardened against tracker hosts (`blocked_hosts`) and anti-bot/captcha interstitials via an absolute output-length floor + block-page markers. New `ScrapeResponse.descriptions_resolved` counter; `config/resolution.yaml`. *(50eb117, 56277f2)*
- **Arbeitsagentur source** — BA Jobsuche-API adapter as the DE-wide master aggregator. *(e9e7f0c)*
- **Recall source-of-truth** — search terms now regenerated from a single `search-profile.yaml` (lives in the consumer repo) → `config/sources.yaml`, closing the niche-recall + location drift that made jobs like HDI/Hannover unfindable. *(e49aefc)*

## Smaller open follow-ups (single-tenant)

Tracked in `docs/audits/` audit reports, summarized here:

- **Profile-File loading** — re-introduce a tiny `config/scoring-profile.local.yaml` that the orchestrator reads at scrape time, so a single user can override the empty default profile without re-deploying. (~1 day; complements the Apply Skill onboarding flow.)
- **`JobQueryBuilder` extraction** — split `JobRepository.query()` (162-line method, 13 params) into a dedicated builder; consolidate `_DE_CITIES` into `LocationNormalizer`. (~90 min; system-architecture audit C-3.)
- **Pydantic schemas for YAML configs** — `sources.yaml`, `scoring.yaml`, `enrichment.yaml`, `archetypes.yaml` currently load as raw `dict`s. Validate at startup. (~45 min.)
- **Explicit registration** — replace plugin self-registration via decorator side effects with an explicit `register_all()` call in `main.py` lifespan. (~30 min.)
- **`test_pagination_limit_max`** — pre-existing test failure: route allows `limit` up to 500, test expects 422 at 101. Fix one or the other. (5 min.)
