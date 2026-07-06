"""RefineRunRepository — flow telemetry writes + WIP/age/cycle reads.

Backs P1 flow diagnostics (.specs/p1-flow-diagnostics.md): drain() persists one
refine_runs row per cycle; /health reads WIP, oldest-'new' age, and the last
24h of cycles. Mock-client pattern mirrors tests/test_scrape_runs_repo.py.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.repositories.refine_runs import RefineRunRepository


@pytest.mark.asyncio
async def test_insert_refine_run():
    """insert() writes isoformat timestamps + stats and returns the row."""
    stats = {
        "fetched": 100,
        "refined": 80,
        "rejected": 5,
        "duplicate": 15,
        "wip_before": 5000,
        "wip_after": 4920,
        "oldest_new_age_seconds": 3600,
    }
    now = datetime.now(timezone.utc)
    client = MagicMock()
    client.table.return_value.insert.return_value.execute.return_value = MagicMock(
        data=[{"id": 1, "stats": stats}]
    )

    result = await RefineRunRepository(client).insert(
        started_at=now, finished_at=now + timedelta(seconds=45), stats=stats
    )

    assert result is not None
    assert result["stats"]["refined"] == 80
    payload = client.table.return_value.insert.call_args[0][0]
    assert payload["started_at"] == now.isoformat()
    assert payload["finished_at"] == (now + timedelta(seconds=45)).isoformat()
    assert payload["stats"] == stats


@pytest.mark.asyncio
async def test_get_wip_count_counts_new_raw_jobs():
    """get_wip_count() returns the exact count of raw_jobs.status='new'."""
    client = MagicMock()
    chain = (
        client.table.return_value.select.return_value
        .eq.return_value.limit.return_value
    )
    chain.execute.return_value = MagicMock(count=8234)

    count = await RefineRunRepository(client).get_wip_count()

    assert count == 8234
    client.table.assert_called_with("raw_jobs")
    client.table.return_value.select.return_value.eq.assert_called_once_with(
        "status", "new"
    )


@pytest.mark.asyncio
async def test_get_oldest_new_age_seconds():
    """Age of the oldest 'new' row in seconds; 0 when the inbox is empty."""
    client = MagicMock()
    chain = (
        client.table.return_value.select.return_value
        .eq.return_value.order.return_value.limit.return_value
    )
    two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    chain.execute.return_value = MagicMock(
        data=[{"ingested_at": two_hours_ago.isoformat()}]
    )

    age = await RefineRunRepository(client).get_oldest_new_age_seconds()

    assert 7195 <= age <= 7205, "≈2h in seconds"

    # Empty inbox → 0, no crash.
    chain.execute.return_value = MagicMock(data=[])
    assert await RefineRunRepository(client).get_oldest_new_age_seconds() == 0


@pytest.mark.asyncio
async def test_get_last_n_cycles_windows_and_limits():
    """get_last_n_cycles(n, hours) filters by finished_at cutoff and caps at n."""
    client = MagicMock()
    rows = [
        {"id": 2, "finished_at": "2026-07-06T12:00:00+00:00", "stats": {"refined": 10}},
        {"id": 1, "finished_at": "2026-07-06T11:00:00+00:00", "stats": {"refined": 7}},
    ]
    chain = (
        client.table.return_value.select.return_value
        .gte.return_value.order.return_value.limit.return_value
    )
    chain.execute.return_value = MagicMock(data=rows)

    result = await RefineRunRepository(client).get_last_n_cycles(n=500, hours=24)

    assert result == rows
    # Cutoff must be a concrete timestamp ~24h ago (PostgREST can't eval now()).
    gte_args = client.table.return_value.select.return_value.gte.call_args[0]
    assert gte_args[0] == "finished_at"
    cutoff = datetime.fromisoformat(gte_args[1])
    delta = datetime.now(timezone.utc) - cutoff
    assert timedelta(hours=23, minutes=59) < delta < timedelta(hours=24, minutes=1)
    chain_limit = client.table.return_value.select.return_value.gte.return_value.order.return_value.limit
    chain_limit.assert_called_once_with(500)
