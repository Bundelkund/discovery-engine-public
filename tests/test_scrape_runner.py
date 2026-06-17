"""Autonomous scrape runner: daily cadence gate, single-flight, scheduler loop.

These guard the engine's self-triggering of scrapes: the fetch path must run from
inside the app (not depend on an external n8n cron), scrape each source at most once
per window (a redeploy must NOT re-hit external/paid APIs), isolate a failing source
from the rest, and never let two cycles overlap.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.services.scrape_runner as runner


@pytest.fixture(autouse=True)
def _reset_guard_and_pause(monkeypatch):
    """Each test starts with the guard released and no inter-source sleep."""
    runner._scrape_running = False
    monkeypatch.setattr(runner, "_INTER_SOURCE_PAUSE_S", 0)
    monkeypatch.setattr("app.dependencies.get_supabase", lambda: MagicMock())
    yield
    runner._scrape_running = False


class _FakeRuns:
    """Stand-in for ScrapeRunRepository — records calls, returns canned last-success."""

    def __init__(self, last_map: dict | None = None):
        self.last_map = last_map or {}
        self.started: list[str] = []
        self.finished: list[tuple] = []
        self._n = 0

    async def last_success_at(self, source: str):
        return self.last_map.get(source)

    async def record_start(self, source: str) -> str:
        self.started.append(source)
        self._n += 1
        return f"run-{self._n}"

    async def record_finish(self, run_id, status, stats=None, error=None):
        self.finished.append((run_id, status, stats, error))


class _FakeOrch:
    """Stand-in for ScrapeOrchestrator. raises_for: sources that should error."""

    def __init__(self, _client, raises_for: set | None = None):
        self._raises_for = raises_for or set()

    async def run(self, source_id: str):
        if source_id in self._raises_for:
            raise RuntimeError(f"boom:{source_id}")
        return SimpleNamespace(jobs_found=10, jobs_stored=7, duration_ms=42)


def _patch(monkeypatch, sources, runs: _FakeRuns, raises_for=None):
    monkeypatch.setattr(runner, "_enabled_sources", lambda: sources)
    monkeypatch.setattr(runner, "ScrapeRunRepository", lambda _c: runs)
    monkeypatch.setattr(
        runner, "ScrapeOrchestrator", lambda c: _FakeOrch(c, raises_for or set())
    )


@pytest.mark.asyncio
async def test_run_due_scrapes_when_never_run(monkeypatch):
    """A source with no prior success is due → scraped + recorded 'done'."""
    runs = _FakeRuns(last_map={"adzuna": None})
    _patch(monkeypatch, ["adzuna"], runs)

    totals = await runner.run_due(min_interval_hours=24)

    assert totals["scraped"] == 1 and totals["skipped"] == 0
    assert runs.started == ["adzuna"]
    assert runs.finished[0][1] == "done"
    assert runs.finished[0][2] == {"jobs_found": 10, "jobs_stored": 7, "duration_ms": 42}


@pytest.mark.asyncio
async def test_run_due_skips_within_window(monkeypatch):
    """A source scraped 1h ago is NOT re-scraped under a 24h window (redeploy-safe)."""
    recent = datetime.now(timezone.utc) - timedelta(hours=1)
    runs = _FakeRuns(last_map={"adzuna": recent})
    _patch(monkeypatch, ["adzuna"], runs)

    totals = await runner.run_due(min_interval_hours=24)

    assert totals["skipped"] == 1 and totals["scraped"] == 0
    assert runs.started == [], "must not start a run for a source within the window"


@pytest.mark.asyncio
async def test_run_due_scrapes_when_stale(monkeypatch):
    """A source last scraped 30h ago is due again under a 24h window."""
    stale = datetime.now(timezone.utc) - timedelta(hours=30)
    runs = _FakeRuns(last_map={"adzuna": stale})
    _patch(monkeypatch, ["adzuna"], runs)

    totals = await runner.run_due(min_interval_hours=24)

    assert totals["scraped"] == 1


@pytest.mark.asyncio
async def test_run_due_isolates_source_failure(monkeypatch):
    """One source raising marks it 'failed' but the others still scrape."""
    runs = _FakeRuns(last_map={"indeed": None, "adzuna": None})
    _patch(monkeypatch, ["indeed", "adzuna"], runs, raises_for={"indeed"})

    totals = await runner.run_due(min_interval_hours=24)

    assert totals["failed"] == 1 and totals["scraped"] == 1
    statuses = {src: status for src, (_, status, _, _) in zip(runs.started, runs.finished)}
    assert statuses == {"indeed": "failed", "adzuna": "done"}


@pytest.mark.asyncio
async def test_run_due_single_flight_skips(monkeypatch):
    """A second cycle is a no-op while one is already running."""
    runs = _FakeRuns()
    _patch(monkeypatch, ["adzuna"], runs)
    runner._scrape_running = True

    result = await runner.run_due(min_interval_hours=24)

    assert result == {"skipped": True}
    assert runs.started == []


@pytest.mark.asyncio
async def test_run_due_releases_guard(monkeypatch):
    """The guard is released after a cycle so the next tick can run."""
    runs = _FakeRuns(last_map={"adzuna": None})
    _patch(monkeypatch, ["adzuna"], runs)

    await runner.run_due(min_interval_hours=24)

    assert runner.is_running() is False


@pytest.mark.asyncio
async def test_scheduler_loop_runs_then_stops_on_event(monkeypatch):
    """scheduler_loop runs a cycle and exits promptly when stop is set."""
    stop = asyncio.Event()
    cycles = {"n": 0}

    async def _fake_run_due(min_interval_hours):
        cycles["n"] += 1
        stop.set()
        return {"scraped": 0}

    monkeypatch.setattr(runner, "run_due", _fake_run_due)

    await asyncio.wait_for(
        runner.scheduler_loop(stop, check_interval_seconds=999, min_interval_hours=24),
        timeout=2.0,
    )

    assert cycles["n"] == 1
