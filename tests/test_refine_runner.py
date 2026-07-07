"""Autonomous refine runner: drain-until-empty, DB-claim concurrency, scheduler loop.

AUDIT-P1-04: the in-process ``_refine_running`` single-flight bool is gone.
Correctness under concurrency now rests on the DB-side atomic claim
(claim_refine_batch RPC: FOR UPDATE SKIP LOCKED + status='refining'), which holds
across uvicorn workers and replicas. These tests prove the drain (a) still loops
until the inbox is empty, (b) reclaims stale claims first, and (c) given an atomic
claim, two CONCURRENT drains process every job exactly once — zero double-processing.
"""
import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.refine_runner as runner


def _patch_reclaim(monkeypatch, count: int = 0, raises: bool = False):
    """Stub RawJobRepository in the runner; return the fake for call assertions."""
    fake_repo = MagicMock()
    if raises:
        fake_repo.reclaim_stale_refining = AsyncMock(side_effect=RuntimeError("db down"))
    else:
        fake_repo.reclaim_stale_refining = AsyncMock(return_value=count)
    monkeypatch.setattr(runner, "RawJobRepository", lambda _c: fake_repo)
    return fake_repo


def _patch_pipeline(monkeypatch, summaries: list[dict]):
    """Make RefinePipeline(...).run(limit=...) return `summaries` in order."""
    calls = {"n": 0}

    class _FakePipeline:
        def __init__(self, _client):
            pass

        async def run(self, limit: int = 200) -> dict:
            i = min(calls["n"], len(summaries) - 1)
            calls["n"] += 1
            return summaries[i]

    monkeypatch.setattr(runner, "RefinePipeline", _FakePipeline)
    monkeypatch.setattr("app.dependencies.get_supabase", lambda: MagicMock())
    _patch_reclaim(monkeypatch)
    return calls


@pytest.mark.asyncio
async def test_drain_loops_until_inbox_empty(monkeypatch):
    """drain() keeps running passes until one returns fewer than `limit` rows."""
    summaries = [
        {"fetched": 200, "refined": 200, "rejected": 0, "duplicate": 0, "errors": 0},
        {"fetched": 200, "refined": 190, "rejected": 5, "duplicate": 5, "errors": 0},
        {"fetched": 40, "refined": 40, "rejected": 0, "duplicate": 0, "errors": 0},
    ]
    calls = _patch_pipeline(monkeypatch, summaries)

    totals = await runner.drain(limit=200, max_passes=100)

    assert calls["n"] == 3, "should stop after the short (40 < 200) batch"
    assert totals["passes"] == 3
    assert totals["fetched"] == 440
    assert totals["refined"] == 430
    assert totals["duplicate"] == 5


@pytest.mark.asyncio
async def test_drain_respects_max_passes_cap(monkeypatch):
    """A full inbox (every pass == limit) stops at max_passes, never infinite."""
    full = {"fetched": 200, "refined": 200, "rejected": 0, "duplicate": 0, "errors": 0}
    calls = _patch_pipeline(monkeypatch, [full])

    totals = await runner.drain(limit=200, max_passes=3)

    assert calls["n"] == 3
    assert totals["passes"] == 3


@pytest.mark.asyncio
async def test_drain_reclaims_stale_claims_first(monkeypatch):
    """Every drain starts by releasing 'refining' claims orphaned by a crashed
    drain (AUDIT-P0-05 zombie lesson) — BEFORE claiming its own batches."""
    order: list[str] = []

    class _OrderPipeline:
        def __init__(self, _client):
            pass

        async def run(self, limit: int = 200) -> dict:
            order.append("pass")
            return {"fetched": 0, "refined": 0, "rejected": 0, "duplicate": 0, "errors": 0}

    monkeypatch.setattr(runner, "RefinePipeline", _OrderPipeline)
    monkeypatch.setattr("app.dependencies.get_supabase", lambda: MagicMock())
    fake_repo = MagicMock()

    async def _reclaim(stale_after_seconds):
        order.append("reclaim")
        return 3

    fake_repo.reclaim_stale_refining = AsyncMock(side_effect=_reclaim)
    monkeypatch.setattr(runner, "RawJobRepository", lambda _c: fake_repo)

    await runner.drain(limit=200, max_passes=10)

    assert order[0] == "reclaim", "stale claims must be released before claiming"
    fake_repo.reclaim_stale_refining.assert_awaited_once_with(runner._CLAIM_STALE_SECONDS)


@pytest.mark.asyncio
async def test_drain_survives_reclaim_failure(monkeypatch):
    """Reclaim is best-effort observably: a reclaim error must not block the drain."""
    calls = _patch_pipeline(
        monkeypatch,
        [{"fetched": 0, "refined": 0, "rejected": 0, "duplicate": 0, "errors": 0}],
    )
    _patch_reclaim(monkeypatch, raises=True)

    totals = await runner.drain(limit=200, max_passes=10)

    assert calls["n"] == 1, "the pipeline pass must still run"
    assert totals["passes"] == 1


@pytest.mark.asyncio
async def test_drain_swallows_pipeline_exception(monkeypatch):
    """An exception mid-drain is logged, not raised — the scheduler must survive."""
    class _BoomPipeline:
        def __init__(self, _c):
            pass

        async def run(self, limit: int = 200):
            raise RuntimeError("boom")

    monkeypatch.setattr(runner, "RefinePipeline", _BoomPipeline)
    monkeypatch.setattr("app.dependencies.get_supabase", lambda: MagicMock())
    _patch_reclaim(monkeypatch)

    totals = await runner.drain(limit=200, max_passes=10)  # must not raise

    assert totals["fetched"] == 0


@pytest.mark.asyncio
async def test_concurrent_drains_process_each_job_exactly_once(monkeypatch):
    """AUDIT-P1-04 acceptance: two drains running concurrently (= two uvicorn
    workers, a second replica, or POST /refine racing the scheduler) must never
    process the same raw_job twice.

    The DB-side claim is modelled by an atomic in-memory pop (exactly the
    atomicity Postgres gives claim_refine_batch: FOR UPDATE SKIP LOCKED + UPDATE
    in one transaction). The test proves (a) drain() no longer serializes or
    skips in-process — BOTH drains do real work — and (b) given an atomic claim,
    the union of processed work is disjoint and complete.
    """
    inbox = list(range(500))
    lock = threading.Lock()
    processed: list[int] = []

    class _ClaimingPipeline:
        def __init__(self, _client):
            pass

        async def run(self, limit: int = 200) -> dict:
            def _claim():
                # Atomic claim: pop up to `limit` rows under a lock — no row can
                # be handed to two callers (claim_refine_batch semantics).
                with lock:
                    batch = inbox[:limit]
                    del inbox[:limit]
                    return batch

            batch = await asyncio.to_thread(_claim)
            await asyncio.sleep(0)  # yield so the two drains interleave
            processed.extend(batch)
            return {
                "fetched": len(batch),
                "refined": len(batch),
                "rejected": 0,
                "duplicate": 0,
                "errors": 0,
            }

    monkeypatch.setattr(runner, "RefinePipeline", _ClaimingPipeline)
    monkeypatch.setattr("app.dependencies.get_supabase", lambda: MagicMock())
    _patch_reclaim(monkeypatch)

    t1, t2 = await asyncio.gather(
        runner.drain(limit=50, max_passes=100),
        runner.drain(limit=50, max_passes=100),
    )

    assert len(processed) == 500, "every job must be processed"
    assert len(set(processed)) == 500, "a job was double-processed"
    assert t1["fetched"] + t2["fetched"] == 500
    # No in-process single-flight remains: neither drain was skipped.
    assert "skipped" not in t1 and "skipped" not in t2
    assert t1["passes"] >= 1 and t2["passes"] >= 1, "both drains must do real work"


@pytest.mark.asyncio
async def test_scheduler_loop_drains_then_stops_on_event(monkeypatch):
    """scheduler_loop drains each cycle and exits promptly when stop is set."""
    stop = asyncio.Event()
    drained = {"n": 0}

    async def _fake_drain(limit, max_passes):
        drained["n"] += 1
        stop.set()  # ask the loop to exit after this cycle
        return {"fetched": 0}

    monkeypatch.setattr(runner, "drain", _fake_drain)

    await asyncio.wait_for(
        runner.scheduler_loop(stop, interval_seconds=999, limit=200, max_passes=100),
        timeout=2.0,
    )

    assert drained["n"] == 1
