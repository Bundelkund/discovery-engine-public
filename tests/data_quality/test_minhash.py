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
    DB rows). Pass [] to simulate no stored bands (not a duplicate).
    """
    client = MagicMock()
    # Read path: select().in_(bands).gte(seen_at, cutoff).limit(1).execute()
    client.table.return_value.select.return_value.in_.return_value.gte.return_value.limit.return_value.execute.return_value.data = (
        band_data if band_data is not None else []
    )
    # upsert path used by add()
    client.table.return_value.upsert.return_value.execute.return_value.data = []
    return client


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


def test_near_duplicate_when_band_in_db() -> None:
    """When DB returns a band collision, is_near_duplicate returns True."""
    text = "This is a job description about software engineering at Acme Corp. " * 20
    bands = _compute_band_hashes(text, NUM_PERM, BAND_WIDTH, SHINGLE_SIZE, SEED)
    client = _stub_client(band_data=[{"band_hash": bands[0]}])
    dedup = _make_dedup(client)
    assert dedup.is_near_duplicate(text) is True


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
    client.table.return_value.select.return_value.in_.return_value.gte.return_value.limit.return_value.execute.side_effect = (
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
