from abc import ABC, abstractmethod

from app.models.job import NormalizedJob, ScorerResult
from app.scoring.types import ScoringProfile


class BaseScorer(ABC):
    scorer_id: str
    stage: int

    @abstractmethod
    async def score(
        self, job: NormalizedJob, profile: ScoringProfile
    ) -> ScorerResult:
        """Score a job against a scoring profile."""
