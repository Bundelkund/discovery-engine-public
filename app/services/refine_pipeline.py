"""Refine pipeline — the store-first state machine (Spec 11, A3).

`raw_jobs` is an append-only inbox: the fetch path inserts rows with
``status='new'`` and stops. This pipeline drives each new row through an ordered
set of steps and ends it in EXACTLY ONE terminal status:

    new ──► refining     (atomic batch claim via claim_refine_batch RPC, AUDIT-P1-04)
            ├─► refined    (survived every gate; upserted into the clean jobs shelf)
            ├─► duplicate  (exact-dup of an existing job, or a near-dup of a peer)
            ├─► rejected   (DQ rules rejected it while reject-mode is active)
            └─► new        (released for retry: upsert failed / stale-claim reclaim)

Ordered steps (each independently testable, fixed order):

  1. zerlegen (parse)   raw row -> NormalizedJob (+ content_hash, guaranteed external_id)
  2. dedup (exact)      DeduplicationService.filter_batch -> duplicate_indices
  3. dedup (near)       MinHashDedup.is_near_duplicate(description) per survivor
  4. dq-rules           RulesEngine.classify -> reject (when mode=='flag+reject') / flags
  5. location           LocationNormalizer.normalize -> location_normalized/lat/lon/remote
  6. gate+resolve       quality_gate (profile-free) -> DescriptionResolver backfill
                        under a hard wall-clock budget (AUDIT-P1-03: slow origins
                        must never hold the 'refining' claim hostage)
                        (NO scoring, NO enrich — engine is profile-agnostic; per-profile
                        scoring lives in the tenant module, enrichment is on-read)
  7. state-upsert        survivors -> ScoredJob -> JobRepository.upsert; mark 'refined'

Idempotency: the input is CLAIMED via ``RawJobRepository.fetch_new`` (atomic
status='new' → 'refining' flip; concurrent drains get disjoint batches). Terminal
rows are never re-read, so a re-run is a no-op over already-processed work.
State-machine integrity: NO raw_job is silently dropped — every fetched row is
``mark_status``'d to one terminal value (or released to 'new') before the pass
returns.
Per-row error isolation: a failure on one raw_job marks it 'rejected' and the
pass continues.

No profile_id anywhere — agnostik invariant (single-tenant clean shelf).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re

from app.config import (
    load_data_quality_config,
    load_resolution_config,
)
from app.data_quality.context import get_dq_context
from app.data_quality.minhash import MinHashDedup
from app.deduplication.dedup import DeduplicationService
from app.models.job import NormalizedJob, RawJob, ScoredJob
from app.data_quality.quality_gate import quality_gate
from app.repositories.jobs import JobRepository
from app.repositories.raw_jobs import RawJobRepository
from app.resolution.description_resolver import DescriptionResolver

logger = logging.getLogger(__name__)

# Terminal raw_jobs states (matches the CHECK constraint on raw_jobs.status).
REFINED = "refined"
REJECTED = "rejected"
DUPLICATE = "duplicate"

# AUDIT-P1-03: hard wall-clock budget for step 6b (description resolution).
# The whole batch sits claimed 'refining' (AUDIT-P1-04) while the pass runs, and
# the resolver is the only origin-dependent step — worst case without a budget
# was ~400s per pass (max_resolve=100 / concurrency=5 x timeout_s=20) held
# hostage by slow posting origins. 8s keeps the origin-dependent tail under the
# p95<10s refine-latency target; a healthy batch (few thin rows, fast origins)
# finishes in 1-3s and never hits it. Override per deploy via resolution.yaml
# `resolve_budget_s`. On budget exhaustion, fetches that already completed keep
# their fills (resolve_batch mutates job.description in place per task); the
# rest are cancelled and those rows proceed thin — the same best-effort contract
# as any single fetch failure. dedup-near is unaffected either way: step 3
# decides on the THIN text before 6b runs.
# debt: rows whose fetch the budget cancels stay thin forever ('refined' is
# terminal; resolution never retries). Upgrade-trigger: if the
# refine_resolution_budget_exhausted log fires on most passes or the shelf's
# thin-description share grows visibly, move backfill OUT of the refine pass
# into a separate scheduled drain that UPDATEs the shelf description for
# already-refined thin rows (AUDIT-P1-03 Option B).
_RESOLVE_BUDGET_SECONDS = 8.0


# Legal-form tokens stripped from company names before hashing: the SAME employer
# appears as "amberra GmbH" on one board and "amberra" on another, which would
# otherwise split the canonical hash. Keep this set in lock-step with the SQL
# backfill migrations (migrations/cross-source-dedup-content-hash.sql,
# migrations/dedup-company-noise-escape.sql) — the Python ingest hash and the
# migrated hash MUST be byte-identical or re-ingests won't match the shelf.
_LEGAL_FORMS = frozenset(
    {"gmbh", "mbh", "ag", "se", "kg", "kgaa", "ug", "ohg", "gbr",
     "co", "ev", "ltd", "llc", "inc", "plc", "holding"}
)

# Adzuna's apply-page label leaks into company.display_name ("Bewerbung als",
# "Bewerbung als Senior Transformation Manager"). That is a CTA, never an
# employer — treat it as an empty company for identity purposes.
_COMPANY_GARBAGE_PREFIX = "bewerbung als"


def _norm_hash_field(s: str) -> str:
    """Normalise a title/company for the canonical content hash.

    Lower-case, drop bracketed suffixes ("(m/w/d)", "(all genders)"), map every
    non-alphanumeric run to a single space, trim. So the SAME posting rendered
    slightly differently across boards collapses to one string.
    """
    s = (s or "").lower()
    s = re.sub(r"\(.*?\)", " ", s)       # "(m/w/d)", "(all genders)" → space
    s = re.sub(r"[^a-z0-9]+", " ", s)    # punctuation / unicode → space
    return " ".join(s.split())


def _norm_company(s: str) -> str:
    """Like _norm_hash_field but drop apply-page garbage labels entirely and
    strip legal-form tokens (GmbH/AG/…)."""
    ns = _norm_hash_field(s)
    if ns == _COMPANY_GARBAGE_PREFIX or ns.startswith(_COMPANY_GARBAGE_PREFIX + " "):
        return ""
    return " ".join(t for t in ns.split() if t not in _LEGAL_FORMS)


def _content_hash(title: str, company: str) -> str:
    """sha256(stem(title,company)|norm_company(company))[:16] — SOURCE-INDEPENDENT identity.

    url is deliberately OMITTED: the same posting scraped from N job boards
    (adzuna/linkedin/indeed/personio/…) carries N different urls but the same
    title+company. Hashing the url defeated the only cross-source dedup tier
    (DeduplicationService Tier 3), so every board produced a separate jobs_v2 row.
    location is ALSO omitted: the same job renders as "Berlin", "Berlin-Mitte",
    "Wedding, Berlin", "Berlin, Berlin, Germany" across boards — too noisy to be a
    stable identity (it splits the hash instead of collapsing it). Company legal
    forms are stripped so "amberra GmbH" == "amberra".

    Title stem (dedup-company-noise-escape): a trailing company echo is removed —
    some adzuna feed variants render "Title - Capco" while others render "Title"
    plus the company field, which split the hash for the SAME posting.

    Deliberately NOT in the hash: trailing-truncation tolerance. Aggregators cut
    long titles mid-word ("…Asset Managemen", adzuna cuts at 64 raw chars), but a
    per-row hash can only absorb that by capping every stem at a fixed prefix —
    which over-merges genuinely different postings whose titles diverge beyond
    the cap (observed on the shelf: 50 per-city "…- 1KOMMA5° <Stadt>" postings
    differ only after char ~48). Truncation is therefore handled as a COMPARISON
    in DeduplicationService (Tier 3b title-prefix probe + intra-batch collapse)
    and in the backfill migration, keyed on the exact 64-char truncation signature.
    MUST stay byte-identical to migrations/dedup-company-noise-escape.sql.
    """
    c = _norm_company(company)
    stem = _norm_hash_field(title)
    if c and stem.endswith(" " + c):
        stem = stem[: -(len(c) + 1)]
    return hashlib.sha256(f"{stem}|{c}".encode()).hexdigest()[:16]


def parse_raw(raw: RawJob | dict, default_source: str = "") -> NormalizedJob:
    """zerlegen (parse): a RawJob or a raw_jobs DB row -> NormalizedJob.

    Relocated from ``BaseScraper.normalize``. Computes content_hash and
    GUARANTEES a non-empty ``external_id`` (jobs_v2.external_id is NOT NULL):
    if the source carries none, derive a stable id from the url, else from the
    content_hash. ``default_source`` backfills an empty source field.
    """
    if isinstance(raw, RawJob):
        title = raw.title
        url = raw.url
        company = raw.company
        location = raw.location
        description = raw.description
        salary = raw.salary
        source = raw.source or default_source
        external_id = raw.external_id
        posted_at = raw.posted_at
    else:
        title = raw.get("title", "") or ""
        url = raw.get("url", "") or ""
        company = raw.get("company", "") or ""
        location = raw.get("location", "") or ""
        description = raw.get("description", "") or ""
        salary = raw.get("salary", "") or ""
        source = raw.get("source", "") or default_source
        external_id = raw.get("external_id", "") or ""
        posted_at = raw.get("posted_at")

    # Always recompute the canonical hash — do NOT honour a pre-supplied
    # existing_hash: legacy raws carry the old url-based hash, which would keep
    # cross-source duplicates apart. The canonical (title|company|location) hash
    # must win so Tier-3 dedup can collapse them.
    content_hash = _content_hash(title, company)

    # external_id guarantee — jobs_v2 upsert PK is (source, external_id) NOT NULL.
    if not external_id:
        external_id = url.strip() or content_hash

    return NormalizedJob(
        title=title,
        url=url,
        company=company,
        location=location,
        description=description,
        salary=salary,
        source=source,
        external_id=external_id,
        posted_at=posted_at,
        content_hash=content_hash,
    )


class RefinePipeline:
    """Drives raw_jobs(status='new') through the ordered refine steps.

    A single ``run()`` call fetches one batch (``fetch_new(limit)``), processes it,
    and returns a summary of terminal-state counts. Safe to call repeatedly.
    """

    def __init__(self, supabase_client) -> None:
        self.supabase = supabase_client
        self.raw_repo = RawJobRepository(supabase_client)
        self.job_repo = JobRepository(supabase_client)

        # Dedup against the active shelf. No snapshot — DeduplicationService now
        # resolves jobs_table from settings per call, so it can never diverge from
        # JobRepository's table after a config reload. (F5)
        self.dedup = DeduplicationService(supabase_client)

        # MinHash is DB-backed (no longer on DQContext) — build it here from config.
        dq_cfg = load_data_quality_config()
        self.minhash = MinHashDedup(
            supabase_client,
            threshold=dq_cfg.minhash.threshold,
            num_perm=dq_cfg.minhash.num_perm,
            band_width=dq_cfg.minhash.band_width,
            shingle_size=dq_cfg.minhash.shingle_size,
            seed=dq_cfg.minhash.seed,
            window_days=dq_cfg.dedup.window_days,
        )

        dq = get_dq_context()
        self.location_normalizer = dq.location_normalizer
        self.rules_engine = dq.rules_engine

    async def run(self, limit: int = 100) -> dict:
        """Process one batch of status='new' raw_jobs into terminal states.

        Returns ``{fetched, refined, rejected, duplicate, errors}``. Every fetched
        row is accounted for: refined + rejected + duplicate == fetched.

        Retention (dedup_memory, raw_jobs, jobs_v2) is owned by pg_cron (nightly).
        Removed inline purges per AUDIT-P1-02 to eliminate wasted DELETE scans.
        """
        rows = await self.raw_repo.fetch_new(limit=limit)
        summary = {
            "fetched": len(rows),
            "refined": 0,
            "rejected": 0,
            "duplicate": 0,
            "errors": 0,
        }
        if not rows:
            logger.info("refine_no_new_rows")
            return summary

        # endstate per raw_job id; None means "still a survivor" until upsert.
        endstate: dict[str, str | None] = {}
        survivors: list[NormalizedJob] = []
        survivor_ids: list[str] = []

        # --- Step 1: zerlegen (parse). Per-row isolation: a parse failure rejects
        #     that single row, never the batch. ---
        for row in rows:
            rid = row.get("id")
            if rid is None:
                # Unaddressable row — cannot mark_status it; skip with a loud log.
                logger.error("refine_row_missing_id", extra={"url": row.get("url", "")[:80]})
                summary["errors"] += 1
                continue
            try:
                normalized = parse_raw(row, default_source=row.get("source", "") or "")
                endstate[rid] = None
                survivors.append(normalized)
                survivor_ids.append(rid)
            except Exception as exc:  # noqa: BLE001
                logger.error("refine_parse_failed", extra={"id": rid, "error": str(exc)})
                endstate[rid] = REJECTED
                summary["errors"] += 1

        # --- Step 2: dedup (exact). duplicate_indices -> mark those raw_jobs 'duplicate'. ---
        if survivors:
            _, _, dup_indices = await self.dedup.filter_batch(survivors)
            for idx in dup_indices:
                endstate[survivor_ids[idx]] = DUPLICATE
            survivors, survivor_ids = self._compact(survivors, survivor_ids, endstate)

        # --- Step 3: dedup (near). is_near_duplicate(description) per survivor
        #     against the rolling LSH memory (cross-batch history). We do NOT add()
        #     here: the band hashes are persisted to dedup_memory only AFTER a row
        #     actually reaches the clean shelf (Step 7). Persisting eagerly meant a
        #     job whose upsert later failed left its bands behind and was wrongly
        #     dropped as a near-duplicate on the retry pass — permanent data loss
        #     for a never-stored job. (F7) ---
        near_bands: dict[str, tuple[str, str]] = {}  # rid -> (description, content_hash)
        kept: list[NormalizedJob] = []
        kept_ids: list[str] = []
        for job, rid in zip(survivors, survivor_ids):
            desc = job.description or ""
            try:
                near = await asyncio.to_thread(self.minhash.is_near_duplicate, desc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("refine_minhash_check_failed", extra={"id": rid, "error": str(exc)})
                near = False
            if near:
                endstate[rid] = DUPLICATE
                continue
            near_bands[rid] = (desc, job.content_hash)
            kept.append(job)
            kept_ids.append(rid)
        survivors, survivor_ids = kept, kept_ids

        # --- Step 4: dq-rules. classify -> reject (when reject-mode active) / flags.
        #     Surviving jobs carry their dq_flags forward via _job_flags. ---
        reject_active = self.rules_engine.mode == "flag+reject"
        job_flags: dict[str, dict] = {}
        kept, kept_ids = [], []
        for job, rid in zip(survivors, survivor_ids):
            # Per-row isolation (matches the module contract): a classify failure on
            # one row marks it 'rejected' and the pass continues, never aborting the
            # whole batch. (Batch steps dedup/scoring instead abort+retry by design.)
            try:
                verdict, flags = self.rules_engine.classify(job.model_dump())
            except Exception as exc:  # noqa: BLE001
                logger.error("refine_dq_classify_failed", extra={"id": rid, "error": str(exc)})
                endstate[rid] = REJECTED
                summary["errors"] += 1
                continue
            if verdict == "reject" and reject_active:
                endstate[rid] = REJECTED
                logger.info("refine_dq_rejected", extra={"id": rid, "flags": list(flags)})
                continue
            job_flags[rid] = flags
            kept.append(job)
            kept_ids.append(rid)
        survivors, survivor_ids = kept, kept_ids

        # --- Step 5: location. normalize -> location_normalized/lat/lon, remote/hybrid.
        #     Stored on a per-id dict, merged into ScoredJob at upsert time. ---
        loc_fields: dict[str, dict] = {}
        for job, rid in zip(survivors, survivor_ids):
            try:
                loc = self.location_normalizer.normalize(job.location or "")
            except Exception as exc:  # noqa: BLE001
                logger.warning("refine_location_failed", extra={"id": rid, "error": str(exc)})
                loc = {}
            loc_fields[rid] = loc

        # --- Step 6: gate + resolve (profile-agnostic; no scoring, no enrich). ---
        # 6a. quality_gate — profile-FREE: drop empty/garbage titles only. Replaces the
        #     old profile-coupled title_gate (which would permanently reject jobs that
        #     did not match Florian's profile — wrong for a shared multi-tenant shelf).
        #     This keeps the ATS-flood cap that title_gate also served (db-driven-slugs).
        gated: list[NormalizedJob] = []
        gated_ids: list[str] = []
        for job, rid in zip(survivors, survivor_ids):
            if not quality_gate(job.title):
                endstate[rid] = REJECTED
                logger.info("refine_quality_gated", extra={"id": rid, "title": job.title[:80]})
                continue
            gated.append(job)
            gated_ids.append(rid)
        survivors, survivor_ids = gated, gated_ids

        # 6b. DescriptionResolver — best-effort thin-description backfill (corpus value:
        #     full text via the posting's redirect URL feeds /jobs ranking; dedup-near
        #     already decided on the THIN text in step 3, bands are persisted from the
        #     thin text in 7b — resolution changes neither). AUDIT-P1-03: hard
        #     wall-clock budget so slow posting origins can never hold the pass (and
        #     its 'refining' claim) hostage. Budget exhaustion is an EXPECTED cap,
        #     not an error: partial fills are kept, unfilled rows proceed thin.
        if survivors:
            try:
                res_cfg = load_resolution_config().get("resolution", {})
                if res_cfg.get("enabled", True):
                    resolver = DescriptionResolver(res_cfg)
                    budget = float(res_cfg.get("resolve_budget_s", _RESOLVE_BUDGET_SECONDS))
                    try:
                        filled = await asyncio.wait_for(
                            resolver.resolve_batch(survivors), timeout=budget
                        )
                        logger.info("refine_descriptions_resolved", extra={"filled": filled})
                    except asyncio.TimeoutError:
                        logger.info(
                            "refine_resolution_budget_exhausted",
                            extra={"budget_s": budget, "survivors": len(survivors)},
                        )
            except Exception as exc:  # noqa: BLE001
                logger.error("refine_resolution_failed", extra={"error": str(exc)})

        # 6c. Build final shelf rows — NO scoring (per-profile scoring is the tenant's
        #     job). Merge location + dq_flags only.
        final_jobs: list[ScoredJob] = []
        final_ids: list[str] = []
        for job, rid in zip(survivors, survivor_ids):
            update = dict(loc_fields.get(rid, {}))
            update["dq_flags"] = job_flags.get(rid, {})
            try:
                sj = ScoredJob(**job.model_dump()).model_copy(update=update)
            except Exception as exc:  # noqa: BLE001
                # Leave rid unmarked (stays 'new') so the next pass retries it.
                logger.warning("refine_build_failed", extra={"id": rid, "error": str(exc)})
                continue
            final_jobs.append(sj)
            final_ids.append(rid)

        # --- Step 7: state-upsert. Survivors -> clean shelf; mark 'refined'. ---
        if final_jobs:
            try:
                results = await self.job_repo.upsert(final_jobs)
            except Exception as exc:  # noqa: BLE001
                # A whole-call upsert failure must not silently drop rows — they stay
                # 'new' (endstate None) and get retried on the next pass. Log loudly.
                logger.error("refine_upsert_failed", extra={"error": str(exc), "count": len(final_jobs)})
            else:
                # Mark 'refined' ONLY rows that actually reached the shelf. A per-row
                # upsert failure (results[i] is False) leaves that raw_job 'new' so the
                # next pass retries it — a swallowed row must never look refined.
                for ok, rid in zip(results, final_ids):
                    if ok:
                        endstate[rid] = REFINED
                    else:
                        summary["errors"] += 1
                        logger.warning("refine_upsert_row_failed", extra={"id": rid})

        # --- Step 7b: persist near-dedup bands ONLY for rows that reached the
        #     shelf. A row whose upsert failed stays 'new' and carries NO bands, so
        #     the next pass re-evaluates it instead of dropping it as a phantom
        #     near-dup. (F7) ---
        for rid in final_ids:
            if endstate.get(rid) != REFINED:
                continue
            desc, content_hash = near_bands.get(rid, ("", ""))
            if not desc:
                continue
            try:
                await asyncio.to_thread(self.minhash.add, desc, content_hash)
            except Exception as exc:  # noqa: BLE001
                logger.warning("refine_minhash_add_failed", extra={"id": rid, "error": str(exc)})

        # --- Commit terminal states. Any survivor still None means upsert failed;
        #     RELEASE it back to 'new' so the next run retries it. (AUDIT-P1-04:
        #     fetch_new now CLAIMS rows to status='refining' — merely omitting the
        #     mark would strand the row invisible until the stale reclaim window.
        #     If the release itself fails, the drain-start reclaim recovers it.) ---
        for rid, state in endstate.items():
            target = state if state is not None else "new"
            try:
                await self.raw_repo.mark_status(rid, target)
                if state is not None:
                    summary[state] += 1
            except Exception as exc:  # noqa: BLE001
                logger.error("refine_mark_status_failed", extra={"id": rid, "state": target, "error": str(exc)})
                summary["errors"] += 1

        logger.info("refine_pass_complete", extra=summary)
        return summary

    @staticmethod
    def _compact(
        jobs: list[NormalizedJob], ids: list[str], endstate: dict[str, str | None]
    ) -> tuple[list[NormalizedJob], list[str]]:
        """Keep only jobs whose endstate is still None (not yet terminal)."""
        out_jobs, out_ids = [], []
        for job, rid in zip(jobs, ids):
            if endstate.get(rid) is None:
                out_jobs.append(job)
                out_ids.append(rid)
        return out_jobs, out_ids
