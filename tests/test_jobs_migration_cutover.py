"""
Cutover read-switch tests (Task #7).

Validates:
1. JOBS_TABLE env var controls which shelf JobRepository reads and writes.
2. DeduplicationService receives the active table name at construction.
3. Gate logic: copy populates v2 keys; report detects missing keys.

Production cutover runbook (not executed here — offline reference):
  1. python scripts/migrate_jobs_v2.py --copy     (idempotent upsert v1 → v2)
  2. python scripts/migrate_jobs_v2.py --report   (exit 0 = gate PASS: all keys present,
                                                   0 field mismatches in 100-row sample)
  3. Coolify → app helpful-hyena → Env Vars → set JOBS_TABLE=jobs_v2 → Redeploy
  4. Keep v1 table (jobs) available for rollback: revert JOBS_TABLE=jobs; redeploy.
  5. Once stable: python scripts/migrate_jobs_v2.py --apply-drop (prints SQL for operator)
     then apply via Supabase MCP / Dashboard.
  NEVER run --copy or --apply-drop against prod without verifying the gate first.
"""
import pytest
from unittest.mock import MagicMock, patch

from app.models.job import ScoredJob
from app.repositories.jobs import JobRepository
from app.deduplication.dedup import DeduplicationService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_repo(client=None) -> JobRepository:
    return JobRepository(client or MagicMock())


def _scored_job(source: str = "adzuna", external_id: str = "az-1") -> ScoredJob:
    return ScoredJob(
        title="Test Role",
        url=f"https://example.com/job/{external_id}",
        source=source,
        external_id=external_id,
    )


# ---------------------------------------------------------------------------
# 1. Read-switch: JOBS_TABLE controls the active shelf
# ---------------------------------------------------------------------------


def test_jobs_table_default_is_jobs_v2(monkeypatch):
    """With no override, JOBS_TABLE defaults to 'jobs_v2'."""
    # Clear any env override and reset lru_cache so we get a fresh Settings read.
    from app import config as cfg
    cfg.get_settings.cache_clear()
    monkeypatch.delenv("JOBS_TABLE", raising=False)

    repo = _make_repo()
    assert repo._table == "jobs_v2", (
        f"Default jobs shelf must be 'jobs_v2', got '{repo._table}'"
    )
    cfg.get_settings.cache_clear()


def test_jobs_table_env_override_to_v1(monkeypatch):
    """Setting JOBS_TABLE=jobs switches the shelf to v1 (rollback path)."""
    from app import config as cfg
    cfg.get_settings.cache_clear()
    monkeypatch.setenv("JOBS_TABLE", "jobs")

    repo = _make_repo()
    assert repo._table == "jobs", (
        f"JOBS_TABLE=jobs must route to 'jobs', got '{repo._table}'"
    )
    cfg.get_settings.cache_clear()


def test_jobs_table_env_override_explicit_v2(monkeypatch):
    """Explicitly setting JOBS_TABLE=jobs_v2 confirms the flag is read."""
    from app import config as cfg
    cfg.get_settings.cache_clear()
    monkeypatch.setenv("JOBS_TABLE", "jobs_v2")

    repo = _make_repo()
    assert repo._table == "jobs_v2"
    cfg.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 2. JobRepository.upsert uses _table (not hardcoded string)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_targets_active_shelf(monkeypatch):
    """upsert() must call client.table(self._table), not a hardcoded 'jobs_v2'."""
    from app import config as cfg
    cfg.get_settings.cache_clear()
    monkeypatch.setenv("JOBS_TABLE", "jobs_v2")

    repo = _make_repo()
    tables_called: list[str] = []

    mock_chain = MagicMock()
    mock_chain.upsert.return_value = mock_chain
    mock_chain.execute.return_value = MagicMock(data=[{}])

    def _table_spy(name):
        tables_called.append(name)
        return mock_chain

    repo.client.table = _table_spy

    await repo.upsert([_scored_job()])

    assert "jobs_v2" in tables_called, (
        f"upsert must write to jobs_v2 when JOBS_TABLE=jobs_v2, called: {tables_called}"
    )
    cfg.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_upsert_targets_v1_when_flag_set(monkeypatch):
    """upsert() targets 'jobs' when JOBS_TABLE=jobs (rollback path)."""
    from app import config as cfg
    cfg.get_settings.cache_clear()
    monkeypatch.setenv("JOBS_TABLE", "jobs")

    repo = _make_repo()
    tables_called: list[str] = []

    mock_chain = MagicMock()
    mock_chain.upsert.return_value = mock_chain
    mock_chain.execute.return_value = MagicMock(data=[{}])

    def _table_spy(name):
        tables_called.append(name)
        return mock_chain

    repo.client.table = _table_spy

    await repo.upsert([_scored_job()])

    assert "jobs" in tables_called, (
        f"upsert must write to 'jobs' when JOBS_TABLE=jobs, called: {tables_called}"
    )
    assert "jobs_v2" not in tables_called
    cfg.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 3. JobRepository.query reads from _table
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_reads_from_active_shelf(monkeypatch):
    """query() must read from self._table, not a hardcoded name."""
    from app import config as cfg
    cfg.get_settings.cache_clear()
    monkeypatch.setenv("JOBS_TABLE", "jobs_v2")

    repo = _make_repo()
    tables_called: list[str] = []

    mock_chain = MagicMock()
    mock_chain.select.return_value = mock_chain
    mock_chain.order.return_value = mock_chain
    mock_chain.limit.return_value = mock_chain
    mock_chain.offset.return_value = mock_chain
    mock_chain.execute.return_value = MagicMock(data=[], count=0)

    def _table_spy(name):
        tables_called.append(name)
        return mock_chain

    repo.client.table = _table_spy

    await repo.query()

    assert "jobs_v2" in tables_called, (
        f"query must read from jobs_v2 when JOBS_TABLE=jobs_v2, called: {tables_called}"
    )
    cfg.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 4. DeduplicationService receives jobs_table from config
# ---------------------------------------------------------------------------


def test_dedup_service_accepts_jobs_table_param(monkeypatch):
    """DeduplicationService must accept jobs_table constructor param."""
    from app import config as cfg
    cfg.get_settings.cache_clear()
    monkeypatch.setenv("JOBS_TABLE", "jobs_v2")

    active_table = cfg.get_settings().jobs_table
    dedup = DeduplicationService(MagicMock(), jobs_table=active_table)

    assert dedup.jobs_table == "jobs_v2", (
        f"DeduplicationService.jobs_table must be 'jobs_v2', got '{dedup.jobs_table}'"
    )
    cfg.get_settings.cache_clear()


def test_dedup_service_rollback_table(monkeypatch):
    """When JOBS_TABLE=jobs, DeduplicationService should be constructed with 'jobs'."""
    from app import config as cfg
    cfg.get_settings.cache_clear()
    monkeypatch.setenv("JOBS_TABLE", "jobs")

    active_table = cfg.get_settings().jobs_table
    dedup = DeduplicationService(MagicMock(), jobs_table=active_table)

    assert dedup.jobs_table == "jobs"
    cfg.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 5. Gate logic: copy populates v2, report detects missing keys
# ---------------------------------------------------------------------------


def test_migrate_gate_passes_when_all_keys_copied():
    """cmd_report gate 1 passes when all v1 keys exist in v2."""
    from scripts.migrate_jobs_v2 import cmd_report

    v1_rows = [
        {"source": "adzuna", "external_id": "az-1"},
        {"source": "greenhouse", "external_id": "gh-2"},
    ]
    v2_rows = [
        {"source": "adzuna", "external_id": "az-1"},
        {"source": "greenhouse", "external_id": "gh-2"},
    ]

    mock_client = MagicMock()

    def _execute_side_effect(table_name):
        chain = MagicMock()
        chain.select.return_value = chain
        chain.range.return_value = chain

        rows = v1_rows if table_name == "jobs" else v2_rows

        execute_result = MagicMock()
        execute_result.data = rows
        chain.execute.return_value = execute_result
        return chain

    mock_client.table = _execute_side_effect

    # Patch random.sample to return all shared keys (deterministic)
    with (
        patch("scripts.migrate_jobs_v2.random.sample", side_effect=lambda pop, k: list(pop)[:k]),
        patch("scripts.migrate_jobs_v2._fetch_all") as mock_fetch,
        patch("scripts.migrate_jobs_v2.print"),
    ):
        def _fake_fetch(client, table, select, page_size=1000):
            if table == "jobs":
                return v1_rows
            return v2_rows

        mock_fetch.side_effect = _fake_fetch

        # Gate 2 sample: mock single-row fetches to return matching data
        def _single_fetch(table_name):
            chain = MagicMock()
            chain.select.return_value = chain
            chain.eq.return_value = chain
            chain.single.return_value = chain
            execute_result = MagicMock()
            # Return same data from both tables — no mismatches
            execute_result.data = {"source": "adzuna", "external_id": "az-1", "title": "T", "url": "U", "company": "C"}
            chain.execute.return_value = execute_result
            return chain

        mock_client.table = _single_fetch

        gate_pass = cmd_report(mock_client)

    assert gate_pass is True, "Gate must PASS when all v1 keys exist in v2 with matching fields"


def test_migrate_gate_fails_when_keys_missing():
    """cmd_report gate 1 fails when v2 is missing keys from v1."""
    from scripts.migrate_jobs_v2 import cmd_report

    v1_rows = [
        {"source": "adzuna", "external_id": "az-1"},
        {"source": "greenhouse", "external_id": "gh-MISSING"},
    ]
    v2_rows = [
        {"source": "adzuna", "external_id": "az-1"},
        # gh-MISSING absent from v2
    ]

    with (
        patch("scripts.migrate_jobs_v2._fetch_all") as mock_fetch,
        patch("scripts.migrate_jobs_v2.print"),
    ):
        def _fake_fetch(client, table, select, page_size=1000):
            return v1_rows if table == "jobs" else v2_rows

        mock_fetch.side_effect = _fake_fetch

        gate_pass = cmd_report(MagicMock())

    assert gate_pass is False, "Gate must FAIL when v2 is missing v1 keys"
