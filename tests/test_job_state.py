"""
JobRepository.upsert and mark_expired state-machine tests.

upsert:  first insert sets first_seen_at = last_seen_at = now(), status='active'.
re-upsert: updates last_seen_at; does NOT overwrite first_seen_at.
mark_expired: sets status='expired' for rows not seen since threshold_days.
"""
import pytest
from unittest.mock import MagicMock

from app.models.job import ScoredJob
from app.repositories.jobs import JobRepository


def _make_repo() -> JobRepository:
    return JobRepository(MagicMock())


def _scored_job(source: str = "adzuna", external_id: str = "az-1") -> ScoredJob:
    return ScoredJob(
        title="Test Role",
        url=f"https://example.com/job/{external_id}",
        source=source,
        external_id=external_id,
    )


# ---------------------------------------------------------------------------
# Upsert: row shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_includes_last_seen_at():
    """upsert() must include last_seen_at in every row."""
    repo = _make_repo()

    upserted_rows: list[dict] = []

    def _fake_upsert(payload, on_conflict=None):
        rows = payload if isinstance(payload, list) else [payload]
        upserted_rows.extend(rows)
        mock = MagicMock()
        mock.execute.return_value = MagicMock(data=rows)
        return mock

    repo.client.table.return_value.upsert = _fake_upsert

    result = await repo.upsert([_scored_job()])

    assert result == [True]  # per-row success flag list (new contract)
    assert len(upserted_rows) == 1
    assert "last_seen_at" in upserted_rows[0], "last_seen_at must be set on every upsert"


@pytest.mark.asyncio
async def test_upsert_sets_status_active():
    """upsert() must set status='active' on the row."""
    repo = _make_repo()

    upserted_rows: list[dict] = []

    def _fake_upsert(payload, on_conflict=None):
        rows = payload if isinstance(payload, list) else [payload]
        upserted_rows.extend(rows)
        mock = MagicMock()
        mock.execute.return_value = MagicMock(data=rows)
        return mock

    repo.client.table.return_value.upsert = _fake_upsert

    await repo.upsert([_scored_job()])

    assert upserted_rows[0]["status"] == "active"


@pytest.mark.asyncio
async def test_upsert_scraped_at_is_refine_time_not_posting_date():
    """scraped_at must be the wall-clock refine time, NOT job.posted_at.

    Regression: storing posted_at in scraped_at made GET /jobs?max_age_days=N
    (filters scraped_at >= now-N) return nothing, because rows carried their
    original (often months-old) posting date. scraped_at must be ~now even when
    the job has a much older posted_at.
    """
    from datetime import datetime, timedelta, timezone

    repo = _make_repo()
    old_posted = datetime(2020, 1, 1, tzinfo=timezone.utc)
    job = _scored_job()
    job.posted_at = old_posted

    upserted_rows: list[dict] = []

    def _fake_upsert(payload, on_conflict=None):
        rows = payload if isinstance(payload, list) else [payload]
        upserted_rows.extend(rows)
        mock = MagicMock()
        mock.execute.return_value = MagicMock(data=rows)
        return mock

    repo.client.table.return_value.upsert = _fake_upsert

    await repo.upsert([job])

    scraped_at = datetime.fromisoformat(upserted_rows[0]["scraped_at"])
    assert scraped_at.year != 2020, "scraped_at must not be the posting date"
    assert datetime.now(timezone.utc) - scraped_at < timedelta(minutes=5), (
        "scraped_at must be ~now (the refine time)"
    )


@pytest.mark.asyncio
async def test_upsert_uses_conflict_on_source_external_id():
    """upsert() must use on_conflict='source,external_id'."""
    repo = _make_repo()

    conflict_kwarg: list[str] = []

    def _fake_upsert(row, on_conflict=None):
        conflict_kwarg.append(on_conflict)
        mock = MagicMock()
        mock.execute.return_value = MagicMock(data=[row])
        return mock

    repo.client.table.return_value.upsert = _fake_upsert

    await repo.upsert([_scored_job()])

    assert conflict_kwarg == ["source,external_id"], (
        f"Expected on_conflict='source,external_id', got {conflict_kwarg}"
    )


@pytest.mark.asyncio
async def test_upsert_no_profile_id_in_row():
    """upsert() row must NOT contain profile_id — agnostik invariant."""
    repo = _make_repo()

    upserted_rows: list[dict] = []

    def _fake_upsert(payload, on_conflict=None):
        rows = payload if isinstance(payload, list) else [payload]
        upserted_rows.extend(rows)
        mock = MagicMock()
        mock.execute.return_value = MagicMock(data=rows)
        return mock

    repo.client.table.return_value.upsert = _fake_upsert

    await repo.upsert([_scored_job()])

    assert "profile_id" not in upserted_rows[0], (
        "profile_id must NOT appear in upsert row — agnostik invariant"
    )


@pytest.mark.asyncio
async def test_upsert_empty_list_returns_empty():
    """upsert([]) must return [] without calling the DB."""
    repo = _make_repo()

    result = await repo.upsert([])

    assert result == []
    repo.client.table.assert_not_called()


@pytest.mark.asyncio
async def test_upsert_bulk_one_request_per_chunk():
    """A batch upserts as a single list request, not one request per row."""
    repo = _make_repo()

    calls: list = []

    def _fake_upsert(payload, on_conflict=None):
        calls.append(payload)
        rows = payload if isinstance(payload, list) else [payload]
        mock = MagicMock()
        mock.execute.return_value = MagicMock(data=rows)
        return mock

    repo.client.table.return_value.upsert = _fake_upsert

    result = await repo.upsert(
        [_scored_job(external_id="a"), _scored_job(external_id="b"), _scored_job(external_id="c")]
    )

    assert result == [True, True, True]
    assert len(calls) == 1, "3 jobs must upsert in ONE bulk request, not 3"
    assert isinstance(calls[0], list) and len(calls[0]) == 3


@pytest.mark.asyncio
async def test_upsert_falls_back_to_per_row_on_bulk_error():
    """If the bulk chunk errors, retry per-row so no good row is dropped."""
    repo = _make_repo()

    calls: list = []

    def _fake_upsert(payload, on_conflict=None):
        calls.append(payload)
        if isinstance(payload, list):
            raise Exception("bulk boom (e.g. ON CONFLICT cannot affect row twice)")
        mock = MagicMock()
        mock.execute.return_value = MagicMock(data=[payload])
        return mock

    repo.client.table.return_value.upsert = _fake_upsert

    result = await repo.upsert([_scored_job(external_id="a"), _scored_job(external_id="b")])

    assert result == [True, True], "per-row fallback still upserts both"
    assert isinstance(calls[0], list) and len(calls[0]) == 2, "first attempt is the bulk chunk"
    assert [c for c in calls[1:]] and all(isinstance(c, dict) for c in calls[1:]), (
        "fallback issues single-row upserts"
    )
    assert len(calls[1:]) == 2


# ---------------------------------------------------------------------------
# mark_expired
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_expired_calls_update_with_status_expired():
    """mark_expired() must set status='expired' via update() on jobs_v2."""
    repo = _make_repo()

    mock_chain = MagicMock()
    mock_chain.update.return_value = mock_chain
    mock_chain.lt.return_value = mock_chain
    mock_chain.eq.return_value = mock_chain
    mock_chain.execute.return_value = MagicMock(data=[{"id": "x"}])

    repo.client.table.return_value = mock_chain

    count = await repo.mark_expired(threshold_days=30)

    # Verify update was called with status='expired'
    mock_chain.update.assert_called_once_with({"status": "expired"})
    assert count == 1


@pytest.mark.asyncio
async def test_mark_expired_targets_jobs_v2():
    """mark_expired() must operate on the jobs_v2 table, not the legacy jobs table."""
    repo = _make_repo()

    repo.client.table.return_value.update.return_value.lt.return_value.eq.return_value.execute.return_value = MagicMock(data=[])

    await repo.mark_expired(threshold_days=14)

    table_calls = [c[0][0] for c in repo.client.table.call_args_list]
    assert "jobs_v2" in table_calls, (
        f"mark_expired must target jobs_v2, called with: {table_calls}"
    )
