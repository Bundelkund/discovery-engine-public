"""RawJobRepository claim + stale reclaim (AUDIT-P1-04).

fetch_new no longer SELECTs status='new' (two concurrent drains would fetch and
double-process the same rows); it delegates to the claim_refine_batch RPC, which
atomically flips the batch to 'refining' under FOR UPDATE SKIP LOCKED inside one
Postgres transaction. These tests pin the RPC contract (function name, params,
tier lists, no residual plain-SELECT path), prove disjointness of concurrent
claims given the RPC's atomicity, and pin the stale-claim reclaim query shape.
"""
import asyncio
import threading
from unittest.mock import MagicMock

import pytest

from app.repositories.raw_jobs import RawJobRepository


@pytest.mark.asyncio
async def test_fetch_new_claims_via_rpc_with_tier_params():
    """fetch_new must call the claim RPC with the limit and BOTH tier lists —
    the 3-tier source priority now lives inside the SQL ordering."""
    client = MagicMock()
    client.rpc.return_value.execute.return_value = MagicMock(data=[{"id": "a"}])

    rows = await RawJobRepository(client).fetch_new(limit=42)

    assert rows == [{"id": "a"}]
    fn, params = client.rpc.call_args.args
    assert fn == "claim_refine_batch"
    assert params == {
        "p_limit": 42,
        "p_priority_sources": RawJobRepository.REFINE_PRIORITY_SOURCES,
        "p_deferred_sources": RawJobRepository.REFINE_DEFERRED_SOURCES,
    }
    # No plain-SELECT fallback may remain — that would reopen the race.
    client.table.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_new_empty_inbox_returns_empty_list():
    client = MagicMock()
    client.rpc.return_value.execute.return_value = MagicMock(data=None)

    rows = await RawJobRepository(client).fetch_new(limit=10)

    assert rows == []


@pytest.mark.asyncio
async def test_concurrent_fetch_new_yields_disjoint_batches():
    """Given the RPC's atomicity (selection + claim in ONE transaction), two
    concurrent fetch_new calls must never receive the same row. The DB is
    modelled by a thread-locked pop inside rpc().execute() — precisely the
    guarantee FOR UPDATE SKIP LOCKED + UPDATE provides — and disjointness is
    asserted end-to-end through the repository."""
    inbox = [{"id": str(i)} for i in range(100)]
    lock = threading.Lock()

    class _FakeRPC:
        def __init__(self, limit: int):
            self._limit = limit

        def execute(self):
            with lock:
                batch = inbox[: self._limit]
                del inbox[: self._limit]
            return MagicMock(data=batch)

    client = MagicMock()
    client.rpc = lambda _fn, params: _FakeRPC(params["p_limit"])
    repo = RawJobRepository(client)

    b1, b2 = await asyncio.gather(repo.fetch_new(limit=60), repo.fetch_new(limit=60))

    ids1 = {r["id"] for r in b1}
    ids2 = {r["id"] for r in b2}
    assert not (ids1 & ids2), "concurrent claims handed out the same row twice"
    assert len(ids1 | ids2) == 100, "no row may be lost"


@pytest.mark.asyncio
async def test_reclaim_stale_refining_releases_only_stale_claims():
    """The reclaim flips ONLY status='refining' rows whose claim is older than the
    window back to 'new' (time-based — never steals a live sibling's claim)."""
    client = MagicMock()
    chain = (
        client.table.return_value.update.return_value
        .eq.return_value.lt.return_value
    )
    chain.execute.return_value = MagicMock(data=[{"id": "a"}, {"id": "b"}])

    n = await RawJobRepository(client).reclaim_stale_refining(stale_after_seconds=1800)

    assert n == 2
    update_arg = client.table.return_value.update.call_args[0][0]
    assert update_arg == {"status": "new", "refine_claimed_at": None}
    client.table.return_value.update.return_value.eq.assert_called_once_with(
        "status", "refining"
    )
    lt_args = client.table.return_value.update.return_value.eq.return_value.lt.call_args[0]
    assert lt_args[0] == "refine_claimed_at"
    assert isinstance(lt_args[1], str) and lt_args[1]  # ISO cutoff timestamp
