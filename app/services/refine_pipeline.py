"""Refine pipeline — the store-first state machine (Spec 11, A3).

`raw_jobs` is an append-only inbox: the fetch path inserts rows with
``status='new'`` and stops. This pipeline drives each new row through an ordered
set of steps and ends it in EXACTLY ONE terminal status:

    new ──► refined      (survived every gate; upserted into the clean jobs shelf)
        ├─► duplicate    (exact-dup of an existing job, or a near-dup of a peer)
        └─► rejected     (DQ rules rejected it while reject-mode is active)

Ordered steps (each independently testable, fixed order):

  1. zerlegen (parse)   raw row -> NormalizedJob (+ content_hash, guaranteed external_id)
  2. dedup (exact)      DeduplicationService.filter_batch -> duplicate_indices
  3. dedup (near)       MinHashDedup.is_near_duplicate(description) per survivor
  4. dq-rules           RulesEngine.classify -> reject (when mode=='flag+reject') / flags
  5. location           LocationNormalizer.normalize -> location_normalized/lat/lon/remote
  6. score+gate+enrich  title_gate -> ScoringPipeline.run_stage1 -> resolve -> enrich
  7. state-upsert        survivors -> ScoredJob -> JobRepository.upsert; mark 'refined'

Idempotency: the input is read via ``RawJobRepository.fetch_new`` (status='new'
only). Terminal rows are never re-read, so a re-run is a no-op over already-
processed work. State-machine integrity: NO raw_job is silently dropped — every
fetched row is ``mark_status``'d to one terminal value before the pass returns.
Per-row error isolation: a failure on one raw_job marks it 'rejected' and the
pass continues.

No profile_id anywhere — agnostik invariant (single-tenant clean shelf).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from urllib.parse import urlparse

from app.config import (
    load_data_quality_config,
    load_enrichment_config,
    load_resolution_config,
    load_scoring_config,
    load_scoring_profile,
)
from app.data_quality.context import get_dq_context
from app.data_quality.minhash import MinHashDedup
from app.deduplication.dedup import DeduplicationService
from app.enrichment.pipeline import EnrichmentPipeline
from app.models.company import CompanyProfile, EnrichmentContext
from app.models.job import NormalizedJob, RawJob, ScoredJob
from app.repositories.companies import CompanyRepository
from app.repositories.jobs import JobRepository
from app.repositories.raw_jobs import RawJobRepository
from app.resolution.description_resolver import DescriptionResolver
from app.scoring.pipeline import ScoringPipeline
from app.scoring.storage_gate import title_gate
from app.scoring.types import ScoringProfile

logger = logging.getLogger(__name__)

# Terminal raw_jobs states (matches the CHECK constraint on raw_jobs.status).
REFINED = "refined"
REJECTED = "rejected"
DUPLICATE = "duplicate"


def _content_hash(url: str, title: str, company: str) -> str:
    """sha256(url|title|company)[:16] — the canonical content hash (was BaseScraper)."""
    content = f"{url}|{title}|{company}".lower().strip()
    return hashlib.sha256(content.encode()).hexdigest()[:16]


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
        existing_hash = raw.content_hash
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
        existing_hash = raw.get("content_hash", "") or ""

    content_hash = existing_hash or _content_hash(url, title, company)

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
        self.company_repo = CompanyRepository(supabase_client)

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

        # Single-user profile (optional) — empty profile keeps all titles / scores 0.
        self.profile = load_scoring_profile() or ScoringProfile(id="")

    async def run(self, limit: int = 100) -> dict:
        """Process one batch of status='new' raw_jobs into terminal states.

        Returns ``{fetched, refined, rejected, duplicate, errors}``. Every fetched
        row is accounted for: refined + rejected + duplicate == fetched.
        """
        # Retention: drop dedup_memory rows older than the window so the table
        # stays bounded. Best-effort — a purge failure must not block the pass.
        # (Read-path correctness is already window-filtered in is_near_duplicate.)
        try:
            purged = await asyncio.to_thread(self.minhash.purge_old)
            if purged:
                logger.info("refine_dedup_purged", extra={"deleted": purged})
        except Exception as exc:  # noqa: BLE001
            logger.warning("refine_dedup_purge_failed", extra={"error": str(exc)})

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

        # --- Step 6: score + gate + enrich (preserved from the old orchestrator). ---
        # 6a. title_gate — drop titles with no profile signal; priority -> dq_flags.
        gated: list[NormalizedJob] = []
        gated_ids: list[str] = []
        for job, rid in zip(survivors, survivor_ids):
            keep, priority = title_gate(job.title, self.profile)
            if not keep:
                endstate[rid] = REJECTED
                logger.info("refine_title_gated", extra={"id": rid, "title": job.title[:80]})
                continue
            if priority:
                job_flags.setdefault(rid, {})["priority"] = True
            gated.append(job)
            gated_ids.append(rid)
        survivors, survivor_ids = gated, gated_ids

        # 6b. DescriptionResolver — best-effort thin-description backfill (before scoring).
        if survivors:
            try:
                res_cfg = load_resolution_config().get("resolution", {})
                if res_cfg.get("enabled", True):
                    resolver = DescriptionResolver(res_cfg)
                    filled = await resolver.resolve_batch(survivors)
                    logger.info("refine_descriptions_resolved", extra={"filled": filled})
            except Exception as exc:  # noqa: BLE001
                logger.error("refine_resolution_failed", extra={"error": str(exc)})

        # 6c. Stage-1 scoring + threshold filter.
        scored_by_id: dict[str, ScoredJob] = {}
        if survivors:
            scoring_cfg = load_scoring_config().get("scoring", {})
            pipeline = ScoringPipeline(scoring_cfg)
            scored = await pipeline.run_stage1(survivors, self.profile)
            kept_scored, _below = pipeline.filter_by_threshold(scored)
            kept_set = {id(j) for j in kept_scored}
            # Map back to raw_job ids by positional alignment (run_stage1 preserves order).
            below_ids: list[str] = []
            for sj, rid in zip(scored, survivor_ids):
                if id(sj) in kept_set:
                    scored_by_id[rid] = sj
                else:
                    below_ids.append(rid)
            for rid in below_ids:
                endstate[rid] = REJECTED
            survivor_ids = [rid for rid in survivor_ids if rid in scored_by_id]

        # 6d. Build final ScoredJobs (merge location + dq_flags) for the upsert.
        final_jobs: list[ScoredJob] = []
        final_ids: list[str] = []
        for rid in survivor_ids:
            sj = scored_by_id[rid]
            update = dict(loc_fields.get(rid, {}))
            update["dq_flags"] = job_flags.get(rid, {})
            try:
                sj = sj.model_copy(update=update)
            except Exception as exc:  # noqa: BLE001
                logger.warning("refine_merge_failed", extra={"id": rid, "error": str(exc)})
            final_jobs.append(sj)
            final_ids.append(rid)

        # 6e. Enrich new company domains (best-effort, no profile_id).
        await self._enrich(final_jobs)

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
        #     leave it 'new' (omit mark_status) so the next run retries it. ---
        for rid, state in endstate.items():
            if state is None:
                continue
            try:
                await self.raw_repo.mark_status(rid, state)
                summary[state] += 1
            except Exception as exc:  # noqa: BLE001
                logger.error("refine_mark_status_failed", extra={"id": rid, "state": state, "error": str(exc)})
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

    async def _enrich(self, jobs: list[ScoredJob]) -> None:
        """Enrich newly-seen company domains. Best-effort; failures are swallowed.

        No profile_id in EnrichmentContext (agnostik). Mirrors the old orchestrator
        step 7: collect unique domains, enrich those needing it, upsert results.
        """
        if not jobs:
            return
        try:
            domains: set[str] = set()
            to_enrich: list[CompanyProfile] = []
            for job in jobs:
                domain = self._extract_domain(job)
                if domain and domain not in domains:
                    domains.add(domain)
                    if await self.company_repo.needs_enrichment(domain):
                        to_enrich.append(CompanyProfile(domain=domain, name=job.company))

            if not to_enrich:
                return

            enrichment_cfg = load_enrichment_config().get("enrichment", {})
            enrich_pipeline = EnrichmentPipeline(enrichment_cfg)
            ctx = EnrichmentContext(jobs=[j.model_dump() for j in jobs])
            enriched = await enrich_pipeline.run(to_enrich, ctx)
            for company in enriched:
                await self.company_repo.upsert(company)
            logger.info("refine_enriched", extra={"companies": len(enriched)})
        except Exception as exc:  # noqa: BLE001
            logger.error("refine_enrichment_failed", extra={"error": str(exc)})

    @staticmethod
    def _extract_domain(job: ScoredJob) -> str:
        """Best-effort company domain from an explicit field or the posting URL."""
        domain = getattr(job, "company_domain", "") or ""
        if domain:
            return domain
        url = getattr(job, "url", "") or ""
        if not url:
            return ""
        try:
            host = urlparse(url).hostname or ""
            if "greenhouse.io" in host:
                parts = (
                    url.split("/boards/") if "/boards/" in url else url.split("greenhouse.io/")
                )
                if len(parts) > 1:
                    return parts[1].split("/")[0] + ".com"
            elif host and "indeed" not in host and "adzuna" not in host:
                return host.replace("www.", "")
        except Exception:  # noqa: BLE001
            pass
        return ""
