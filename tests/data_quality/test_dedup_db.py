"""DB-backed MinHashDedup tests.

Covers:
- restart simulation: add via one instance, is_near_duplicate True on a fresh instance
  that reads from the same (mocked) DB state
- window purge: rows with old seen_at are deleted by purge_old(); afterwards the
  text no longer registers as a near-duplicate
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.data_quality.minhash import MinHashDedup, _compute_band_hashes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NUM_PERM = 128
BAND_WIDTH = 4
SHINGLE_SIZE = 5
SEED = 42
TEXT = (
    "Senior Python Engineer at Acme Corp. We are looking for experienced "
    "software engineers to join our growing team. " * 10
)


def _band_hashes(text: str) -> list[str]:
    return _compute_band_hashes(text, NUM_PERM, BAND_WIDTH, SHINGLE_SIZE, SEED)


def _make_dedup(client) -> MinHashDedup:
    return MinHashDedup(
        client,
        threshold=0.9,
        num_perm=NUM_PERM,
        band_width=BAND_WIDTH,
        shingle_size=SHINGLE_SIZE,
        seed=SEED,
        window_days=42,
    )


# ---------------------------------------------------------------------------
# Restart simulation
# ---------------------------------------------------------------------------


def test_restart_sim_near_duplicate_persists():
    """After add() on instance A, a fresh instance B queries the DB and detects the dup."""
    bands = _band_hashes(TEXT)

    # Instance A — write path (add)
    client_a = MagicMock()
    client_a.table.return_value.upsert.return_value.execute.return_value.data = []
    dedup_a = _make_dedup(client_a)
    dedup_a.add(TEXT)
    assert client_a.table.called, "add() must call supabase upsert"

    # Instance B — fresh process, queries DB
    client_b = MagicMock()
    # Simulate DB having the same document's bands stored (within retention
    # window) — enough collisions on one content_hash to clear the threshold.
    client_b.table.return_value.select.return_value.in_.return_value.gte.return_value.execute.return_value.data = [
        {"band_hash": bh, "content_hash": "stored-doc"} for bh in bands
    ]
    dedup_b = _make_dedup(client_b)
    assert dedup_b.is_near_duplicate(TEXT) is True, (
        "Fresh instance must detect near-dup from DB-stored bands"
    )


def test_restart_sim_no_stored_rows_is_not_dup():
    """Fresh instance with empty DB does not flag text as duplicate."""
    client = MagicMock()
    client.table.return_value.select.return_value.in_.return_value.gte.return_value.execute.return_value.data = []
    dedup = _make_dedup(client)
    assert dedup.is_near_duplicate(TEXT) is False


def test_is_near_duplicate_filters_by_retention_window():
    """The read path MUST filter on seen_at >= now - window_days so expired
    bands no longer count as near-dups (regression guard for the window fix)."""
    client = MagicMock()
    client.table.return_value.select.return_value.in_.return_value.gte.return_value.execute.return_value.data = []
    dedup = _make_dedup(client)
    dedup.is_near_duplicate(TEXT)

    gte_call = client.table.return_value.select.return_value.in_.return_value.gte
    gte_call.assert_called_once()
    col_arg, cutoff_arg = gte_call.call_args[0]
    assert col_arg == "seen_at"
    cutoff_dt = datetime.fromisoformat(cutoff_arg)
    expected = datetime.now(tz=timezone.utc) - timedelta(days=42)
    assert abs((cutoff_dt - expected).total_seconds()) < 5


# ---------------------------------------------------------------------------
# Window / purge
# ---------------------------------------------------------------------------


def test_purge_old_calls_delete_with_cutoff():
    """purge_old() must issue a DELETE with lt('seen_at', cutoff_iso)."""
    client = MagicMock()
    client.table.return_value.delete.return_value.lt.return_value.execute.return_value.data = [
        {"id": "row-1"},
        {"id": "row-2"},
    ]
    dedup = _make_dedup(client)
    deleted = dedup.purge_old()

    assert deleted == 2
    client.table.assert_called_with("dedup_memory")
    client.table.return_value.delete.assert_called_once()
    lt_call = client.table.return_value.delete.return_value.lt
    lt_call.assert_called_once()
    col_arg, cutoff_arg = lt_call.call_args[0]
    assert col_arg == "seen_at"
    # cutoff must be a valid ISO timestamp string
    cutoff_dt = datetime.fromisoformat(cutoff_arg)
    expected_cutoff = datetime.now(tz=timezone.utc) - timedelta(days=42)
    assert abs((cutoff_dt - expected_cutoff).total_seconds()) < 5


def test_purge_after_window_not_duplicate():
    """Rows purged by purge_old() must not be found by is_near_duplicate on next query."""
    client = MagicMock()

    # Step 1: purge removes rows
    client.table.return_value.delete.return_value.lt.return_value.execute.return_value.data = [
        {"id": "old-row"}
    ]
    dedup = _make_dedup(client)
    dedup.purge_old()

    # Step 2: subsequent is_near_duplicate query returns empty (rows gone)
    client.table.return_value.select.return_value.in_.return_value.gte.return_value.execute.return_value.data = []
    assert dedup.is_near_duplicate(TEXT) is False


# ---------------------------------------------------------------------------
# add() idempotency — ON CONFLICT DO NOTHING
# ---------------------------------------------------------------------------


def test_add_calls_upsert_with_ignore_duplicates():
    """add() must use upsert with ignore_duplicates=True (ON CONFLICT DO NOTHING)."""
    client = MagicMock()
    client.table.return_value.upsert.return_value.execute.return_value.data = []
    dedup = _make_dedup(client)
    dedup.add(TEXT)

    upsert_call = client.table.return_value.upsert
    upsert_call.assert_called_once()
    _, kwargs = upsert_call.call_args
    assert kwargs.get("ignore_duplicates") is True


def test_add_empty_text_skips_upsert():
    """add() with empty text must not call supabase at all."""
    client = MagicMock()
    dedup = _make_dedup(client)
    dedup.add("")
    client.table.assert_not_called()


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_band_width_must_divide_num_perm():
    client = MagicMock()
    with pytest.raises(ValueError, match="band_width"):
        MinHashDedup(client, num_perm=128, band_width=3)
