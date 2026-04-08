import logging

from app.models.job import NormalizedJob, ScoredJob
from app.models.profile import UserProfile
from app.registry.scorer_registry import ScorerRegistry

logger = logging.getLogger(__name__)


class ScoringPipeline:
    def __init__(self, config: dict):
        self.stages = []
        self.store_threshold = config.get("store_threshold", 30)
        for stage_cfg in config.get("stages", []):
            if not stage_cfg.get("enabled", True):
                continue
            scorer_cls = ScorerRegistry.get(stage_cfg["scorer_id"])
            self.stages.append(scorer_cls(config=stage_cfg))

    async def run_stage1(
        self, jobs: list[NormalizedJob], profile: UserProfile
    ) -> list[ScoredJob]:
        """Run stage 1 scorers on all jobs. Returns ScoredJob list (only stage1)."""
        stage1_scorers = [s for s in self.stages if s.stage == 1]
        results = []
        for job in jobs:
            total_score = 0
            best_archetype = ""
            for scorer in stage1_scorers:
                try:
                    result = await scorer.score(job, profile)
                    total_score += result.score
                    if result.details.get("best_archetype"):
                        best_archetype = result.details["best_archetype"]
                except Exception as e:
                    logger.warning(
                        f"Scorer {scorer.scorer_id} failed for '{job.title}': {e}"
                    )
            scored = ScoredJob(
                **job.model_dump(),
                score_stage_1=int(total_score),
                archetype=best_archetype,
                profile_id=profile.id,
            )
            results.append(scored)
        return results

    async def run_stage2(
        self, jobs: list[ScoredJob], profile: UserProfile
    ) -> list[ScoredJob]:
        """Run stage 2 scorers on high-scoring jobs only."""
        stage2_scorers = [s for s in self.stages if s.stage == 2]
        if not stage2_scorers:
            return jobs
        gate = next((s for s in self.stages if s.stage == 2), None)
        gate_threshold = gate.config.get("gate_threshold", 50) if gate else 50
        for job in jobs:
            if job.score_stage_1 < gate_threshold:
                continue
            for scorer in stage2_scorers:
                try:
                    result = await scorer.score(
                        NormalizedJob(
                            **{
                                k: v
                                for k, v in job.model_dump().items()
                                if k in NormalizedJob.model_fields
                            }
                        ),
                        profile,
                    )
                    job.score_stage_2 = result.score
                except Exception as e:
                    logger.warning(
                        f"Stage 2 scorer {scorer.scorer_id} failed: {e}"
                    )
        return jobs

    def filter_by_threshold(
        self, jobs: list[ScoredJob]
    ) -> tuple[list[ScoredJob], int]:
        """Filter out jobs below store_threshold. Returns (kept, discarded_count)."""
        kept = [j for j in jobs if j.score_stage_1 >= self.store_threshold]
        return kept, len(jobs) - len(kept)
