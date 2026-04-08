from abc import ABC, abstractmethod

from app.models.job import NormalizedJob, ScorerResult
from app.models.profile import UserProfile


class BaseScorer(ABC):
    scorer_id: str
    stage: int

    @abstractmethod
    async def score(
        self, job: NormalizedJob, profile: UserProfile
    ) -> ScorerResult:
        """Score a job against a user profile."""
