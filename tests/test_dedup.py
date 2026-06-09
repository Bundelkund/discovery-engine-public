import asyncio
from unittest.mock import MagicMock

from app.deduplication.dedup import DeduplicationService
from app.models.job import NormalizedJob


def _make_job(url="http://test.com/1", title="Test Job", company="TestCo", external_id=""):
    return NormalizedJob(
        title=title,
        url=url,
        source="test",
        company=company,
        description="desc",
        content_hash="abc123",
        external_id=external_id,
    )


# ---------------------------------------------------------------------------
# filter_batch return shape — GAP 2.3b
# ---------------------------------------------------------------------------


def test_filter_batch_returns_three_tuple():
    """filter_batch must return (kept_jobs, dup_count, duplicate_indices)."""
    mock_client = MagicMock()
    mock_client.table.return_value.select.return_value.in_.return_value.execute.return_value.data = []
    service = DeduplicationService(mock_client)
    result = asyncio.run(service.filter_batch([_make_job()]))
    assert len(result) == 3, "filter_batch must return a 3-tuple"
    kept, dup_count, dup_indices = result
    assert isinstance(kept, list)
    assert isinstance(dup_count, int)
    assert isinstance(dup_indices, set)


def test_dedup_filters_known_urls_exposes_indices():
    """Known URLs result in non-empty duplicate_indices."""
    mock_client = MagicMock()

    def mock_select(col):
        select_result = MagicMock()

        def mock_in(in_col, values):
            in_result = MagicMock()
            if in_col == "url":
                in_result.execute.return_value.data = [{"url": "http://test.com/1"}]
            else:
                in_result.execute.return_value.data = []
            return in_result

        select_result.in_ = mock_in
        return select_result

    mock_client.table.return_value.select = mock_select

    service = DeduplicationService(mock_client)
    jobs = [_make_job("http://test.com/1"), _make_job("http://test.com/2")]
    kept, dup_count, dup_indices = asyncio.run(service.filter_batch(jobs))

    assert dup_count >= 1
    assert len(dup_indices) >= 1
    # Index 0 was the known URL — must be in dup_indices
    assert 0 in dup_indices
    # kept list must NOT contain the duplicate
    for job in kept:
        assert job.url != "http://test.com/1"


def test_dedup_passes_new_jobs():
    mock_client = MagicMock()
    mock_client.table.return_value.select.return_value.in_.return_value.execute.return_value.data = []

    service = DeduplicationService(mock_client)
    jobs = [_make_job("http://new.com/1"), _make_job("http://new.com/2")]
    kept, dup_count, dup_indices = asyncio.run(service.filter_batch(jobs))

    assert len(kept) == 2
    assert dup_count == 0
    assert dup_indices == set()


def test_dedup_empty_list():
    mock_client = MagicMock()
    service = DeduplicationService(mock_client)
    kept, dup_count, dup_indices = asyncio.run(service.filter_batch([]))

    assert kept == []
    assert dup_count == 0
    assert dup_indices == set()


def test_duplicate_indices_are_positions_in_original_list():
    """duplicate_indices must be the original positions, not offsets in kept list."""
    mock_client = MagicMock()

    def mock_select(col):
        sr = MagicMock()

        def mock_in(in_col, values):
            ir = MagicMock()
            # external_id "eid-middle" is a known duplicate
            if in_col == "external_id" and "eid-middle" in values:
                ir.execute.return_value.data = [{"external_id": "eid-middle"}]
            else:
                ir.execute.return_value.data = []
            return ir

        sr.in_ = mock_in
        return sr

    mock_client.table.return_value.select = mock_select

    jobs = [
        _make_job("http://a.com/1", external_id="eid-a"),
        _make_job("http://a.com/2", external_id="eid-middle"),
        _make_job("http://a.com/3", external_id="eid-c"),
    ]
    service = DeduplicationService(mock_client)
    kept, dup_count, dup_indices = asyncio.run(service.filter_batch(jobs))

    assert dup_count == 1
    assert dup_indices == {1}  # position 1 in original list
    assert len(kept) == 2
    assert all(j.external_id != "eid-middle" for j in kept)


# ---------------------------------------------------------------------------
# _batch_check targets configured table — GAP 4.5b
# ---------------------------------------------------------------------------


def test_batch_check_uses_active_shelf_by_default():
    """With no pinned table, _batch_check resolves the active shelf from settings
    (the read-switch) rather than a hardcoded default. (F4/F5)"""
    from app.config import get_settings

    mock_client = MagicMock()
    mock_client.table.return_value.select.return_value.in_.return_value.execute.return_value.data = []

    service = DeduplicationService(mock_client)
    asyncio.run(service.filter_batch([_make_job("http://x.com/1")]))

    called_tables = [c.args[0] for c in mock_client.table.call_args_list]
    assert get_settings().jobs_table in called_tables


def test_batch_check_uses_configured_table():
    """When jobs_table='jobs_v2' is passed at construction, _batch_check queries jobs_v2."""
    mock_client = MagicMock()
    mock_client.table.return_value.select.return_value.in_.return_value.execute.return_value.data = []

    service = DeduplicationService(mock_client, jobs_table="jobs_v2")
    asyncio.run(service.filter_batch([_make_job("http://x.com/2")]))

    called_tables = [c.args[0] for c in mock_client.table.call_args_list]
    assert "jobs_v2" in called_tables
    assert "jobs" not in called_tables


def test_batch_check_never_queries_old_table_when_overridden():
    """Verify hardcoded 'jobs' cannot leak when jobs_table='jobs_v2' is set."""
    mock_client = MagicMock()
    mock_client.table.return_value.select.return_value.in_.return_value.execute.return_value.data = []

    service = DeduplicationService(mock_client, jobs_table="jobs_v2")
    jobs = [
        _make_job("http://x.com/1", external_id="e1"),
        _make_job("http://x.com/2", external_id="e2"),
    ]
    asyncio.run(service.filter_batch(jobs))

    called_tables = {c.args[0] for c in mock_client.table.call_args_list}
    assert "jobs" not in called_tables
    assert "jobs_v2" in called_tables
