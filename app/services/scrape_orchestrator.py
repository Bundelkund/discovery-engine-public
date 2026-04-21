import logging
import time
from urllib.parse import urlparse

from fastapi import HTTPException

from app.config import (
    load_enrichment_config,
    load_scoring_config,
    load_sources_config,
)
from app.utils.profile_mapper import map_profile_data
from app.deduplication.dedup import DeduplicationService
from app.enrichment.pipeline import EnrichmentPipeline
from app.models.company import CompanyProfile, EnrichmentContext
from app.models.profile import UserProfile
from app.models.responses import ScrapeResponse
from app.registry.source_registry import SourceRegistry
from app.repositories.companies import CompanyRepository
from app.repositories.jobs import JobRepository
from app.repositories.profiles import ProfileRepository
from app.scoring.pipeline import ScoringPipeline

logger = logging.getLogger(__name__)


class ScrapeOrchestrator:
    def __init__(self, supabase_client):
        self.supabase = supabase_client
        self.job_repo = JobRepository(supabase_client)
        self.profile_repo = ProfileRepository(supabase_client)
        self.company_repo = CompanyRepository(supabase_client)
        self.dedup = DeduplicationService(supabase_client)

    async def run(
        self,
        source_id: str,
        profile_id: str,
        location: str = None,
        limit: int = None,
        store: bool = True,
    ) -> ScrapeResponse:
        start = time.time()
        errors: list[str] = []
        response = ScrapeResponse(source=source_id, profile_id=profile_id)

        try:
            # 1. Load profile
            profile_data = await self.profile_repo.get(profile_id)
            if not profile_data:
                raise HTTPException(
                    status_code=404,
                    detail=f"Profile {profile_id} not found",
                )
            map_profile_data(profile_data)
            profile = UserProfile(
                **{
                    k: v
                    for k, v in profile_data.items()
                    if k in UserProfile.model_fields
                }
            )

            # 2. Fetch jobs from source
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

            # 4. Dedup
            new_jobs, dup_count = await self.dedup.filter_batch(normalized)
            response.jobs_duplicate = dup_count
            response.jobs_new = len(new_jobs)

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
                stored = await self.job_repo.insert_batch(kept, profile_id)
                response.jobs_stored = stored

            # 7. Score stage 2 (for high scorers)
            if kept:
                await pipeline.run_stage2(kept, profile)
                for job in kept:
                    if job.score_stage_2 is not None:
                        await self.job_repo.update_scores(
                            job.url, job.score_stage_2
                        )

            # 7b. Score stage 3 (LLM role analysis for top stage-1 scorers)
            if kept:
                await pipeline.run_stage3(kept, profile)
                for job in kept:
                    if job.score_stage_3 is not None:
                        await self.job_repo.update_stage3_score(
                            job.url,
                            job.score_stage_3,
                            job.match_reasoning,
                            job.match_highlights,
                            job.match_pitch,
                        )

            # 8. Enrich new companies
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

        except HTTPException:
            raise
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
