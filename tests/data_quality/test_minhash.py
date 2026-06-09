"""Tests for DB-backed MinHashDedup near-duplicate detection.

In-mem LSH semantics (.size, job_id tracking, bulk_add, remove) are gone —
that state now lives in the dedup_memory table. DB-interaction tests live in
test_dedup_db.py. This file covers:
  - constructor config validation (ValueError guards)
  - is_near_duplicate() signature and mock-DB behaviour
  - add() empty-text guard
"""
import pytest
from unittest.mock import MagicMock

from app.data_quality.minhash import MinHashDedup, _compute_band_hashes


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

NUM_PERM = 128
BAND_WIDTH = 4
SHINGLE_SIZE = 5
SEED = 42


def _stub_client(band_data=None):
    """Return a MagicMock supabase client.

    *band_data* — list of dicts returned by the dedup_memory query (simulates
    DB rows; each row is {"band_hash": ..., "content_hash": ...}). Pass [] to
    simulate no stored bands (not a duplicate).
    """
    client = MagicMock()
    # Read path: select().in_(bands).gte(seen_at, cutoff).execute()
    # (no .limit() — F3 counts ALL band collisions to honour the threshold)
    client.table.return_value.select.return_value.in_.return_value.gte.return_value.execute.return_value.data = (
        band_data if band_data is not None else []
    )
    # upsert path used by add()
    client.table.return_value.upsert.return_value.execute.return_value.data = []
    return client


def _dup_rows(text, content_hash="dup1", n=None):
    """Build dedup_memory rows simulating a stored document *content_hash* that
    shares the first *n* of this text's bands. n defaults to enough to exceed the
    0.9-threshold band requirement (all bands)."""
    bands = _compute_band_hashes(text, NUM_PERM, BAND_WIDTH, SHINGLE_SIZE, SEED)
    if n is None:
        n = len(bands)
    return [{"band_hash": bh, "content_hash": content_hash} for bh in bands[:n]]


def _make_dedup(client=None, **kwargs) -> MinHashDedup:
    if client is None:
        client = _stub_client()
    params = dict(
        threshold=0.9,
        num_perm=NUM_PERM,
        band_width=BAND_WIDTH,
        shingle_size=SHINGLE_SIZE,
        seed=SEED,
    )
    params.update(kwargs)
    return MinHashDedup(client, **params)


# ---------------------------------------------------------------------------
# Configuration validation
# ---------------------------------------------------------------------------


def test_invalid_threshold_raises() -> None:
    with pytest.raises(ValueError, match="threshold"):
        MinHashDedup(_stub_client(), threshold=0.0)


def test_invalid_num_perm_raises() -> None:
    with pytest.raises(ValueError, match="num_perm"):
        MinHashDedup(_stub_client(), num_perm=0)


def test_invalid_band_width_raises() -> None:
    with pytest.raises(ValueError, match="band_width"):
        # band_width=3 does not divide num_perm=128 evenly
        MinHashDedup(_stub_client(), num_perm=128, band_width=3)


# ---------------------------------------------------------------------------
# is_near_duplicate() — DB query behaviour
# ---------------------------------------------------------------------------


def test_near_duplicate_when_enough_bands_match() -> None:
    """When one stored doc shares >= required bands, is_near_duplicate is True."""
    text = "This is a job description about software engineering at Acme Corp. " * 20
    # All bands of the same stored content_hash collide → well over threshold.
    client = _stub_client(band_data=_dup_rows(text))
    dedup = _make_dedup(client)
    assert dedup.is_near_duplicate(text) is True


def test_single_band_match_is_not_near_duplicate() -> None:
    """F3: a SINGLE shared band must NOT trigger a near-dup at threshold 0.9.

    Old code declared a match on any one band (effective threshold ~0.42); the
    threshold knob now requires ~21 of 32 bands, so one band is well below."""
    text = "This is a job description about software engineering at Acme Corp. " * 20
    client = _stub_client(band_data=_dup_rows(text, n=1))
    dedup = _make_dedup(client)
    assert dedup.required_band_matches() == 21  # ceil(32 * 0.9**4)
    assert dedup.is_near_duplicate(text) is False


def test_required_band_matches_tracks_threshold() -> None:
    """A looser threshold lowers the required band count; stricter raises it."""
    text = "Senior backend engineer, distributed systems, Go and Rust. " * 20
    loose = _make_dedup(_stub_client(), threshold=0.5)
    strict = _make_dedup(_stub_client(), threshold=0.99)
    assert loose.required_band_matches() < strict.required_band_matches()
    # 14 shared bands: above loose(0.5 → 2), below strict(0.99 → 31).
    rows = _dup_rows(text, n=14)
    assert _make_dedup(_stub_client(rows), threshold=0.5).is_near_duplicate(text) is True
    assert _make_dedup(_stub_client(rows), threshold=0.99).is_near_duplicate(text) is False


def test_not_near_duplicate_when_db_empty() -> None:
    """When DB returns no rows, is_near_duplicate returns False."""
    text = "Backend Java developer role at TechCo. Spring Boot, microservices. " * 15
    client = _stub_client(band_data=[])
    dedup = _make_dedup(client)
    assert dedup.is_near_duplicate(text) is False


def test_near_duplicate_empty_text_returns_false() -> None:
    """Empty text is never a duplicate (no DB call made)."""
    client = _stub_client()
    dedup = _make_dedup(client)
    assert dedup.is_near_duplicate("") is False
    client.table.assert_not_called()


def test_near_duplicate_db_error_returns_false() -> None:
    """A DB exception during query must be caught and return False (fail-safe)."""
    client = MagicMock()
    client.table.return_value.select.return_value.in_.return_value.gte.return_value.execute.side_effect = (
        RuntimeError("connection refused")
    )
    dedup = _make_dedup(client)
    assert dedup.is_near_duplicate("some job text " * 10) is False


# ---------------------------------------------------------------------------
# add() guards
# ---------------------------------------------------------------------------


def test_add_empty_text_skips_upsert() -> None:
    """add() with empty text must not call supabase at all."""
    client = _stub_client()
    dedup = _make_dedup(client)
    dedup.add("")
    client.table.assert_not_called()


def test_add_non_empty_text_calls_upsert() -> None:
    """add() with valid text must issue exactly one upsert call."""
    client = _stub_client()
    dedup = _make_dedup(client)
    dedup.add("Python developer at Acme Corp. " * 10)
    client.table.assert_called_with("dedup_memory")
    client.table.return_value.upsert.assert_called_once()
