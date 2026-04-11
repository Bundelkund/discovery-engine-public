import logging

from app.models.job import ScoredJob
from app.repositories.base import BaseRepository

logger = logging.getLogger(__name__)


class JobRepository(BaseRepository):
    TABLE = "jobs"

    async def insert_batch(self, jobs: list[ScoredJob], profile_id: str) -> int:
        if not jobs:
            return 0
        rows = []
        for job in jobs:
            rows.append(
                {
                    "title": job.title,
                    "url": job.url,
                    "company": job.company,
                    "location": job.location,
                    "description": job.description,
                    "source": job.source,
                    "external_id": job.external_id,
                    "content_hash": job.content_hash,
                    "score_stage_1": job.score_stage_1,
                    "score_stage_2": job.score_stage_2,
                    "archetype": job.archetype,
                    "company_domain": job.company_domain,
                    "profile_id": profile_id,
                    "scraped_at": job.posted_at.isoformat() if job.posted_at else None,
                }
            )
        inserted = 0
        for row in rows:
            try:
                self.client.table(self.TABLE).insert(row).execute()
                inserted += 1
            except Exception as e:
                if "23505" in str(e):
                    logger.debug(f"Duplicate skipped: {row['url'][:60]}")
                else:
                    logger.error(f"Failed to insert job: {e}")
        return inserted

    async def update_stage1_score(
        self, job_url: str, score_stage_1: int, archetype: str = None,
        profile_id: str = None
    ) -> None:
        data = {"score_stage_1": score_stage_1}
        if archetype:
            data["archetype"] = archetype
        if profile_id:
            data["profile_id"] = profile_id
        self.client.table(self.TABLE).update(data).eq("url", job_url).execute()

    async def update_scores(
        self, job_url: str, score_stage_2: float
    ) -> None:
        self.client.table(self.TABLE).update(
            {"score_stage_2": score_stage_2}
        ).eq("url", job_url).execute()

    async def update_stage3_score(
        self,
        job_url: str,
        score_stage_3: float,
        match_reasoning: str = None,
        match_highlights: list[str] = None,
        match_pitch: str = None,
    ) -> None:
        data = {"score_stage_3": score_stage_3}
        if match_reasoning:
            data["match_reasoning"] = match_reasoning
        if match_highlights:
            data["match_highlights"] = match_highlights
        if match_pitch:
            data["match_pitch"] = match_pitch
        self.client.table(self.TABLE).update(data).eq("url", job_url).execute()

    async def get_unscored(
        self, profile_id: str, source: str = None, limit: int = 500
    ) -> list[dict]:
        """Get all unscored jobs — both profile-owned AND legacy (profile_id IS NULL)."""
        query = (
            self.client.table(self.TABLE)
            .select("*")
            .is_("score_stage_1", "null")
            .or_(f"profile_id.eq.{profile_id},profile_id.is.null")
        )
        if source:
            query = query.eq("source", source)
        result = query.limit(limit).execute()
        return result.data or []
