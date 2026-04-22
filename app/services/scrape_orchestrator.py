import logging
import time
from urllib.parse import urlparse

from app.config import (
    load_enrichment_config,
    load_scoring_config,
    load_sources_config,
)
from app.data_quality.context import get_dq_context
from app.deduplication.dedup import DeduplicationService
from app.enrichment.pipeline import EnrichmentPipeline
from app.models.company import CompanyProfile, EnrichmentContext
from app.models.responses import ScrapeResponse
from app.registry.source_registry import SourceRegistry
from app.repositories.companies import CompanyRepository
from app.repositories.jobs import JobRepository
from app.scoring.pipeline import ScoringPipeline
from app.scoring.types import ScoringProfile

logger = logging.getLogger(__name__)


class ScrapeOrchestrator:
    def __init__(self, supabase_client):
        self.supabase = supabase_client
        self.job_repo = JobRepository(supabase_client)
        self.company_repo = CompanyRepository(supabase_client)
        self.dedup = DeduplicationService(supabase_client)

        # Shared DQ context (singleton) — keeps /health and orchestrator in sync
        dq = get_dq_context()
        self._minhash = dq.minhash
        self._location_normalizer = dq.location_normalizer
        self._rules_engine = dq.rules_engine

    async def run(
        self,
        source_id: str,
        profile_id: str | None = None,
        location: str = None,
        limit: int = None,
        store: bool = True,
    ) -> ScrapeResponse:
        start = time.time()
        errors: list[str] = []
        response = ScrapeResponse(source=source_id, profile_id=profile_id or "")

        # Profile loading is handled by the consumer layer (e.g. WonderApply).
        # The orchestrator uses an empty ScoringProfile so stage-1 keyword
        # scoring runs without error; scores will be low until the consumer
        # injects real profile data via the Phase 3 Query-API.
        profile = ScoringProfile(id=profile_id or "")

        try:
            # 1. Fetch jobs from source
            sources_config = load_sources_config().get("sources", {})
            source_config = sources_config.get(source_id, {})
            if limit:
                source_config["limit"] = limit
            if location:
                source_config["location"] = location

            scraper_cls = SourceRegistry.get(source_id)
            scraper = scraper_cls()
            raw_jobs = await scraper.fetch(source_config)
            response.jobs_found = len(raw_jobs)

            if not raw_jobs:
                response.duration_ms = int((time.time() - start) * 1000)
                return response

            # 3. Normalize
            normalized = [scraper.normalize(raw) for raw in raw_jobs]

            # 4. Dedup (hash-based)
            new_jobs, dup_count = await self.dedup.filter_batch(normalized)
            response.jobs_duplicate = dup_count
            response.jobs_new = len(new_jobs)

            if not new_jobs:
                response.duration_ms = int((time.time() - start) * 1000)
                return response

            # 4b. MinHash near-duplicate filter
            minhash_filtered: list = []
            for job in new_jobs:
                desc = getattr(job, "description", "") or ""
                if self._minhash.is_near_duplicate(desc, []):
                    logger.info(
                        "MinHash near-duplicate skipped",
                        extra={"url": getattr(job, "url", "")[:80]},
                    )
                    response.jobs_duplicate = (response.jobs_duplicate or 0) + 1
                else:
                    job_id = getattr(job, "external_id", "") or getattr(job, "url", "")
                    self._minhash.add(desc, job_id)
                    minhash_filtered.append(job)
            new_jobs = minhash_filtered

            if not new_jobs:
                response.duration_ms = int((time.time() - start) * 1000)
                return response

            # 4c. Location normalization + Rules engine
            dq_rejected: list = []
            dq_kept: list = []
            reject_active = self._rules_engine.mode == "flag+reject"

            for job in new_jobs:
                job_dict = job.model_dump() if hasattr(job, "model_dump") else dict(job)

                # Location normalization
                raw_location = job_dict.get("location", "") or ""
                loc_result = self._location_normalizer.normalize(raw_location)
                job_dict.update(loc_result)

                # Rules classification
                verdict, flags = self._rules_engine.classify(job_dict)
                job_dict["dq_flags"] = flags

                if verdict == "reject" and reject_active:
                    logger.info(
                        "Job rejected by DQ rules",
                        extra={"flags": flags, "url": job_dict.get("url", "")[:80]},
                    )
                    dq_rejected.append(job)
                    # F6: remove from LSH to prevent memory leak + false-positive drift
                    lsh_id = getattr(job, "external_id", "") or getattr(job, "url", "")
                    if lsh_id:
                        self._minhash.remove(lsh_id)
                else:
                    # Rebuild job with enriched fields where model supports it
                    try:
                        enriched = job.model_copy(update=job_dict)
                    except Exception:
                        enriched = job
                    dq_kept.append(enriched)

            new_jobs = dq_kept
            logger.info(
                "DQ filter complete",
                extra={"kept": len(dq_kept), "rejected": len(dq_rejected)},
            )

            if not new_jobs:
                response.duration_ms = int((time.time() - start) * 1000)
                return response

            # 5. Score stage 1
            scoring_config = load_scoring_config().get("scoring", {})
            pipeline = ScoringPipeline(scoring_config)
            scored = await pipeline.run_stage1(new_jobs, profile)
            kept, below = pipeline.filter_by_threshold(scored)
            response.jobs_below_threshold = below

            # 6. Store (if enabled)
            if store and kept:
                stored = await self.job_repo.insert_batch(kept, profile_id or "")
                response.jobs_stored = stored

            # 7. Enrich new companies
            try:
                domains: set[str] = set()
                companies_to_enrich: list[CompanyProfile] = []
                for job in kept:
                    domain = self._extract_domain(job)
                    if domain and domain not in domains:
                        domains.add(domain)
                        needs = await self.company_repo.needs_enrichment(
                            domain
                        )
                        if needs:
                            companies_to_enrich.append(
                                CompanyProfile(
                                    domain=domain, name=job.company
                                )
                            )

                if companies_to_enrich:
                    enrichment_config = load_enrichment_config().get(
                        "enrichment", {}
                    )
                    enrich_pipeline = EnrichmentPipeline(enrichment_config)
                    ctx = EnrichmentContext(
                        jobs=[j.model_dump() for j in kept],
                        profile_id=profile_id,
                        source=source_id,
                    )
                    enriched = await enrich_pipeline.run(
                        companies_to_enrich, ctx
                    )
                    for company in enriched:
                        await self.company_repo.upsert(company)
                    response.jobs_enriched = len(enriched)
            except Exception as e:
                logger.error("Enrichment failed: %s", e)
                errors.append(f"enrichment: {e}")

        except Exception as e:
            logger.error("Scrape orchestrator failed: %s", e)
            errors.append(str(e))
            raise

        response.errors = errors
        response.duration_ms = int((time.time() - start) * 1000)
        return response

    @staticmethod
    def _extract_domain(job) -> str:
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
                    url.split("/boards/")
                    if "/boards/" in url
                    else url.split("greenhouse.io/")
                )
                if len(parts) > 1:
                    return parts[1].split("/")[0] + ".com"
            elif host and "indeed" not in host and "adzuna" not in host:
                return host.replace("www.", "")
        except Exception:
            pass
        return ""
