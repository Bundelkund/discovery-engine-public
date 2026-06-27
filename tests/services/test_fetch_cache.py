"""FetchCache — checksum-skip helper (fetch-checksum-skip).

seen_unchanged: True on byte-identical body (bumps last_fetched_at), False on change.
Fail-open: missing creds OR a DB error -> False (caller takes the normal parse+insert path).
record: upserts checksum on a miss/change.
"""
from unittest.mock import MagicMock, patch

import pytest

from app.services.fetch_cache import FetchCache, checksum


def _no_client_cache():
    """FetchCache whose client build failed (missing creds / create_client raised)."""
    with patch.object(FetchCache, "_build_client", return_value=None):
        return FetchCache()


def _select_chain(client):
    return (
        client.table.return_value.select.return_value
        .eq.return_value.eq.return_value.limit.return_value
    )


@pytest.mark.asyncio
async def test_seen_unchanged_true_on_matching_body_and_bumps_last_fetched_at():
    body = '{"jobs": [{"id": 1}]}'
    client = MagicMock()
    _select_chain(client).execute.return_value = MagicMock(
        data=[{"checksum": checksum(body)}]
    )

    result = await FetchCache(client).seen_unchanged("greenhouse", "acme", body)

    assert result is True
    # match -> last_fetched_at bumped, nothing else
    update_arg = client.table.return_value.update.call_args[0][0]
    assert "last_fetched_at" in update_arg
    assert set(update_arg) == {"last_fetched_at"}


@pytest.mark.asyncio
async def test_seen_unchanged_false_on_changed_body_does_not_bump():
    body = '{"jobs": [{"id": 2}]}'
    client = MagicMock()
    _select_chain(client).execute.return_value = MagicMock(
        data=[{"checksum": "stale-different-checksum"}]
    )

    result = await FetchCache(client).seen_unchanged("greenhouse", "acme", body)

    assert result is False
    client.table.return_value.update.assert_not_called()


@pytest.mark.asyncio
async def test_seen_unchanged_false_on_first_seen():
    client = MagicMock()
    _select_chain(client).execute.return_value = MagicMock(data=[])

    result = await FetchCache(client).seen_unchanged("greenhouse", "new-board", "x")

    assert result is False


@pytest.mark.asyncio
async def test_seen_unchanged_fail_open_on_db_error():
    client = MagicMock()
    _select_chain(client).execute.side_effect = RuntimeError("pooler timeout")

    result = await FetchCache(client).seen_unchanged("greenhouse", "acme", "x")

    assert result is False  # fail-open -> normal path


@pytest.mark.asyncio
async def test_seen_unchanged_fail_open_without_client():
    """No creds / client build failed -> client is None -> never blocks a scrape."""
    result = await _no_client_cache().seen_unchanged("greenhouse", "acme", "x")
    assert result is False


@pytest.mark.asyncio
async def test_record_upserts_checksum_on_conflict():
    body = '{"jobs": [{"id": 3}]}'
    client = MagicMock()

    await FetchCache(client).record("greenhouse", "acme", body)

    upsert_args, upsert_kwargs = client.table.return_value.upsert.call_args
    row = upsert_args[0]
    assert row["source_name"] == "greenhouse"
    assert row["fetch_key"] == "acme"
    assert row["checksum"] == checksum(body)
    assert "last_changed_at" in row and "last_fetched_at" in row
    assert upsert_kwargs["on_conflict"] == "source_name,fetch_key"


@pytest.mark.asyncio
async def test_record_swallows_errors():
    client = MagicMock()
    client.table.return_value.upsert.return_value.execute.side_effect = RuntimeError("down")
    # must not raise
    await FetchCache(client).record("greenhouse", "acme", "x")


@pytest.mark.asyncio
async def test_record_noop_without_client():
    await _no_client_cache().record("greenhouse", "acme", "x")  # must not raise
