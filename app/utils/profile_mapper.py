"""Map Supabase profile rows to Discovery Engine's UserProfile model.

The profiles table uses keywords_positive_tech + keywords_positive_soft (from WonderApply),
but the scoring engine expects a single keywords_positive list. This mapper is the single
source of truth for that transformation.
"""


def map_profile_data(data: dict) -> dict:
    """Prepare a Supabase profile row for UserProfile construction.

    Merges keywords_positive_tech + keywords_positive_soft into keywords_positive.
    Passes through all other fields unchanged.
    """
    data["keywords_positive"] = (
        (data.get("keywords_positive_tech") or [])
        + (data.get("keywords_positive_soft") or [])
    )
    return data
