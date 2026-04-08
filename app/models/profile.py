from typing import Optional

from pydantic import BaseModel, Field


class UserProfile(BaseModel):
    """User profile for scoring."""

    id: str
    name: str = ""
    cv_text: str = ""
    archetypes: dict[str, float] = Field(default_factory=dict)
    keywords_positive: list[str] = Field(default_factory=list)
    keywords_negative: list[str] = Field(default_factory=list)
    seniority_boost: list[str] = Field(
        default_factory=lambda: ["Senior", "Lead", "Head", "Principal"]
    )
    seniority_penalty: list[str] = Field(
        default_factory=lambda: ["Junior", "Intern", "Trainee", "Werkstudent"]
    )
    target_roles: list[str] = Field(default_factory=list)
    cv_embedding: Optional[list[float]] = None
