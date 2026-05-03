"""Scoring-internal profile type.

ScoringProfile holds the keyword/archetype signals needed by the scoring
pipeline. It is intentionally minimal — consumer-specific profile data
(CV text, embeddings, DB persistence) lives in the consumer layer.
"""
from pydantic import BaseModel, Field


class ScoringProfile(BaseModel):
    """Scoring-only representation of a candidate profile."""

    id: str
    name: str = ""
    archetypes: dict[str, float] = Field(default_factory=dict)
    keywords_positive: list[str] = Field(default_factory=list)
    keywords_negative: list[str] = Field(default_factory=list)
    seniority_boost: list[str] = Field(
        default_factory=lambda: ["Senior", "Lead", "Head", "Principal"]
    )
    seniority_penalty: list[str] = Field(
        default_factory=lambda: ["Junior", "Intern", "Trainee", "Werkstudent"]
    )
    target_roles_primary: list[str] = Field(default_factory=list)
    target_roles_secondary: list[str] = Field(default_factory=list)
    target_locations: list[str] = Field(default_factory=list)
    negative_domains: list[str] = Field(default_factory=list)
