"""Autonomous refine runner: drain-until-empty, single-flight, scheduler loop.

These guard the engine's self-triggering: the refine step must run from inside
the app (not depend on an external n8n cron), drain a backlog in one cycle, and
never let two drains run concurrently against the un-claimed raw_jobs inbox.
"""
import asyncio
from unittest.mock import MagicMock

import pytest

import app.services.refine_runner as runner


@pytest.fixture(autouse=True)
def _reset_guard():
    """Each test starts with the single-flight guard released."""
    runner._refine_running = False
    yield
    runner._refine_running = False


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
async def test_drain_single_flight_skips_when_running(monkeypatch):
    """A second drain is a no-op while one is already in flight."""
    _patch_pipeline(monkeypatch, [{"fetched": 0}])
    runner._refine_running = True  # simulate an in-flight drain

    result = await runner.drain(limit=200, max_passes=10)

    assert result == {"skipped": True}


@pytest.mark.asyncio
async def test_drain_releases_guard_after_completion(monkeypatch):
    """The guard must be released once a drain finishes (so the next can run)."""
    _patch_pipeline(monkeypatch, [{"fetched": 0, "refined": 0, "rejected": 0, "duplicate": 0, "errors": 0}])

    await runner.drain(limit=200, max_passes=10)

    assert runner._refine_running is False


@pytest.mark.asyncio
async def test_drain_releases_guard_on_exception(monkeypatch):
    """An exception mid-drain must still release the guard (no permanent lock)."""
    class _BoomPipeline:
        def __init__(self, _c):
            pass

        async def run(self, limit: int = 200):
            raise RuntimeError("boom")

    monkeypatch.setattr(runner, "RefinePipeline", _BoomPipeline)
    monkeypatch.setattr("app.dependencies.get_supabase", lambda: MagicMock())

    await runner.drain(limit=200, max_passes=10)  # swallows + logs

    assert runner._refine_running is False


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
