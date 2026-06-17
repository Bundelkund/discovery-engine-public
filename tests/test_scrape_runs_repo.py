"""ScrapeRunRepository — cadence state + audit for the scrape scheduler.

last_success_at backs the 24h cadence gate; latest_per_source backs /health.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.repositories.scrape_runs import ScrapeRunRepository


@pytest.mark.asyncio
async def test_last_success_at_parses_timestamp():
    iso = "2026-06-17T04:00:00+00:00"
    client = MagicMock()
    chain = (
        client.table.return_value.select.return_value
        .eq.return_value.eq.return_value
        .order.return_value.limit.return_value
    )
    chain.execute.return_value = MagicMock(data=[{"finished_at": iso}])

    ts = await ScrapeRunRepository(client).last_success_at("adzuna")

    assert ts == datetime(2026, 6, 17, 4, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_last_success_at_none_when_no_runs():
    client = MagicMock()
    chain = (
        client.table.return_value.select.return_value
        .eq.return_value.eq.return_value
        .order.return_value.limit.return_value
    )
    chain.execute.return_value = MagicMock(data=[])

    ts = await ScrapeRunRepository(client).last_success_at("adzuna")

    assert ts is None


@pytest.mark.asyncio
async def test_record_start_returns_id():
    client = MagicMock()
    client.table.return_value.insert.return_value.execute.return_value = MagicMock(
        data=[{"id": "run-123"}]
    )

    run_id = await ScrapeRunRepository(client).record_start("adzuna")

    assert run_id == "run-123"


@pytest.mark.asyncio
async def test_reclaim_stale_running_marks_failed():
    """Startup reclaim closes orphaned 'running' rows (status→failed)."""
    client = MagicMock()
    client.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "a"}, {"id": "b"}]
    )

    n = await ScrapeRunRepository(client).reclaim_stale_running()

    assert n == 2
    update_arg = client.table.return_value.update.call_args[0][0]
    assert update_arg["status"] == "failed"
    assert "finished_at" in update_arg
    # only 'running' rows are reclaimed
    client.table.return_value.update.return_value.eq.assert_called_once_with("status", "running")


@pytest.mark.asyncio
async def test_latest_per_source_keeps_newest_per_source():
    """Rows arrive newest-first; keep the first (newest) seen per source."""
    client = MagicMock()
    client.table.return_value.select.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
        data=[
            {"source": "adzuna", "status": "done", "started_at": "2026-06-17T04:00:00+00:00", "finished_at": "2026-06-17T04:01:00+00:00", "stats": {"jobs_stored": 7}},
            {"source": "indeed", "status": "failed", "started_at": "2026-06-17T03:00:00+00:00", "finished_at": None, "stats": None},
            {"source": "adzuna", "status": "done", "started_at": "2026-06-16T04:00:00+00:00", "finished_at": "2026-06-16T04:01:00+00:00", "stats": {"jobs_stored": 3}},
        ]
    )

    rows = await ScrapeRunRepository(client).latest_per_source()

    by_source = {r["source"]: r for r in rows}
    assert set(by_source) == {"adzuna", "indeed"}
    assert by_source["adzuna"]["stats"] == {"jobs_stored": 7}, "newest adzuna row wins"
    assert by_source["indeed"]["status"] == "failed"
