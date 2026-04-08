import logging

from app.config import load_archetypes_config
from app.models.job import NormalizedJob, ScorerResult
from app.models.profile import UserProfile
from app.registry.scorer_registry import ScorerRegistry
from app.scoring.base import BaseScorer

logger = logging.getLogger(__name__)


@ScorerRegistry.register("keyword")
class KeywordScorer(BaseScorer):
    scorer_id = "keyword"
    stage = 1

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.weights = self.config.get(
            "weights",
            {
                "archetype_match": 30,
                "keyword_positive": 25,
                "seniority": 15,
                "remote_bonus": 10,
                "noise_penalty": -20,
            },
        )
        self._archetypes = None

    @property
    def archetypes(self):
        if self._archetypes is None:
            self._archetypes = load_archetypes_config().get("archetypes", {})
        return self._archetypes

    async def score(
        self, job: NormalizedJob, profile: UserProfile
    ) -> ScorerResult:
        score = 0.0
        details = {}
        text = f"{job.title} {job.description}".lower()

        # 1. Archetype match (weighted by profile preferences)
        archetype_score, best_archetype = self._score_archetypes(text, profile)
        score += archetype_score * self.weights.get("archetype_match", 30) / 100
        details["archetype_score"] = archetype_score
        details["best_archetype"] = best_archetype

        # 2. Keyword positive match
        kw_score = self._score_keywords_positive(text, profile)
        score += kw_score * self.weights.get("keyword_positive", 25) / 100
        details["keyword_positive_score"] = kw_score

        # 3. Seniority
        sen_score = self._score_seniority(job.title, profile)
        score += sen_score * self.weights.get("seniority", 15) / 100
        details["seniority_score"] = sen_score

        # 4. Remote bonus
        remote_score = self._score_remote(text)
        score += remote_score * self.weights.get("remote_bonus", 10) / 100
        details["remote_score"] = remote_score

        # 5. Noise penalty
        noise = self._score_noise(text, profile)
        score += noise * abs(self.weights.get("noise_penalty", -20)) / 100
        details["noise_score"] = noise

        return ScorerResult(
            scorer_id=self.scorer_id,
            stage=self.stage,
            score=max(0, min(100, score)),
            details=details,
        )

    def _score_archetypes(
        self, text: str, profile: UserProfile
    ) -> tuple[float, str]:
        if not profile.archetypes:
            return 0.0, ""
        best_score = 0.0
        best_id = ""
        for arch_id, weight in profile.archetypes.items():
            if weight <= 0:
                continue
            arch_config = self.archetypes.get(arch_id, {})
            keywords = arch_config.get("keywords_de", []) + arch_config.get(
                "keywords_en", []
            )
            matches = sum(1 for kw in keywords if kw.lower() in text)
            if matches > 0:
                arch_score = min(100, matches * 40) * weight
                if arch_score > best_score:
                    best_score = arch_score
                    best_id = arch_id
        return best_score, best_id

    def _score_keywords_positive(
        self, text: str, profile: UserProfile
    ) -> float:
        if not profile.keywords_positive:
            return 0.0
        matches = sum(
            1 for kw in profile.keywords_positive if kw.lower() in text
        )
        return min(100, matches * 25)

    def _score_seniority(self, title: str, profile: UserProfile) -> float:
        title_lower = title.lower()
        for term in profile.seniority_boost:
            if term.lower() in title_lower:
                return 100.0
        for term in profile.seniority_penalty:
            if term.lower() in title_lower:
                return -100.0
        return 0.0

    def _score_remote(self, text: str) -> float:
        remote_terms = [
            "remote",
            "homeoffice",
            "home office",
            "hybrid",
            "remote-first",
        ]
        return 100.0 if any(t in text for t in remote_terms) else 0.0

    def _score_noise(self, text: str, profile: UserProfile) -> float:
        if not profile.keywords_negative:
            return 0.0
        matches = sum(
            1 for kw in profile.keywords_negative if kw.lower() in text
        )
        return -min(100, matches * 50)
