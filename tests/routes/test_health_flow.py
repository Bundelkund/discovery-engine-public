"""GET /health `flow` block — P1 flow diagnostics (.specs/p1-flow-diagnostics.md).

Contract: 6 required fields (wip_new, throughput_24h, duplicate_rate_24h,
arrivals_24h, last_cycle, wip_gate), computed best-effort — an empty
refine_runs table (first deploy) yields zeros, never an error.
Mock strategy mirrors tests/test_jobs_api.py (TestClient + dependency override).
"""
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_supabase
from app.main import app


def _make_flow_supabase(
    wip_count: int = 0,
    cycles: list[dict] | None = None,
    scrape_stats_rows: list[dict] | None = None,
) -> MagicMock:
    """Mock supabase client wired for every chain the /health route touches.

    Chains (distinct MagicMock paths, so no cross-talk):
      select→limit                → jobs_v2 coverage total (count)
      select→eq→limit             → raw_jobs count (wip + backlog, count)
      select→eq→order→limit       → raw_jobs oldest ingested_at (data)
      select→order→limit          → scrape_runs latest_per_source (data)
      select→gte→order→limit      → refine_runs last-24h cycles (data)
      select→gte                  → scrape_runs 24h arrivals (data)
    """
    client = MagicMock()
    sel = client.table.return_value.select.return_value
    sel.limit.return_value.execute.return_value = MagicMock(count=0, data=[])
    sel.eq.return_value.limit.return_value.execute.return_value = MagicMock(
        count=wip_count, data=[]
    )
    sel.eq.return_value.order.return_value.limit.return_value.execute.return_value = (
        MagicMock(data=[])
    )
    sel.order.return_value.limit.return_value.execute.return_value = MagicMock(data=[])
    sel.gte.return_value.order.return_value.limit.return_value.execute.return_value = (
        MagicMock(data=cycles or [])
    )
    sel.gte.return_value.execute.return_value = MagicMock(data=scrape_stats_rows or [])
    return client


@pytest.fixture
def flow_client():
    """TestClient factory: pass a mock supabase, get a client; auto-cleanup."""
    def _build(mock_supabase: MagicMock) -> TestClient:
        app.dependency_overrides[get_supabase] = lambda: mock_supabase
        return TestClient(app)

    yield _build
    app.dependency_overrides.clear()


CYCLES = [
    {
        "id": 2,
        "finished_at": "2026-07-06T12:00:00+00:00",
        "stats": {"refined": 145, "duplicate": 43, "rejected": 12},
    },
    {
        "id": 1,
        "finished_at": "2026-07-06T11:00:00+00:00",
        "stats": {"refined": 100, "duplicate": 20, "rejected": 5},
    },
]
SCRAPE_ROWS = [{"stats": {"jobs_stored": 4000}}, {"stats": {"jobs_stored": 2380}}, {"stats": None}]


def test_health_flow_block_exists(flow_client):
    """GET /health returns a flow block carrying all 6 required fields."""
    client = flow_client(_make_flow_supabase(wip_count=8234, cycles=CYCLES,
                                             scrape_stats_rows=SCRAPE_ROWS))
    response = client.get("/health")

    assert response.status_code == 200
    assert "flow" in response.json()
    flow = response.json()["flow"]
    for field in ("wip_new", "throughput_24h", "duplicate_rate_24h",
                  "arrivals_24h", "last_cycle", "wip_gate"):
        assert field in flow, f"missing flow field: {field}"
    assert flow["wip_new"] == 8234
    assert flow["throughput_24h"] == 245           # 145 + 100
    assert flow["duplicate_rate_24h"] == 0.25      # (43+12+20+5)/(245+80)
    assert flow["arrivals_24h"] == 6380            # 4000 + 2380, None-stats skipped
    assert flow["last_cycle"]["refined"] == 145    # newest cycle wins


def test_flow_wip_gate_state_calculation(flow_client, monkeypatch):
    """Gate state matches wip_new vs. soft_limit (env WIP_SOFT_LIMIT, default 30k)."""
    client = flow_client(_make_flow_supabase(wip_count=8234, cycles=CYCLES))
    flow = client.get("/health").json()["flow"]
    expected_state = (
        "throttled" if flow["wip_new"] >= flow["wip_gate"]["soft_limit"] else "open"
    )
    assert flow["wip_gate"]["state"] == expected_state
    assert flow["wip_gate"] == {"soft_limit": 30000, "state": "open"}

    # Lower the limit below current WIP → throttled.
    monkeypatch.setenv("WIP_SOFT_LIMIT", "1000")
    flow = client.get("/health").json()["flow"]
    assert flow["wip_gate"] == {"soft_limit": 1000, "state": "throttled"}


def test_flow_handles_missing_refine_runs_gracefully(flow_client):
    """Empty refine_runs (first deploy) → zeros and null last_cycle, not error."""
    client = flow_client(_make_flow_supabase(wip_count=0, cycles=[]))
    response = client.get("/health")

    assert response.status_code == 200
    flow = response.json()["flow"]
    assert flow["throughput_24h"] == 0
    assert flow["duplicate_rate_24h"] == 0
    assert flow["arrivals_24h"] == 0
    assert flow["last_cycle"] is None
    assert flow.get("error") is None
