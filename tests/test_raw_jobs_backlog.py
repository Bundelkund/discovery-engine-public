"""RawJobRepository.backlog_metrics — the stalled-refine early-warning signal.

Surfaced on /health so monitoring can alert when raw_jobs pile up as 'new'
(the exact failure that froze jobs_v2 for 8 days, undetected).
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.repositories.raw_jobs import RawJobRepository


def _repo_with(count: int, oldest_iso: str | None) -> RawJobRepository:
    """Wire a mock client so the two backlog queries return count + oldest row."""
    client = MagicMock()
    q = client.table.return_value.select.return_value.eq.return_value

    count_res = MagicMock(count=count)
    q.limit.return_value.execute.return_value = count_res

    oldest_rows = [{"ingested_at": oldest_iso}] if oldest_iso else []
    q.order.return_value.limit.return_value.execute.return_value = MagicMock(data=oldest_rows)

    return RawJobRepository(client)


@pytest.mark.asyncio
async def test_backlog_metrics_empty_inbox():
    repo = _repo_with(count=0, oldest_iso=None)
    m = await repo.backlog_metrics()
    assert m == {"new_count": 0, "oldest_new_age_hours": 0.0}


@pytest.mark.asyncio
async def test_backlog_metrics_reports_count_and_age():
    three_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    repo = _repo_with(count=4601, oldest_iso=three_hours_ago)

    m = await repo.backlog_metrics()

    assert m["new_count"] == 4601
    assert 2.5 <= m["oldest_new_age_hours"] <= 3.5, "age ~3h from oldest ingested_at"
