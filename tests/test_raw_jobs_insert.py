"""RawJobRepository.insert_batch — bulk insert with existing-key pre-filter.

A daily re-scrape re-fetches the same postings (jobs stay online for weeks), so
almost every row collides with the (source, external_id) unique index. insert_batch
must NOT issue one insert per already-known row: it pre-filters against existing keys
and bulk-inserts only the genuinely-new ones, falling back to per-row only on a
residual conflict.
"""
from unittest.mock import MagicMock

import pytest

from app.models.job import RawJob
from app.repositories.raw_jobs import RawJobRepository


class _FakeTable:
    def __init__(self, store: dict):
        self.store = store
        self._op = None
        self._source = None
        self._range = (0, 999)
        self._payload: list = []

    def select(self, *a, **k):
        self._op = "select"
        return self

    def eq(self, col, val):
        if col == "source":
            self._source = val
        return self

    def neq(self, *a, **k):
        return self

    def range(self, lo, hi):
        self._range = (lo, hi)
        return self

    def insert(self, rows):
        self._op = "insert"
        self._payload = rows if isinstance(rows, list) else [rows]
        return self

    def execute(self):
        if self._op == "select":
            eids = self.store["existing"].get(self._source, [])
            lo, hi = self._range
            return MagicMock(data=[{"external_id": e} for e in eids[lo : hi + 1]])
        # insert
        self.store["insert_calls"].append(len(self._payload))
        if len(self._payload) > 1 and self.store.get("fail_bulk_once"):
            self.store["fail_bulk_once"] = False
            raise Exception("duplicate key value violates unique constraint (23505)")
        self.store["inserted"].extend(self._payload)
        return MagicMock(data=self._payload)


class _FakeClient:
    def __init__(self, store: dict):
        self.store = store

    def table(self, _name: str) -> _FakeTable:
        return _FakeTable(self.store)


def _repo(store: dict) -> RawJobRepository:
    repo = RawJobRepository(MagicMock())
    repo.client = _FakeClient(store)
    return repo


def _job(external_id: str, source: str = "greenhouse") -> RawJob:
    return RawJob(
        title="Role",
        url=f"https://ex.com/{external_id or 'x'}",
        source=source,
        external_id=external_id,
        raw_data={"id": external_id},
    )


@pytest.mark.asyncio
async def test_prefilter_skips_already_known_keys():
    """Rows whose (source, external_id) already exist are never inserted."""
    store = {"existing": {"greenhouse": ["g1", "g2"]}, "inserted": [], "insert_calls": []}
    repo = _repo(store)

    count = await repo.insert_batch([_job("g1"), _job("g2"), _job("g3")])

    assert count == 1, "only the unseen g3 should be inserted"
    assert [r["external_id"] for r in store["inserted"]] == ["g3"]


@pytest.mark.asyncio
async def test_all_known_inserts_nothing():
    """The common daily case (every posting already seen) hits zero inserts."""
    store = {"existing": {"greenhouse": ["g1", "g2"]}, "inserted": [], "insert_calls": []}
    repo = _repo(store)

    count = await repo.insert_batch([_job("g1"), _job("g2")])

    assert count == 0
    assert store["insert_calls"] == [], "no insert round-trip when all rows are known"


@pytest.mark.asyncio
async def test_fresh_rows_bulk_inserted_in_one_call():
    """Genuinely-new rows go out as a single bulk insert, not row-by-row."""
    store = {"existing": {}, "inserted": [], "insert_calls": []}
    repo = _repo(store)

    count = await repo.insert_batch([_job("g1"), _job("g2"), _job("g3")])

    assert count == 3
    assert store["insert_calls"] == [3], "one bulk insert of 3, not 3 single inserts"


@pytest.mark.asyncio
async def test_empty_external_id_always_inserted():
    """external_id='' is outside the partial unique index → never pre-filtered out."""
    store = {"existing": {"greenhouse": ["g1"]}, "inserted": [], "insert_calls": []}
    repo = _repo(store)

    count = await repo.insert_batch([_job(""), _job("g1")])

    assert count == 1, "the empty-external_id row inserts; the known g1 is skipped"
    assert [r["external_id"] for r in store["inserted"]] == [""]


@pytest.mark.asyncio
async def test_residual_conflict_falls_back_to_per_row():
    """A chunk-level 23505 (race) retries the chunk row-by-row, losing no good rows."""
    store = {"existing": {}, "inserted": [], "insert_calls": [], "fail_bulk_once": True}
    repo = _repo(store)

    count = await repo.insert_batch([_job("g1"), _job("g2"), _job("g3")])

    assert count == 3, "bulk failed once → per-row fallback still inserts all three"
    # first call is the failed bulk (3), then three per-row inserts (1 each)
    assert store["insert_calls"] == [3, 1, 1, 1]
