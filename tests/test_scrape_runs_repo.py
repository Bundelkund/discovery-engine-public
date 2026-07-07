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
async def test_record_start_returns_none_when_claim_lost():
    """AUDIT-P1-04: a 23505 against the one-running-per-source unique index means
    another worker/replica already scrapes this source — record_start maps it to
    None (caller skips) instead of raising."""
    client = MagicMock()
    client.table.return_value.insert.return_value.execute.side_effect = Exception(
        '23505: duplicate key value violates unique constraint '
        '"uq_scrape_runs_one_running_per_source"'
    )

    run_id = await ScrapeRunRepository(client).record_start("adzuna")

    assert run_id is None


@pytest.mark.asyncio
async def test_record_start_propagates_non_conflict_errors():
    """Only the claim conflict is swallowed; other DB errors keep raising."""
    client = MagicMock()
    client.table.return_value.insert.return_value.execute.side_effect = Exception(
        "connection reset"
    )

    with pytest.raises(Exception, match="connection reset"):
        await ScrapeRunRepository(client).record_start("adzuna")


@pytest.mark.asyncio
async def test_reclaim_stale_running_marks_failed():
    """Time-based reclaim closes only 'running' rows STARTED before the cutoff
    (crash orphans) — never a sibling worker's younger in-flight run."""
    client = MagicMock()
    chain = client.table.return_value.update.return_value.eq.return_value.lt.return_value
    chain.execute.return_value = MagicMock(data=[{"id": "a"}, {"id": "b"}])

    cutoff = datetime(2026, 7, 6, 3, 0, tzinfo=timezone.utc)
    n = await ScrapeRunRepository(client).reclaim_stale_running(cutoff)

    assert n == 2
    update_arg = client.table.return_value.update.call_args[0][0]
    assert update_arg["status"] == "failed"
    assert "finished_at" in update_arg
    # only 'running' rows older than the cutoff are reclaimed
    client.table.return_value.update.return_value.eq.assert_called_once_with("status", "running")
    client.table.return_value.update.return_value.eq.return_value.lt.assert_called_once_with(
        "started_at", cutoff.isoformat()
    )


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
