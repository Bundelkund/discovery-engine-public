import logging
import re

from app.config import load_archetypes_config
from app.models.job import NormalizedJob, ScorerResult
from app.registry.scorer_registry import ScorerRegistry
from app.scoring.base import BaseScorer
from app.scoring.types import ScoringProfile

logger = logging.getLogger(__name__)


def _word_match(keyword: str, text: str) -> bool:
    kw = re.escape(keyword.lower())
    if len(keyword) <= 3:
        return bool(re.search(rf'\b{kw}\b', text))
    else:
        return bool(re.search(rf'\b{kw}', text))


@ScorerRegistry.register("keyword")
class KeywordScorer(BaseScorer):
    scorer_id = "keyword"
    stage = 1

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.weights = self.config.get(
            "weights",
            {
                "role_match": 25,
                "archetype_match": 20,
                "keyword_positive": 15,
                "location_match": 20,
                "seniority": 5,
                "remote_bonus": 5,
                "noise_penalty": -10,
            },
        )
        self._archetypes = None

    @property
    def archetypes(self):
        if self._archetypes is None:
            self._archetypes = load_archetypes_config().get("archetypes", {})
        return self._archetypes

    async def score(
        self, job: NormalizedJob, profile: ScoringProfile
    ) -> ScorerResult:
        score = 0.0
        details = {}
        title = job.title
        description = job.description
        text = f"{title} {description}".lower()

        # 1. Role match
        role_score = self._score_role_match(title, profile)
        score += role_score * self.weights.get("role_match", 25) / 100
        details["role_score"] = role_score

        # 2. Archetype match
        archetype_score, best_archetype = self._score_archetypes(text, profile)
        score += archetype_score * self.weights.get("archetype_match", 20) / 100
        details["archetype_score"] = archetype_score
        details["best_archetype"] = best_archetype

        # 3. Keyword positive match (title vs description weighted)
        kw_score = self._score_keywords_positive(title, description, profile)
        score += kw_score * self.weights.get("keyword_positive", 15) / 100
        details["keyword_positive_score"] = kw_score

        # 4. Location match
        loc_text = f"{job.location} {text}".lower()
        loc_score = self._score_location(loc_text, profile)
        score += loc_score * self.weights.get("location_match", 20) / 100
        details["location_score"] = loc_score

        # 5. Seniority
        sen_score = self._score_seniority(title, profile)
        score += sen_score * self.weights.get("seniority", 5) / 100
        details["seniority_score"] = sen_score

        # 6. Remote bonus
        remote_score = self._score_remote(text)
        score += remote_score * self.weights.get("remote_bonus", 5) / 100
        details["remote_score"] = remote_score

        # 7. Noise penalty (keywords_negative + negative_domains)
        noise = self._score_noise(text, profile)
        score += noise * abs(self.weights.get("noise_penalty", -10)) / 100
        details["noise_score"] = noise

        return ScorerResult(
            scorer_id=self.scorer_id,
            stage=self.stage,
            score=max(0, min(100, score)),
            details=details,
        )

    def _score_role_match(self, title: str, profile: ScoringProfile) -> float:
        title_lower = title.lower()
        # Check primary roles first
        primary_hits = sum(1 for role in profile.target_roles_primary if role.lower() in title_lower)
        if primary_hits >= 2:
            return 100.0
        if primary_hits == 1:
            return 80.0
        # Secondary roles only if no primary
        secondary_hits = sum(1 for role in profile.target_roles_secondary if role.lower() in title_lower)
        if secondary_hits >= 2:
            return 60.0
        if secondary_hits == 1:
            return 40.0
        return 0.0

    def _score_archetypes(
        self, text: str, profile: ScoringProfile
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
            matches = sum(1 for kw in keywords if _word_match(kw, text))
            if matches > 0:
                arch_score = min(100, matches * 40) * weight
                if arch_score > best_score:
                    best_score = arch_score
                    best_id = arch_id
        return best_score, best_id

    def _score_keywords_positive(
        self, title: str, description: str, profile: ScoringProfile
    ) -> float:
        if not profile.keywords_positive:
            return 0.0
        title_lower = title.lower()
        desc_lower = description.lower()
        score = 0
        for kw in profile.keywords_positive:
            if _word_match(kw, title_lower):
                score += 15
            elif _word_match(kw, desc_lower):
                score += 5
        return min(100, score)

    def _score_seniority(self, title: str, profile: ScoringProfile) -> float:
        title_lower = title.lower()
        for term in profile.seniority_boost:
            if term.lower() in title_lower:
                return 100.0
        for term in profile.seniority_penalty:
            if term.lower() in title_lower:
                return -100.0
        return 0.0

    def _score_location(self, text: str, profile: ScoringProfile) -> float:
        if not profile.target_locations:
            return 0.0
        # Exact city match → 100, remote → 75, EU/DACH → 50
        for loc in profile.target_locations:
            if loc.lower() in text:
                return 100.0
        remote_terms = ["remote", "remote-first", "anywhere", "worldwide"]
        if any(t in text for t in remote_terms):
            return 75.0
        eu_terms = [
            "europe", "eu", "emea", "dach", "germany", "deutschland",
            "austria", "switzerland", "oesterreich", "schweiz",
        ]
        if any(t in text for t in eu_terms):
            return 50.0
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

    def _score_noise(self, text: str, profile: ScoringProfile) -> float:
        all_negative = (profile.keywords_negative or []) + (profile.negative_domains or [])
        if not all_negative:
            return 0.0
        matches = sum(1 for kw in all_negative if _word_match(kw, text))
        return -min(100, matches * 50)
