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

    # --- WA Provider API Methods ---

    async def list_jobs(
        self,
        profile_id: str,
        page: int = 1,
        page_size: int = 20,
        sort: str = "final_score",
        sort_dir: str = "desc",
        search: str = None,
        source: str = None,
        score_min: float = None,
        archetype: str = None,
    ) -> list[dict]:
        """List jobs with scores, paginated and filterable."""
        query = (
            self.client.table(self.TABLE)
            .select("*")
            .or_(f"profile_id.eq.{profile_id},profile_id.is.null")
        )
        if source:
            query = query.eq("source", source)
        if archetype:
            query = query.eq("archetype", archetype)
        if score_min is not None:
            query = query.gte("score_stage_1", score_min)
        if search:
            # Sanitize: strip PostgREST operators to prevent filter injection
            safe_search = search.replace(",", " ").replace(".", " ").replace("(", "").replace(")", "")
            pattern = f"%{safe_search}%"
            query = query.or_(
                f"title.ilike.{pattern},"
                f"company.ilike.{pattern},"
                f"description.ilike.{pattern}"
            )

        # Sorting: score_stage_3 > stage_2 > stage_1 for final_score
        sort_column = {
            "final_score": "score_stage_3",
            "scraped_at": "scraped_at",
            "company": "company",
        }.get(sort, "score_stage_3")
        desc = sort_dir == "desc"
        query = query.order(sort_column, desc=desc)

        # Pagination
        offset = (page - 1) * page_size
        query = query.range(offset, offset + page_size - 1)

        result = query.execute()
        return result.data or []

    async def count_jobs(
        self,
        profile_id: str,
        search: str = None,
        source: str = None,
        score_min: float = None,
        archetype: str = None,
    ) -> int:
        """Count jobs matching filters (for pagination total)."""
        query = (
            self.client.table(self.TABLE)
            .select("id", count="exact")
            .or_(f"profile_id.eq.{profile_id},profile_id.is.null")
        )
        if source:
            query = query.eq("source", source)
        if archetype:
            query = query.eq("archetype", archetype)
        if score_min is not None:
            query = query.gte("score_stage_1", score_min)
        if search:
            safe_search = search.replace(",", " ").replace(".", " ").replace("(", "").replace(")", "")
            pattern = f"%{safe_search}%"
            query = query.or_(
                f"title.ilike.{pattern},"
                f"company.ilike.{pattern},"
                f"description.ilike.{pattern}"
            )
        result = query.execute()
        return result.count if result.count is not None else 0

    async def get_by_id(self, job_id: str) -> dict | None:
        """Get a single job by ID."""
        result = (
            self.client.table(self.TABLE)
            .select("*")
            .eq("id", job_id)
            .execute()
        )
        return result.data[0] if result.data else None
