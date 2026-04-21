"""Map Supabase profile rows to Discovery Engine's UserProfile model.

The profiles table uses keywords_positive_tech + keywords_positive_soft (from WonderApply),
but the scoring engine expects a single keywords_positive list. This mapper is the single
source of truth for that transformation.
"""

import json


def _parse_embedding(value):
    """Supabase returns pgvector columns as strings like '[0.1,0.2,...]'.

    Convert to list[float] so Pydantic accepts it. Pass through None and
    already-parsed lists unchanged.
    """
    if value is None or isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def map_profile_data(data: dict) -> dict:
    """Prepare a Supabase profile row for UserProfile construction.

    Merges keywords_positive_tech + keywords_positive_soft into keywords_positive.
    Parses cv_embedding pgvector string into list[float].
    """
    data["keywords_positive"] = (
        (data.get("keywords_positive_tech") or [])
        + (data.get("keywords_positive_soft") or [])
    )
    if "cv_embedding" in data:
        data["cv_embedding"] = _parse_embedding(data["cv_embedding"])
    return data
