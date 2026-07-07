"""Autonomous scrape runner: daily cadence gate, per-source DB claim, scheduler loop.

These guard the engine's self-triggering of scrapes: the fetch path must run from
inside the app (not depend on an external n8n cron), scrape each source at most once
per window (a redeploy must NOT re-hit external/paid APIs), isolate a failing source
from the rest. AUDIT-P1-04: the in-process ``_scrape_running`` bool is gone —
overlap protection is per-source in the DB (record_start's insert against the
one-running-per-source unique index; a lost claim skips the source), plus a
time-based stale-'running' reclaim at every cycle start.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.services.scrape_runner as runner


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch):
    """No inter-source sleep; stub the supabase client."""
    monkeypatch.setattr(runner, "_INTER_SOURCE_PAUSE_S", 0)
    monkeypatch.setattr("app.dependencies.get_supabase", lambda: MagicMock())
    yield


class _FakeRuns:
    """Stand-in for ScrapeRunRepository — records calls, returns canned last-success.

    ``deny``: sources whose record_start loses the DB claim (returns None).
    """

    def __init__(self, last_map: dict | None = None, deny: set | None = None):
        self.last_map = last_map or {}
        self.deny = deny or set()
        self.started: list[str] = []
        self.finished: list[tuple] = []
        self.reclaim_calls: list[datetime] = []
        self._n = 0

    async def last_success_at(self, source: str):
        return self.last_map.get(source)

    async def record_start(self, source: str) -> str | None:
        if source in self.deny:
            return None
        self.started.append(source)
        self._n += 1
        return f"run-{self._n}"

    async def record_finish(self, run_id, status, stats=None, error=None):
        self.finished.append((run_id, status, stats, error))

    async def reclaim_stale_running(self, stale_before: datetime) -> int:
        self.reclaim_calls.append(stale_before)
        return 0


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
async def test_run_due_source_timeout_marks_failed_and_continues(monkeypatch):
    """A source exceeding the per-source timeout is marked 'failed'; the next runs."""
    runs = _FakeRuns(last_map={"slowsrc": None, "adzuna": None})
    monkeypatch.setattr(runner, "_enabled_sources", lambda: ["slowsrc", "adzuna"])
    monkeypatch.setattr(runner, "ScrapeRunRepository", lambda _c: runs)

    class _SlowOrch:
        def __init__(self, _c):
            pass

        async def run(self, source_id: str):
            if source_id == "slowsrc":
                await asyncio.sleep(10)  # exceeds the tiny timeout below
            return SimpleNamespace(jobs_found=1, jobs_stored=1, duration_ms=1)

    monkeypatch.setattr(runner, "ScrapeOrchestrator", lambda c: _SlowOrch(c))

    totals = await runner.run_due(min_interval_hours=24, source_timeout_seconds=0.05)

    assert totals["failed"] == 1 and totals["scraped"] == 1
    statuses = {src: status for src, (_, status, _, _) in zip(runs.started, runs.finished)}
    assert statuses == {"slowsrc": "failed", "adzuna": "done"}


@pytest.mark.asyncio
async def test_run_due_skips_source_when_db_claim_lost(monkeypatch):
    """AUDIT-P1-04: a lost record_start claim (another worker/replica holds the
    source's 'running' row) skips THAT source — never scraped, never finished —
    while the sibling source still runs. Replaces the old in-process guard."""
    runs = _FakeRuns(last_map={"adzuna": None, "indeed": None}, deny={"adzuna"})
    _patch(monkeypatch, ["adzuna", "indeed"], runs)

    totals = await runner.run_due(min_interval_hours=24)

    assert totals["skipped"] == 1 and totals["scraped"] == 1
    assert runs.started == ["indeed"], "the denied source must not be scraped"
    statuses = {src: status for src, (_, status, _, _) in zip(runs.started, runs.finished)}
    assert statuses == {"indeed": "done"}


@pytest.mark.asyncio
async def test_run_due_reclaims_stale_running_first(monkeypatch):
    """Every cycle starts with the time-based stale-'running' reclaim: cutoff =
    now - (source_timeout + margin), so only provably-dead rows are reclaimed
    (a live scrape is wait_for-capped at source_timeout)."""
    runs = _FakeRuns(last_map={"adzuna": None})
    _patch(monkeypatch, ["adzuna"], runs)

    before = datetime.now(timezone.utc)
    await runner.run_due(min_interval_hours=24, source_timeout_seconds=1800)
    after = datetime.now(timezone.utc)

    assert len(runs.reclaim_calls) == 1
    cutoff = runs.reclaim_calls[0]
    expected_lo = before - timedelta(seconds=1800 + runner._RECLAIM_MARGIN_S)
    expected_hi = after - timedelta(seconds=1800 + runner._RECLAIM_MARGIN_S)
    assert expected_lo <= cutoff <= expected_hi


@pytest.mark.asyncio
async def test_run_due_survives_reclaim_failure(monkeypatch):
    """A reclaim error is best-effort: the cycle must still scrape due sources."""
    runs = _FakeRuns(last_map={"adzuna": None})

    async def _boom(stale_before):
        raise RuntimeError("db down")

    runs.reclaim_stale_running = _boom
    _patch(monkeypatch, ["adzuna"], runs)

    totals = await runner.run_due(min_interval_hours=24)

    assert totals["scraped"] == 1


@pytest.mark.asyncio
async def test_scheduler_loop_runs_then_stops_on_event(monkeypatch):
    """scheduler_loop runs a cycle and exits promptly when stop is set."""
    stop = asyncio.Event()
    cycles = {"n": 0}

    async def _fake_run_due(min_interval_hours, source_timeout_seconds=1800, daily_anchor_hour=None):
        cycles["n"] += 1
        stop.set()
        return {"scraped": 0}

    monkeypatch.setattr(runner, "run_due", _fake_run_due)

    await asyncio.wait_for(
        runner.scheduler_loop(stop, check_interval_seconds=999, min_interval_hours=24),
        timeout=2.0,
    )

    assert cycles["n"] == 1


# --- Anchored cadence (daily_anchor_hour) -------------------------------------
# The interval gate drifts ~1h later each day; the anchor pins "due" to a fixed
# UTC hour so completion time can't creep past downstream consumers (the digest).

def _freeze_now(monkeypatch, fixed: datetime):
    """Pin runner.datetime.now() to a fixed instant for deterministic anchor math."""
    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed
    monkeypatch.setattr(runner, "datetime", _FixedDatetime)


@pytest.mark.asyncio
async def test_anchor_scrapes_when_last_before_todays_anchor(monkeypatch):
    """Last success before today's anchor (03:00) → due, even if <24h ago."""
    _freeze_now(monkeypatch, datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc))
    runs = _FakeRuns(last_map={"adzuna": datetime(2026, 6, 27, 2, 30, tzinfo=timezone.utc)})
    _patch(monkeypatch, ["adzuna"], runs)

    totals = await runner.run_due(daily_anchor_hour=3)

    assert totals["scraped"] == 1 and totals["skipped"] == 0


@pytest.mark.asyncio
async def test_anchor_skips_when_last_after_todays_anchor(monkeypatch):
    """Already scraped after today's anchor → skip for the rest of the day (redeploy-safe)."""
    _freeze_now(monkeypatch, datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc))
    runs = _FakeRuns(last_map={"adzuna": datetime(2026, 6, 27, 4, 0, tzinfo=timezone.utc)})
    _patch(monkeypatch, ["adzuna"], runs)

    totals = await runner.run_due(daily_anchor_hour=3)

    assert totals["skipped"] == 1 and totals["scraped"] == 0
    assert runs.started == []


@pytest.mark.asyncio
async def test_anchor_uses_yesterday_when_before_anchor_hour(monkeypatch):
    """Before the anchor hour, the anchor is yesterday's; a run after it still skips."""
    _freeze_now(monkeypatch, datetime(2026, 6, 27, 1, 0, tzinfo=timezone.utc))
    runs = _FakeRuns(last_map={"adzuna": datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)})
    _patch(monkeypatch, ["adzuna"], runs)

    totals = await runner.run_due(daily_anchor_hour=3)

    assert totals["skipped"] == 1 and totals["scraped"] == 0
