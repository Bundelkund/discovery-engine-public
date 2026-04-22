"""Tests for MinHashDedup near-duplicate detection."""
import pytest

from app.data_quality.minhash import MinHashDedup


@pytest.fixture()
def dedup():
    return MinHashDedup(threshold=0.9, num_perm=128, shingle_size=5)


# ---------------------------------------------------------------------------
# Near-duplicate detection
# ---------------------------------------------------------------------------


def test_near_duplicate_identical_texts(dedup: MinHashDedup) -> None:
    """Two identical texts should be detected as near-duplicates."""
    text = "This is a job description about software engineering at Acme Corp. " * 20
    dedup.add(text, "job-1")
    assert dedup.is_near_duplicate(text, []) is True


def test_near_duplicate_slightly_modified(dedup: MinHashDedup) -> None:
    """Texts with >95% similarity should be flagged as near-duplicates.

    We use a base text appended with a short suffix so the Jaccard similarity
    is reliably above 0.95, which MinHash-LSH detects consistently at threshold=0.9.
    (Changing only a few words in a long repeated text produces Jaccard ~0.95 but
    MinHash estimation variance can cause misses; appending is more deterministic.)
    """
    base = (
        "Senior Python Engineer at Acme Corp. We are looking for an experienced "
        "software engineer to join our growing team. You will work on exciting "
        "projects involving distributed systems and cloud computing. "
    ) * 10
    # Append a small suffix — keeps Jaccard ~0.95 (reliably above 0.9 threshold)
    modified = base + "Apply now!"
    dedup.add(base, "job-original")
    assert dedup.is_near_duplicate(modified, []) is True


def test_non_duplicate_distinct_texts(dedup: MinHashDedup) -> None:
    """Completely different texts should NOT be flagged as near-duplicates."""
    text_a = "Backend Java developer role at TechCo. Spring Boot, microservices. " * 15
    text_b = "Marketing manager position at StartupXYZ. Social media, campaigns. " * 15
    dedup.add(text_a, "job-a")
    assert dedup.is_near_duplicate(text_b, []) is False


def test_non_duplicate_empty_index(dedup: MinHashDedup) -> None:
    """Empty index never returns a duplicate."""
    text = "Any job description here."
    assert dedup.is_near_duplicate(text, []) is False


# ---------------------------------------------------------------------------
# add() / query behaviour
# ---------------------------------------------------------------------------


def test_add_increases_size(dedup: MinHashDedup) -> None:
    dedup.add("First unique job description " * 10, "job-1")
    assert dedup.size == 1
    dedup.add("Second unique job description " * 10, "job-2")
    assert dedup.size == 2


def test_add_duplicate_job_id_is_noop(dedup: MinHashDedup) -> None:
    """Adding the same job_id twice should not grow the index."""
    text = "Python developer at Acme " * 10
    dedup.add(text, "job-same")
    dedup.add(text, "job-same")
    assert dedup.size == 1


def test_add_empty_text_is_skipped(dedup: MinHashDedup) -> None:
    dedup.add("", "job-empty")
    assert dedup.size == 0


def test_add_empty_id_is_skipped(dedup: MinHashDedup) -> None:
    dedup.add("Some text " * 10, "")
    assert dedup.size == 0


# ---------------------------------------------------------------------------
# Configuration validation
# ---------------------------------------------------------------------------


def test_invalid_threshold_raises() -> None:
    with pytest.raises(ValueError, match="threshold"):
        MinHashDedup(threshold=0.0)


def test_invalid_num_perm_raises() -> None:
    with pytest.raises(ValueError, match="num_perm"):
        MinHashDedup(num_perm=0)
