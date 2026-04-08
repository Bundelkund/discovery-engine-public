import asyncio
from unittest.mock import MagicMock

from app.deduplication.dedup import DeduplicationService
from app.models.job import NormalizedJob


def _make_job(url="http://test.com/1", title="Test Job", company="TestCo"):
    return NormalizedJob(
        title=title,
        url=url,
        source="test",
        company=company,
        description="desc",
        content_hash="abc123",
    )


def test_dedup_filters_known_urls():
    mock_client = MagicMock()

    # _batch_check calls client.table("jobs").select(col).in_(col, chunk).execute()
    # We need to make in_ return different results based on column
    def mock_select(col):
        select_result = MagicMock()

        def mock_in(in_col, values):
            in_result = MagicMock()
            if in_col == "url":
                in_result.execute.return_value.data = [
                    {"url": "http://test.com/1"}
                ]
            else:
                in_result.execute.return_value.data = []
            return in_result

        select_result.in_ = mock_in
        return select_result

    mock_client.table.return_value.select = mock_select

    service = DeduplicationService(mock_client)
    jobs = [_make_job("http://test.com/1"), _make_job("http://test.com/2")]
    filtered, dup_count = asyncio.run(service.filter_batch(jobs))
    assert dup_count >= 1


def test_dedup_passes_new_jobs():
    mock_client = MagicMock()
    mock_client.table.return_value.select.return_value.in_.return_value.execute.return_value.data = (
        []
    )

    service = DeduplicationService(mock_client)
    jobs = [_make_job("http://new.com/1"), _make_job("http://new.com/2")]
    filtered, dup_count = asyncio.run(service.filter_batch(jobs))
    assert len(filtered) == 2
    assert dup_count == 0


def test_dedup_empty_list():
    mock_client = MagicMock()
    service = DeduplicationService(mock_client)
    filtered, dup_count = asyncio.run(service.filter_batch([]))
    assert filtered == []
    assert dup_count == 0
