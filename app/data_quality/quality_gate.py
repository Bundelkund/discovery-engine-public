"""Profile-free title quality gate (replaces the profile-coupled storage_gate.title_gate).

The engine is profile-agnostic: it must NOT reject a job just because it fails to match
one user's profile — a second tenant would never see it. But the old title_gate also
served as the ATS-flood cap: with db-driven-slugs feeding ~5915 boards and
``store_threshold=0``, a row with no real title would pollute the shared shelf.

So this gate keeps EVERY job that has a real title and drops only structurally empty /
garbage ones. No ScoringProfile, no keywords — pure data hygiene.
"""
import re

MIN_TITLE_CHARS = 2
_ALNUM = re.compile(r"[A-Za-z0-9]")


def quality_gate(title: str) -> bool:
    """Return True to keep the job on the shelf, False to reject.

    Profile-free floor: keep any job whose title is a real, non-trivial string;
    drop only empty / whitespace-only / too-short / punctuation-only titles.
    """
    t = (title or "").strip()
    if len(t) < MIN_TITLE_CHARS:
        return False
    if not _ALNUM.search(t):  # pure punctuation / symbols → garbage
        return False
    return True
