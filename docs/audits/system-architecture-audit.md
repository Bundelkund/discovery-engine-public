# System Architecture Audit — discovery-engine

**Date:** 2026-05-03
**Scope:** FastAPI/Python 3.12 intake service. Read-only static analysis of `app/`, `config/`, `tests/`.
**Method:** Skill `system-architecture-audit` (10 dimensions). Evidence cited as `file:line`.

---

## Executive Summary

discovery-engine has a **clean conceptual architecture** — clear layering (routes → services → repositories → sources/scoring/enrichment), three plugin registries with self-registration via decorators, and a strict consumer boundary (X-API-Key per consumer, no direct Supabase access for clients). Phase 1/3/5 cleanup work has paid off: profile endpoints removed, query endpoint is consumer-agnostic, per-consumer auth in place.

**However**, a 12-month archaeology of half-finished migrations has left load-bearing dead code in three places:

1. `ScoringPipeline.run_stage2()` and `run_stage3()` exist as fully-implemented async methods that **nobody calls**. Stage-2/3 was officially "removed in bundle-b-phase1" (per `config/scoring.yaml:7`) but the Python code stayed.
2. `JobRepository` defines 7 async methods (`update_stage1_score`, `update_scores`, `update_stage3_score`, `get_unscored`, `get_needs_rescore`, `list_jobs`, `count_jobs`) that **only the file itself references**. Real consumers go through `query()`.
3. `ScoredJob.score_stage_2/3`, `match_reasoning`, `match_pitch`, `match_highlights` and the corresponding response-model fields are written by nobody and read by the WA Provider API as "always None". They are persistence ghosts of the deprecated stages.

The **biggest live runtime risk** is async/sync mixing: every Supabase call (sync `supabase-py` client) and the Indeed scraper (sync `python-jobspy`) and the RSS scraper (sync `feedparser`) all run **inside `async def` handlers without `run_in_threadpool`**, blocking the event loop on every request. With a single uvicorn worker this serialises throughput to one request at a time during DB I/O.

The **biggest design risk** is the `JobRepository.query()` method (162 lines, 13 keyword-args, 7 control-flow branches for filter assembly + a SQL bbox prefilter + a Python Haversine post-filter split between repo and route). The split between `repo.query()` and `routes.jobs_api.list_jobs()` for the distance-filter is fragile — the route imports a `_haversine_km` private helper from the repo module (`jobs_api.py:12` → `repositories/jobs.py:465`).

---

## Findings

### CRITICAL

#### C-1 — Sync I/O blocking the FastAPI event loop in async handlers

**Dimension 2 (async patterns), Dimension 1 (responsibility distribution)**

`supabase-py` (the only DB client) is **synchronous**. `python-jobspy` (Indeed scraper backend) and `feedparser` (RSS scraper) are also synchronous. They are all called from inside `async def` request handlers without `asyncio.to_thread()` or `run_in_threadpool()`.

Evidence:
- `app/dependencies.py:22-24` — `get_supabase()` returns sync client; injected as a FastAPI dependency into async routes.
- `app/repositories/jobs.py:115` `self.client.table(...).insert(row).execute()` runs inside `async def insert_batch` but is fully synchronous → blocks loop. Same for every `.execute()` in `repositories/jobs.py`, `repositories/companies.py`, `deduplication/dedup.py`, `routes/health.py:44`.
- `app/sources/indeed.py:25-32` — `jobspy.scrape_jobs(...)` is synchronous network I/O inside `async def fetch`. With `results_wanted=50` per term and 4 search terms (`config/sources.yaml:4`), this can block for many seconds on each `/scrape/indeed` call.
- `app/sources/rss.py:27` — `feedparser.parse(feed_url)` is sync HTTP+parse inside `async def fetch`.
- `app/scoring/pipeline.py:78` — Hidden complication: `run_stage1` declares `async def` but inside `for job in jobs: for scorer in stage1_scorers: await scorer.score(...)`. `KeywordScorer.score()` is purely CPU-bound (`scoring/keyword.py:48-99`); the `await` adds nothing. Cosmetic, but indicates a missing distinction between async (I/O) and CPU paths.

**Impact:**
- Single uvicorn worker → throughput equals 1 concurrent in-flight DB call. Health checks queue behind scrapes.
- Long scrape requests (Indeed × 4 search terms = potentially 30-60s of blocking) will time out concurrent requests rather than yielding.
- Tests pass because they mock the supabase client — production behavior is untested.

**Fix path:** Wrap blocking calls in `await asyncio.to_thread(...)` OR move the orchestrator to a background queue (Phase-shift to fire-and-forget pattern: route returns 202 with a job ID, worker processes). The latter aligns with this skill's origin lesson (Kamran: "120s sync waits → fire-and-forget").

---

#### C-2 — Dead-code from incomplete Stage 2/3 deprecation (Migration Completeness, Dim 7c)

**Dimension 7c (migration completeness)**

`config/scoring.yaml:7` says: *"Stages 2 (embedding) and 3 (LLM) were removed in bundle-b-phase1. Consumer-side scoring will be injected via the Phase 3 Query-API."* The YAML config is correctly stripped (only `stage: 1` remains). The Python code, however, was not.

Evidence (all dead):
- `app/scoring/pipeline.py:48-118` — `ScoringPipeline.run_stage2()` and `run_stage3()` defined but never called by `scrape_orchestrator.py` or anything else (verified via repo-wide grep — only references are inside `pipeline.py` itself and the knowledge-graph.json).
- `app/repositories/jobs.py:127-160` — `update_stage1_score()`, `update_scores()` (stage-2), `update_stage3_score()` defined; `update_scores`/`update_stage3_score` never called anywhere; `update_stage1_score` never called anywhere either (orchestrator inserts via `insert_batch` only).
- `app/repositories/jobs.py:162-199` — `get_unscored()` and `get_needs_rescore()` defined; never called.
- `app/repositories/jobs.py:203-292` — `list_jobs()` and `count_jobs()` defined (the old paginated-scored-list path); never called by routes (the live path is `query()`).
- `app/models/job.py:51-58` — `ScoredJob.score_stage_2`, `score_stage_3`, `match_reasoning`, `match_highlights`, `match_pitch` written by `pipeline.run_stage{2,3}` only (i.e., never).
- `app/models/responses.py:21-25, 47-48, 92-93` — `ScoreResponse.stage1_passed/stage2_triggered/stage3_triggered` and `score_stage_2/3` echoed in `JobListItem`/`JobDetailResponse`. The /jobs API forwards them to consumers as always-None.
- `app/routes/jobs_api.py:23-27, 60-63, 98-99` — `_compute_final_score()` falls through stage_3 → stage_2 → stage_1; in production stages 2/3 are always None so it is equivalent to `row.get("score_stage_1") or 0.0`.

**Impact:**
- ~250 LOC of dead code in the file deemed "the God module" (`repositories/jobs.py`, 529 lines). Removing the dead methods drops it to ~280 lines.
- API consumers see a contract that promises three scoring stages but only stage-1 ever populates. Breaking-change risk if a consumer starts depending on `score_stage_2`/`score_stage_3` thinking they will be filled later.
- Confuses new developers reading the codebase: was Phase-3 not delivered yet? (It was; the cleanup just stopped at the YAML.)

**Fix:** Delete `run_stage2`, `run_stage3`, the unused repo methods, and either drop the response fields or document them as "reserved for consumer-side scoring".

---

#### C-3 — `query()` method is the new God: 162 lines, 13 params, mixed concerns

**Dimension 1 (responsibility distribution)**

`app/repositories/jobs.py:296-457` — `JobRepository.query()` does:
1. Sanitize PostgREST operators in keyword args (line 334-352, helper `_safe` defined twice)
2. Build OR-clause for `keywords_positive`
3. Apply `not.or_` for each `keywords_negative` (per-keyword loop because supabase-py has no NOT-OR)
4. ILIKE `location`
5. Compute `scraped_at` cutoff for `max_age_days`
6. NOT-equal filter for `exclude_domain`
7. `IN` whitelists for `source` and `company_domain`
8. Seniority synonym map and ILIKE filter
9. Salary range with NULL-tolerance hack (`gte` AND `not.is.null` chained)
10. **Geocode the `location` string** (line 410, calls module-private `_geocode_city`)
11. Compute lat/lon delta from `max_distance_km` and `cos(lat)`
12. Apply SQL bounding-box prefilter
13. Sort + paginate
14. Run query, return rows + count

Then `routes/jobs_api.py:194-213` runs the **second half** of the distance filter (Haversine post-filter) by importing `_geocode_city` and `_haversine_km` directly from `repositories/jobs.py:12`.

Evidence of layer-violation:
- `app/routes/jobs_api.py:12` `from app.repositories.jobs import JobRepository, _geocode_city, _haversine_km` — route imports private helpers from repo module.
- `app/repositories/jobs.py:475-516` — A 41-entry hardcoded German-cities dict lives in the repository file, not in `data/` or `data_quality/location.py` (which already has a real GeoNames CSV loader).

**Impact:**
- The `_DE_CITIES` dict and `_haversine_km` duplicate functionality that already exists in `app/data_quality/location.py` (GeoNames-CSV-loaded `LocationNormalizer`). Two sources of "city → coords", will drift.
- Route has business logic (Haversine refinement). Repository has business logic (geocoding). Both should live in a `LocationFilter` service.
- The "consumer-agnostic Query API" was supposed to replace stage 2/3 scoring with consumer-side filtering — but the repository now carries 13 filter parameters that should be SQL-only. The mixing of "filter assembly" + "geocoding" + "post-filter contract" makes the boundary porous.

**Fix:**
- Extract a `JobQueryBuilder` class that takes a Pydantic `JobQueryFilters` model and returns the prepared supabase query.
- Move `_DE_CITIES`/`_geocode_city` into `data_quality/location.py` and reuse `LocationNormalizer`.
- Move Haversine post-filter into a `LocationFilter` service callable from both route and repo.

---

### HIGH

#### H-1 — `ScrapeOrchestrator` reloads YAML config on every request

**Dimension 7 (configuration vs code)**

`app/services/scrape_orchestrator.py:57, 151, 181` — `load_sources_config()`, `load_scoring_config()`, `load_enrichment_config()` are called per scrape, and `app/config.py:50-59` does **not** wrap them in `@lru_cache` (only `get_settings()` and `load_data_quality_config()` are cached).

Evidence:
- `app/config.py:50` `def load_sources_config() -> dict: return load_yaml("sources.yaml")` — bare function, no cache.
- `app/scoring/keyword.py:42-46` — `KeywordScorer` lazy-loads archetypes only on first access (good pattern), then caches in `self._archetypes`. But a fresh `KeywordScorer` is built on every scrape (`scoring/pipeline.py:18`), so the cache is per-scrape.

**Impact:** ~5 YAML disk reads per scrape; OK for the current low traffic (one scrape per cron tick) but wasteful and a foot-gun if scrape rate goes up.

**Fix:** `@lru_cache` on the four `load_*_config()` helpers. Add a `cache_clear()` testing hook similar to `reset_dq_context()`.

---

#### H-2 — Registry pattern: import-side-effect ordering is fragile and opaque

**Dimension 7c (migration completeness), Dimension 1 (responsibility distribution)**

`app/main.py:6-9` does `from app.sources import *`, `from app.scoring import *`, `from app.enrichment import *` to trigger `@SourceRegistry.register(...)` decorators. The `__init__.py` files (`app/sources/__init__.py:1-7`) re-export the modules to ensure the decorators run.

Evidence:
- `app/main.py:6-9` — three star-imports purely for side-effects, with `# noqa: F401, F403` to silence linter.
- `app/sources/__init__.py:1-7` — `from app.sources import indeed as indeed` (and 6 more) — purely to trigger registration.
- `tests/test_registry.py:3-9` — the test suite **must** import each adapter individually before checking the registry. Adding a new source means editing 3 places (`__init__.py`, `test_registry.py`, and adding the file).
- `app/main.py:27-31` — Routes are dynamically created **inside the lifespan** for each registered source. If a source registration fails silently (e.g. import error in one adapter), the route also disappears with no error reported.
- `SourceRegistry.register()` raises `ValueError("Already registered: ...")` (`registry/source_registry.py:8-10`) — duplicate-detection is good. But ordering is uncontrolled: which adapter wins if two register the same id? Whichever imports first.

**Impact:**
- Silent failure mode: an `ImportError` in one adapter (e.g. `python-jobspy` missing) removes its route at lifespan time without a startup error log. The /health endpoint will list registered sources but won't say which expected sources failed.
- Adding a source is a 3-step ritual; the `__init__.py` re-export is invisible coupling.

**Fix:**
- Replace decorator-side-effect with explicit registration in `main.py`'s lifespan: `SourceRegistry.register("indeed", IndeedScraper)` etc. Failed import becomes a startup error, not a silent missing route.
- OR: add a startup check that compares `SourceRegistry.registered_ids()` to an expected set from `config/sources.yaml` and logs warnings for missing.

---

#### H-3 — `config.py` carries three unrelated responsibilities

**Dimension 1 (responsibility distribution)**

`app/config.py` contains:
1. `Settings` Pydantic-Settings env loader (lines 13-23)
2. Generic `load_yaml(name)` + per-config helpers (lines 31-63)
3. `DataQualityConfig` Pydantic model + `load_data_quality_config()` validator (lines 71-101)

Inconsistency: only `data-quality.yaml` gets a Pydantic schema; `sources.yaml`, `scoring.yaml`, `enrichment.yaml`, `archetypes.yaml`, `api-keys.yaml`, `portals.yaml` are returned as raw `dict`s and accessed via `.get("foo", default)` everywhere. This is the source of e.g. `scrape_orchestrator.py:151` `load_scoring_config().get("scoring", {})` — defensive, but no validation, no type-safety.

**Impact:**
- Schema drift: a typo in `scoring.yaml` (e.g. `scoreing:` instead of `scoring:`) silently produces an empty config and disables scoring (`store_threshold` falls to default 30, scorer-list empty).
- Mixing of config concerns makes the file hard to import without pulling Pydantic models into modules that just need `Settings`.

**Fix:**
- Split `app/config.py` into `app/config/settings.py` (Settings only), `app/config/loader.py` (generic YAML), `app/config/schemas/` (per-yaml Pydantic models).
- Promote the other YAMLs to validated Pydantic models incrementally.

---

#### H-4 — `JobRepository.insert_batch()` does N round-trips to swallow uniqueness conflicts

**Dimension 2 (timeout/failure handling), Dimension 5 (data flow)**

`app/repositories/jobs.py:112-125` — `insert_batch` loops one row at a time through `self.client.table(...).insert(row).execute()`, catching `23505` (unique violation) per row. With 50 jobs from Indeed, that's 50 sync round-trips to Supabase per scrape, each blocking the event loop.

Evidence:
- The dedup pass already runs (`deduplication/dedup.py:11-83`, batched 3-tier check), so duplicates *should* be filtered. But if a parallel scrape inserts the same URL between dedup-check and insert, this protects against it. The cost is real per-job round-trips.

**Impact:** Scraping 50-200 jobs amplifies the C-1 event-loop blocking proportionally.

**Fix:** Use `.upsert(rows, on_conflict="url", ignore_duplicates=True)` and a single round-trip. Move the partial-failure handling out of the hot loop.

---

#### H-5 — Tests do not cover the registry pattern's failure modes

**Dimension 1 (testability)**

`tests/test_registry.py` imports each adapter explicitly and checks `*_Registry.registered_ids()`. It does **not** test:
- Behavior when an adapter import fails (ImportError → silent missing route)
- Behavior when two adapters register the same id (the `ValueError` branch in `register()`)
- The `app.main.lifespan` route auto-registration loop

`test_scrape_orchestrator.py` mocks `SourceRegistry.get` (`tests/test_scrape_orchestrator.py:46`) — perfectly fine for orchestrator tests, but means the registry → orchestrator hand-off is tested only via a mock.

**Impact:** Refactoring the registry to explicit-registration is risky without tests. A regression in `main.py`'s lifespan (e.g. an early `return` in the loop) would not be caught.

**Fix:** Add `tests/test_main_lifespan.py` that boots the FastAPI app via `TestClient` and asserts that for each entry in `SourceRegistry.registered_ids()`, a `POST /scrape/{id}` route exists.

---

### MEDIUM

#### M-1 — `ScrapeOrchestrator.run()` is a 170-line procedural pipeline with implicit failure semantics

**Dimension 2 (timeout/failure handling), Dimension 1 (responsibility distribution)**

`app/services/scrape_orchestrator.py:37-207` — Steps 1, 3, 4, 4b, 4c, 5, 6, 7 (yes, the comments enumerate them out of order — there is no step 2). Failure handling is mixed:

- Step 7 (enrichment) is wrapped in its own try/except → "errors.append, continue" pattern (line 196-198).
- Steps 3-6 share an outer try/except → "errors.append, raise" (line 200-203). So a Greenhouse outage during step 3 produces an HTTP 500, but a Hunter outage in step 7 is logged and swallowed.
- The MinHash filter in step 4b mutates a counter on the response (`response.jobs_duplicate += 1`, line 94) but the dedup count from step 4 is also written there (line 78). Both contributions are summed silently.
- Step 4c uses `model_copy(update=job_dict)` (line 135) inside a `try`/`except Exception: enriched = job` — silent fallback to un-enriched job if Pydantic validation fails. No log line on the silent path.

**Impact:** Hard to predict what a partial scrape returns. Hard to debug "why is jobs_stored < jobs_new?" when DQ silently rejects rows and dedup-count includes both URL-dedup and MinHash-dedup.

**Fix:**
- Extract each step into a named method (`_normalize`, `_dedup`, `_minhash_filter`, `_dq_filter`, `_score`, `_store`, `_enrich`).
- Add named counters per stage in `ScrapeResponse` (e.g. `jobs_minhash_filtered: int`, `jobs_dq_rejected: int`) instead of overloading `jobs_duplicate`.

---

#### M-2 — Per-request `create_client(supabase_url, supabase_key)` instead of singleton

**Dimension 7 (configuration vs code)**

`app/dependencies.py:22-24` — `get_supabase()` builds a fresh `Client` on every request (no `@lru_cache`). For supabase-py v2 this opens a fresh httpx client + session per request.

**Impact:** Adds ~10-30ms TLS handshake per request, plus garbage-collection pressure.

**Fix:** Add `@lru_cache` to `get_supabase()` (the client is thread-safe; only test isolation requires reset, which can be done via `dependency_overrides`).

---

#### M-3 — `_DE_CITIES` city list duplicates `LocationNormalizer` (GeoNames CSV)

**Dimension 7a (config source consistency)**

Already noted in C-3. Standalone medium-severity issue: two sources of "German city → lat/lon":
- `app/repositories/jobs.py:475-516` — 41 hardcoded cities for the `query()` distance filter.
- `app/data_quality/location.py:65-195` — full GeoNames CSV loader with alternate-name indexing, used by `ScrapeOrchestrator` for normalization at write-time.

If GeoNames CSV is updated (e.g. new city) the repo's distance filter won't see it. If a query asks for `max_distance_km` from a non-top-40 city, it returns coords=None and the filter is silently skipped (`jobs_api.py:196-200` does log a warning, but the SQL prefilter in `repositories/jobs.py:411` produces incorrect bounds).

**Fix:** Have `query()` call `LocationNormalizer.normalize(location)` and use `location_lat`/`location_lon` from the normalized result.

---

#### M-4 — `routes/scrape.py` constructs an orchestrator per request

**Dimension 7 (configuration vs code), Dimension 2 (failure handling)**

`app/routes/scrape.py:24` — `ScrapeOrchestrator(supabase)` is built fresh per request. The orchestrator's `__init__` (`scrape_orchestrator.py:25-35`) builds three repositories, the dedup service, and reads the DQ singleton. Cheap but unnecessary, and means the scorer pipeline is not reusable across requests (`scrape_orchestrator.py:152` builds a new `ScoringPipeline` every time, and the keyword scorer's archetype cache resets).

**Fix:** Build the orchestrator at app startup (`lifespan`) and inject as a dependency. Or memoise the `ScoringPipeline` and `KeywordScorer.archetypes` at module level.

---

#### M-5 — `enrich.py` has a Pydantic-construction footgun

**Dimension 6 (validation boundaries)**

`app/routes/enrich.py:17` — `CompanyProfile(domain=domain, **(existing or {}))`. `existing` is a dict from Supabase that may contain extra fields not in the Pydantic model (e.g. `id`, `created_at`, `enriched_at`). Pydantic v2 by default raises on extra fields if model has no `model_config = ConfigDict(extra="allow")`.

The check `app/models/company.py` is not shown in the audit but the convention should be verified: if `CompanyProfile` doesn't allow extras, this route 500s on first call against an existing row. If it does allow extras, then we're carrying DB-only fields into the enrichment pipeline silently.

**Fix:** Pass an explicit field-projection from `existing` to `CompanyProfile.__init__`, or add `model_config = ConfigDict(extra="ignore")` to the model.

---

#### M-6 — `count_jobs` and `query()` duplicate filter assembly logic

**Dimension 1 (responsibility distribution)**

`repositories/jobs.py:215-282` (`list_jobs` + `count_jobs`) and `repositories/jobs.py:296-457` (`query()`) both build supabase filter chains for keyword/source/score/etc. Even though `list_jobs`/`count_jobs` are dead (per C-2), the pattern means future filter additions risk being applied to one path and forgotten in the other.

**Fix:** Delete `list_jobs`/`count_jobs` (dead anyway). For `query()`, extract a private `_apply_filters(q, filters: JobQueryFilters)` helper.

---

### LOW

#### L-1 — `Settings.de_api_key` is dead

`app/config.py:16-17` — comment says "removed in Phase 5 — replaced by per-consumer keys"; field is kept as `de_api_key: str = ""` for backward compat. Verify no env still depends on it; if so, remove.

#### L-2 — Logging is `logging.basicConfig(level=INFO)` only

`app/main.py:18-21` — no JSON formatter, no correlation-id middleware, no structured fields besides what each call passes via `extra=...`. The `dependencies.py:70` "request_authenticated" log uses `extra={"consumer_id": ...}` but no request_id is propagated through the orchestrator. Hard to trace one consumer's request through scrape → dedup → store.

**Fix:** Add `python-json-logger` or `structlog` and a request-id middleware.

#### L-3 — `Dockerfile` does `pip install --no-cache-dir .` then `COPY . .` after — this is fine for Python but doesn't cache layers well.

`Dockerfile:5-8`. Standard optimization: COPY `pyproject.toml` and dependencies first, then COPY source. Currently both are done before `COPY . .`, so any source change invalidates the install layer. Minor build-speed loss.

#### L-4 — `keyword.py:18` has subtle word-boundary bug

`app/scoring/keyword.py:13-18` — `_word_match` uses `\b{kw}\b` for short keywords and `\b{kw}` (no trailing `\b`) for longer ones. This is intentional to match "developer" inside "developers", but it also matches "AI" inside "AI/ML" via the short branch (`<= 3 chars`). For e.g. keyword "AI", "AIaaS" would match — likely unintended.

#### L-5 — `_safe()` defined twice in `query()` (jobs.py:334, jobs.py:351)

Cosmetic; `# noqa: F811` already applied.

---

## Strengths

### S-1 — Strict consumer boundary

`app/dependencies.py:55-71` — every protected route requires `X-API-Key`, mapped via `config/api-keys.yaml` to a `ConsumerIdentity` with id/name/scopes. No path lets a consumer bypass the FastAPI layer to talk to Supabase directly. `tests/test_auth_per_consumer.py` validates this. **This is exactly the green pattern from the SKILL's Dim-1.** WonderApply cannot reach into the shared Supabase — they have to go through `/jobs`, `/companies/{domain}`, `/scrape/{source}`.

### S-2 — Plugin registries cleanly separated from registrants

`app/registry/source_registry.py`, `scorer_registry.py`, `enricher_registry.py` — each is a 26-line class-method pattern. They sit in their own module with no domain knowledge. Adapters depend on the registries, registries don't depend on adapters. The decorator pattern + duplicate-detection via `ValueError` is clean.

### S-3 — DQ singleton via `get_dq_context()`

`app/data_quality/context.py:64-78` — single source of truth for MinHash LSH, GeoNames normalizer, RulesEngine. Both `/health` and `ScrapeOrchestrator` read from it, so coverage metrics never drift from what dedup actually saw. The `reset_dq_context()` testing hook is the right shape.

### S-4 — Lazy-loading of archetypes in `KeywordScorer`

`app/scoring/keyword.py:42-46` — archetypes loaded on first `.archetypes` access, cached on the instance. Avoids YAML reads during scorer construction.

### S-5 — Phase-1 endpoint removal is test-enforced

`tests/test_removed_endpoints.py` is a **regression guard** that asserts old endpoints (`POST /profiles`, `POST /score/batch`, `POST /discover/opportunities`) return 404. This is the right pattern after a contract-breaking refactor — without it, a future merge of legacy code goes unnoticed.

### S-6 — Activation-date pattern for DQ rules

`app/data_quality/rules.py:51-93`, `compute_activation_date()` — first run writes `data/dq_rules_activation.txt` with a future date; subsequent runs read it. Allows safe rollout of `reject` rules with grace period, and `RulesEngine.mode` ("flag-only" vs "flag+reject") is observable via `/health`. Good operational hygiene.

### S-7 — `MinHashDedup` properly removes rejected jobs

`app/services/scrape_orchestrator.py:128-131` — when DQ rejects a job, the orchestrator removes it from the LSH index (`MinHashDedup.remove`). Without this, rejected content would block legitimate near-duplicates and the LSH index would grow unboundedly. This is the kind of "would have been a memory leak" detail that's easy to miss; the comment "F6: prevent memory leak + false-positive drift" shows it was caught deliberately.

### S-8 — PostgREST operator sanitization

`app/repositories/jobs.py:228-230, 334-352` — keyword args are stripped of `,()`. before being interpolated into PostgREST `or_(...)` strings. Prevents filter injection. The pattern should be extracted into a shared `_safe()` helper.

---

## Recommendations (Prioritized)

### P0 — This sprint

1. **Resolve event-loop blocking (C-1).** Either:
   - Wrap every `self.client.table(...).execute()` in `asyncio.to_thread(...)` (≈30 call sites; mechanical) OR
   - Switch to `supabase-py`'s async client (if/when supported) OR
   - Move the orchestrator to a Celery/Arq queue and return 202 from `/scrape/*`. **Recommended for `/scrape/*` specifically; the read-only `/jobs` and `/companies/{domain}` can stay sync-via-threadpool.**

2. **Delete dead Stage-2/3 code (C-2).** Remove `run_stage2`, `run_stage3`, `update_scores`, `update_stage3_score`, `get_unscored`, `get_needs_rescore`, `list_jobs`, `count_jobs`, the unused `ScoredJob` fields, and the response-model fields. Add deprecation notes to the OpenAPI spec for `score_stage_2/3` if WonderApply already consumes them. Drops `repositories/jobs.py` from 529 → ~280 lines.

3. **Cache YAML loaders (H-1).** Five-line fix: `@lru_cache` on `load_sources_config`, `load_scoring_config`, `load_enrichment_config`, `load_archetypes_config`. Add `cache_clear()` calls to test fixtures.

### P1 — Next sprint

4. **Extract `JobQueryBuilder` and reuse `LocationNormalizer` (C-3, M-3).** Replace `_DE_CITIES` and `_geocode_city`/`_haversine_km` in `repositories/jobs.py` with calls to `data_quality/location.py`. Move Haversine post-filter into a `LocationFilter` service used by both repo and route. Eliminates the private-helper import in `routes/jobs_api.py:12`.

5. **Switch registries to explicit registration (H-2).** Replace `from app.sources import *` star-imports with explicit `SourceRegistry.register("indeed", IndeedScraper)` calls in `main.py` lifespan. ImportErrors become startup errors. Add `tests/test_main_lifespan.py`.

6. **Validate all YAML configs with Pydantic (H-3).** Promote `sources.yaml`, `scoring.yaml`, `enrichment.yaml`, `api-keys.yaml`, `archetypes.yaml` to typed schemas in `app/config/schemas/`. Adds startup safety, catches typos.

### P2 — Backlog

7. **Refactor `ScrapeOrchestrator.run()` (M-1).** One method per pipeline step, named counters in `ScrapeResponse`, explicit per-step error policy (raise vs swallow). Targets the clarity-of-failure problem, not performance.

8. **Cache the supabase client (M-2)** and the orchestrator (M-4). Add structured logging with request-ids (L-2).

9. **Replace `insert_batch` per-row inserts with `upsert` (H-4).** One round-trip instead of N. Combine with C-1 fix for compounded effect.

---

## Open Questions for Architect Review

- Is the `score_stage_2/3` field exposed to WonderApply already, and if so, can we deprecate it (response shape change) or do we need a transitional period?
- Is `/scrape/*` ever called by an end-user-facing path that needs <1s response, or is it always a cron/n8n fire? If always cron, fire-and-forget is overkill — sync-via-threadpool is enough.
- Why is `JH_API_KEY` consumer `active: false` (`config/api-keys.yaml:11`)? If JobHunt is no longer a consumer, the dual-consumer assumption in tests can be simplified.
- Does the read-vs-write separation hold in WonderApply's actual usage, or has WA been seen reaching into Supabase tables directly bypassing the API?
