import logging

import numpy as np
from openai import AsyncOpenAI

from app.config import get_settings
from app.models.job import NormalizedJob, ScorerResult
from app.models.profile import UserProfile
from app.registry.scorer_registry import ScorerRegistry
from app.scoring.base import BaseScorer

logger = logging.getLogger(__name__)


@ScorerRegistry.register("embedding")
class EmbeddingScorer(BaseScorer):
    scorer_id = "embedding"
    stage = 2

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.model = self.config.get("model", "text-embedding-3-small")
        self._client = None

    @property
    def client(self):
        if self._client is None:
            settings = get_settings()
            self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        return self._client

    async def score(
        self, job: NormalizedJob, profile: UserProfile
    ) -> ScorerResult:
        try:
            if not profile.cv_embedding:
                return ScorerResult(
                    scorer_id=self.scorer_id,
                    stage=self.stage,
                    score=0.0,
                    details={"error": "no_cv_embedding"},
                )

            jd_text = f"{job.title} {job.description}"[:8000]
            response = await self.client.embeddings.create(
                input=[jd_text], model=self.model
            )
            jd_embedding = response.data[0].embedding

            similarity = self._cosine_similarity(
                profile.cv_embedding, jd_embedding
            )
            score = max(0, min(100, similarity * 100))

            return ScorerResult(
                scorer_id=self.scorer_id,
                stage=self.stage,
                score=score,
                details={"cosine_similarity": round(similarity, 4)},
            )
        except Exception as e:
            logger.error(f"Embedding scoring failed: {e}")
            return ScorerResult(
                scorer_id=self.scorer_id,
                stage=self.stage,
                score=0.0,
                details={"error": str(e)},
            )

    def _cosine_similarity(self, a: list[float], b: list[float]) -> float:
        a_arr = np.array(a)
        b_arr = np.array(b)
        dot = np.dot(a_arr, b_arr)
        norm = np.linalg.norm(a_arr) * np.linalg.norm(b_arr)
        return float(dot / norm) if norm > 0 else 0.0
