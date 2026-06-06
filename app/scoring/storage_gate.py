"""Title-level storage gate (T6 / storage-gate).

Pure decision fn: should a job be persisted at all, given the active
scoring profile's title signals?

scoring.yaml sets store_threshold=0 — the scoring pipeline otherwise
stores everything that survives dedup + DQ. Once db-driven-slugs (T5)
turns on the ~5915 ATS boards from ats_companies, that floods the jobs
table (5915 boards x N jobs). A global threshold change is the wrong
fix: stage-1 keyword score is 0 for an empty profile, so a non-zero
threshold would silently discard EVERYTHING when no profile_id is sent.

This gate is the source-local filter instead: keep a job only if its
TITLE carries a positive profile signal and no negative one. A profile
with no positive signals at all disables the gate (demo / no-profile
deploys unchanged).

Match semantics mirror app/scoring/keyword.py for consistency:
  - keywords (positive/negative): word-boundary match via _word_match
  - target_roles (primary/secondary): plain substring (as _score_role_match)
"""
from app.scoring.keyword import _word_match
from app.scoring.types import ScoringProfile


def title_gate(title: str, profile: ScoringProfile) -> tuple[bool, bool]:
    """Return ``(keep, priority)`` for a job title.

    keep     = title hits a positive signal (keywords_positive OR any
               target_role primary/secondary) AND no keywords_negative hit.
    priority = title hits a target_roles_primary entry (and is kept).

    A profile with no positive signals (empty keywords_positive AND empty
    target_roles_primary AND empty target_roles_secondary) disables the
    gate -> ``(True, False)`` for every title.
    """
    t = (title or "").lower()

    # Negative veto first — a negative title is never stored, never priority.
    if any(_word_match(kw, t) for kw in profile.keywords_negative):
        return False, False

    primary = profile.target_roles_primary
    secondary = profile.target_roles_secondary
    positives = profile.keywords_positive

    # No positive signals configured -> gate disabled (demo unchanged).
    if not (positives or primary or secondary):
        return True, False

    is_priority = any(role.lower() in t for role in primary)
    keep = (
        is_priority
        or any(role.lower() in t for role in secondary)
        or any(_word_match(kw, t) for kw in positives)
    )
    return keep, (keep and is_priority)
