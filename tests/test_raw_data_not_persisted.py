"""
Guard: RawJobRepository.insert_batch must NOT persist raw_data (L1, 2026-07-02).

raw_data was 66% of the raw_jobs table (~106 MB) yet the refine pipeline never reads
it back from the DB. It is dropped from staging to reclaim space and keep every insert
light. The blob still lives on the in-memory RawJob (adapters read it during the same
fetch) — this test only asserts it never reaches the persisted row, while the real
columns (title/url/source/external_id/content_hash) still do.
Tests one RawJob per representative source payload shape.
"""
import pytest
from unittest.mock import MagicMock

from app.models.job import RawJob
from app.repositories.raw_jobs import RawJobRepository


class _FakeTable:
    """Supports the insert_batch chain: select(...).eq/neq/range().execute() for the
    existing-key pre-filter (returns no existing rows), and insert(list).execute()."""

    def __init__(self, captured: list):
        self._captured = captured
        self._mode = None
        self._rows: list = []

    def select(self, *a, **k):
        self._mode = "select"
        return self

    def eq(self, *a, **k):
        return self

    def neq(self, *a, **k):
        return self

    def range(self, *a, **k):
        return self

    def insert(self, rows):
        self._mode = "insert"
        self._rows = rows if isinstance(rows, list) else [rows]
        return self

    def execute(self):
        if self._mode == "select":
            return MagicMock(data=[])  # nothing known yet → all rows are fresh
        self._captured.extend(self._rows)
        return MagicMock(data=self._rows)


class _FakeClient:
    def __init__(self, captured: list):
        self._captured = captured

    def table(self, _name: str) -> _FakeTable:
        return _FakeTable(self._captured)


def _make_repo() -> RawJobRepository:
    return RawJobRepository(MagicMock())


# ---------------------------------------------------------------------------
# Per-source payload fixtures
# ---------------------------------------------------------------------------

_SOURCE_PAYLOADS = {
    "adzuna": {
        "id": "az-42",
        "title": "Data Engineer",
        "company": {"display_name": "ACME"},
        "location": {"display_name": "Berlin, Germany"},
        "description": "Build our data platform.",
        "redirect_url": "https://www.adzuna.de/jobs/details/42",
    },
    "indeed": {
        "job_id": "in-99",
        "title": "Backend Developer",
        "company": "StartupX",
        "location": "Hamburg",
        "description": "FastAPI, Python, Postgres",
        "job_url": "https://de.indeed.com/viewjob?jk=99",
    },
    "rss": {
        "id": "rss-7",
        "title": "Agile Coach",
        "link": "https://feed.example.com/jobs/7",
        "summary": "Full-time Agile Coach role in Munich.",
        "published": "2026-06-01T09:00:00Z",
    },
    "greenhouse": {
        "id": 1234,
        "title": "Senior Software Engineer",
        "absolute_url": "https://boards.greenhouse.io/acme/jobs/1234",
        "location": {"name": "Remote"},
        "content": "<p>Full JD here</p>",
    },
}


@pytest.mark.parametrize("source,payload", _SOURCE_PAYLOADS.items())
@pytest.mark.asyncio
async def test_raw_data_not_persisted_for_source(source: str, payload: dict):
    """The persisted staging row must exclude raw_data but keep the real columns."""
    repo = _make_repo()

    job = RawJob(
        title=payload.get("title", ""),
        url=(
            payload.get("redirect_url")
            or payload.get("job_url")
            or payload.get("link")
            or payload.get("absolute_url")
            or "https://example.com"
        ),
        source=source,
        external_id=str(payload.get("id") or payload.get("job_id") or ""),
        raw_data=payload,
    )

    inserted_rows: list[dict] = []
    repo.client = _FakeClient(inserted_rows)

    count = await repo.insert_batch([job])

    assert count == 1
    assert len(inserted_rows) == 1
    row = inserted_rows[0]
    # L1: raw_data must NOT be written to staging (dead weight, never read by refine)
    assert "raw_data" not in row, f"raw_data must not be persisted for source '{source}'"
    # the columns refine actually needs are still there
    assert row["source"] == source
    assert row["external_id"] == str(payload.get("id") or payload.get("job_id") or "")
    assert "content_hash" in row
