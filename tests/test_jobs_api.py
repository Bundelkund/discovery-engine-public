"""
Tests for the consumer-agnostic GET /jobs Query API (Phase 3 AC-001, AC-015-AC-018).

Strategy: mock JobRepository.query() to avoid live DB dependency.
Each test validates a distinct filter or response-shape contract.
"""
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from app.main import app
from app.dependencies import get_consumer, get_supabase

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TEST_API_KEY = "test-key"


def _make_job_row(**kwargs) -> dict:
    defaults = {
        "id": "job-1",
        "title": "Python Developer",
        "company": "Acme GmbH",
        "location": "Berlin",
        "remote": True,
        "description": "We are looking for a Python developer with FastAPI skills.",
        "url": "https://acme.de/jobs/1",
        "source": "greenhouse",
        "external_id": "ext-1",
        "content_hash": "abc123",
        "score_stage_1": 80,
        "archetype": "backend",
        "company_domain": "acme.de",
        "salary_min": 60000,
        "salary_max": 90000,
        "scraped_at": "2026-04-20T10:00:00+00:00",
        "keywords": ["python", "fastapi"],
        "metadata": {},
    }
    defaults.update(kwargs)
    return defaults


@pytest.fixture
def mock_supabase():
    """Minimal mock — query() on JobRepository is patched separately."""
    client = MagicMock()
    return client


@pytest.fixture
def client(mock_supabase):
    """TestClient with auth and supabase overridden."""
    from app.dependencies import ConsumerIdentity

    app.dependency_overrides[get_supabase] = lambda: mock_supabase
    app.dependency_overrides[get_consumer] = lambda: ConsumerIdentity(
        id="test-consumer", name="Test", scopes=["jobs:read"]
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patch_query(rows: list[dict], total: int | None = None):
    """Return a patch context for JobRepository.query."""
    if total is None:
        total = len(rows)
    return patch(
        "app.routes.jobs_api.JobRepository.query",
        return_value=(rows, total),
    )


# ---------------------------------------------------------------------------
# AC-001: Response shape contract
# ---------------------------------------------------------------------------


def test_query_api_contract(client):
    """GET /jobs returns 200 with correct {jobs, total, limit, offset} shape."""
    rows = [_make_job_row()]
    with _patch_query(rows, total=42):
        resp = client.get(
            "/jobs",
            params={
                "keywords_positive": "python",
                "location": "Berlin",
                "max_age_days": 30,
                "limit": 5,
                "offset": 0,
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "jobs" in body
    assert "total" in body
    assert "limit" in body
    assert "offset" in body
    assert body["total"] == 42
    assert body["limit"] == 5
    assert body["offset"] == 0
    assert isinstance(body["jobs"], list)
    assert len(body["jobs"]) == 1


def test_query_api_empty_result(client):
    """Empty results return 200 with empty jobs list."""
    with _patch_query([], total=0):
        resp = client.get("/jobs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["jobs"] == []
    assert body["total"] == 0


# ---------------------------------------------------------------------------
# AC-001: keywords_positive
# ---------------------------------------------------------------------------


def test_keywords_positive_filter(client):
    """keywords_positive param is forwarded to repo.query correctly."""
    rows = [_make_job_row(title="Senior Python Developer")]
    with _patch_query(rows) as mock_q:
        resp = client.get("/jobs?keywords_positive=python&keywords_positive=fastapi")
    assert resp.status_code == 200
    call_kwargs = mock_q.call_args.kwargs
    assert "python" in call_kwargs["keywords_positive"]
    assert "fastapi" in call_kwargs["keywords_positive"]


def test_keywords_positive_uses_ranked_rpc():
    """Unit-test: keywords_positive routes to the search_jobs_ranked RPC (E1 ranking).

    The RPC ORDER BYs ts_rank DESC server-side; the old unranked PostgREST wfts path
    buried selective terms past the page window (recall blocker, 2026-06-08).
    """
    from unittest.mock import MagicMock

    mock_client = MagicMock()
    mock_client.rpc.return_value.execute.return_value = MagicMock(
        data=[{"result": _make_job_row(), "total_count": 5103}]
    )

    from app.repositories.jobs import JobRepository
    import asyncio

    repo = JobRepository(mock_client)
    rows, total = asyncio.run(
        repo.query(keywords_positive=["agile coach", "scrum master"])
    )
    assert total == 5103
    assert len(rows) == 1
    # RPC called with the ranked-search function + correct shelf/terms.
    name, params = mock_client.rpc.call_args[0]
    assert name == "search_jobs_ranked"
    assert params["p_table"] == "jobs"
    assert params["p_keywords_positive"] == ["agile coach", "scrum master"]
    # The legacy unranked PostgREST path must NOT be used.
    mock_client.table.assert_not_called()


def test_keywords_positive_with_distance_falls_back_to_legacy():
    """keywords_positive + max_distance_km uses the legacy PostgREST path (no RPC).

    The ranked RPC has no bbox-prefilter equivalent yet, so distance+keywords keeps
    the wfts path. Guards the fallback branch in query()."""
    from unittest.mock import MagicMock

    mock_client = MagicMock()
    chain = MagicMock()
    chain.execute.return_value = MagicMock(data=[_make_job_row()], count=1)
    (
        mock_client.table.return_value
        .select.return_value
        .or_.return_value
        .ilike.return_value
        .order.return_value
        .limit.return_value
        .offset.return_value
    ) = chain

    from app.repositories.jobs import JobRepository
    import asyncio

    repo = JobRepository(mock_client)
    rows, total = asyncio.run(
        repo.query(keywords_positive=["python"], location="Berlin", max_distance_km=50)
    )
    # Legacy wfts path used; RPC NOT called.
    mock_client.rpc.assert_not_called()
    call_args = mock_client.table.return_value.select.return_value.or_.call_args
    assert "title.wfts(simple).python" in call_args[0][0]


# ---------------------------------------------------------------------------
# AC-001: keywords_negative
# ---------------------------------------------------------------------------


def test_keywords_negative_filter(client):
    """keywords_negative param is forwarded to repo.query correctly."""
    with _patch_query([]) as mock_q:
        resp = client.get("/jobs?keywords_negative=sales&keywords_negative=marketing")
    assert resp.status_code == 200
    call_kwargs = mock_q.call_args.kwargs
    assert "sales" in call_kwargs["keywords_negative"]
    assert "marketing" in call_kwargs["keywords_negative"]


# ---------------------------------------------------------------------------
# AC-001: max_age_days
# ---------------------------------------------------------------------------


def test_max_age_days(client):
    """max_age_days param is forwarded to repo.query."""
    with _patch_query([]) as mock_q:
        resp = client.get("/jobs?max_age_days=7")
    assert resp.status_code == 200
    assert mock_q.call_args.kwargs["max_age_days"] == 7


# ---------------------------------------------------------------------------
# AC-001: pagination
# ---------------------------------------------------------------------------


def test_pagination(client):
    """limit and offset are forwarded and reflected in response."""
    rows = [_make_job_row(id=f"job-{i}") for i in range(3)]
    with _patch_query(rows, total=100):
        resp = client.get("/jobs?limit=3&offset=10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["limit"] == 3
    assert body["offset"] == 10
    assert body["total"] == 100
    assert len(body["jobs"]) == 3


def test_pagination_limit_max(client):
    """limit > 500 returns 422 (route declares ge=1, le=500)."""
    with _patch_query([], 0):
        resp_ok = client.get("/jobs?limit=500")
    assert resp_ok.status_code == 200
    resp_too_big = client.get("/jobs?limit=501")
    assert resp_too_big.status_code == 422


# ---------------------------------------------------------------------------
# AC-001: sort
# ---------------------------------------------------------------------------


def test_sort_recency_vs_keyword(client):
    """sort param is forwarded correctly for both values."""
    with _patch_query([]) as mock_q:
        resp = client.get("/jobs?sort=recency")
    assert resp.status_code == 200
    assert mock_q.call_args.kwargs["sort"] == "recency"

    with _patch_query([]) as mock_q:
        resp = client.get("/jobs?sort=score_keyword")
    assert resp.status_code == 200
    assert mock_q.call_args.kwargs["sort"] == "score_keyword"


def test_sort_invalid_returns_422(client):
    """Invalid sort value returns 422."""
    resp = client.get("/jobs?sort=invalid_sort")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# AC-015: max_distance_km
# ---------------------------------------------------------------------------


def test_max_distance_km_with_known_city(client):
    """max_distance_km with a known city geocodes and filters."""
    berlin_row = _make_job_row(location_lat=52.52, location_lon=13.40)
    far_row = _make_job_row(id="job-far", location_lat=48.14, location_lon=11.58)  # Munich

    with _patch_query([berlin_row, far_row], total=2):
        resp = client.get("/jobs?location=Berlin&max_distance_km=50")
    assert resp.status_code == 200
    body = resp.json()
    # Only Berlin row should survive
    assert len(body["jobs"]) == 1


def test_max_distance_km_without_location_returns_400(client):
    """max_distance_km without location returns 400."""
    resp = client.get("/jobs?max_distance_km=50")
    assert resp.status_code == 400


def test_max_distance_km_geocode_fallback_includes_all(client):
    """Pre-migration: rows without location_lat/lon are included, not dropped."""
    rows = [_make_job_row()]  # No location_lat/lon keys
    with _patch_query(rows, total=1):
        resp = client.get("/jobs?location=Berlin&max_distance_km=50")
    assert resp.status_code == 200
    # Row without coords is kept (pre-migration graceful fallback)
    assert len(resp.json()["jobs"]) == 1


# ---------------------------------------------------------------------------
# AC-016: source filter
# ---------------------------------------------------------------------------


def test_source_filter(client):
    """source param is forwarded to repo.query."""
    with _patch_query([]) as mock_q:
        resp = client.get("/jobs?source=greenhouse&source=linkedin")
    assert resp.status_code == 200
    call_kwargs = mock_q.call_args.kwargs
    assert "greenhouse" in call_kwargs["source"]
    assert "linkedin" in call_kwargs["source"]


# ---------------------------------------------------------------------------
# AC-017: company_domain filter (whitelist)
# ---------------------------------------------------------------------------


def test_company_domain_filter(client):
    """company_domain param is forwarded to repo.query as whitelist."""
    with _patch_query([]) as mock_q:
        resp = client.get("/jobs?company_domain=acme.de&company_domain=startup.io")
    assert resp.status_code == 200
    call_kwargs = mock_q.call_args.kwargs
    assert "acme.de" in call_kwargs["company_domain"]
    assert "startup.io" in call_kwargs["company_domain"]


# ---------------------------------------------------------------------------
# AC-018: seniority filter
# ---------------------------------------------------------------------------


def test_seniority_filter(client):
    """seniority param is forwarded to repo.query."""
    with _patch_query([]) as mock_q:
        resp = client.get("/jobs?seniority=senior")
    assert resp.status_code == 200
    assert mock_q.call_args.kwargs["seniority"] == "senior"


def test_seniority_invalid_returns_422(client):
    """Invalid seniority value returns 422."""
    resp = client.get("/jobs?seniority=guru")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Salary filter
# ---------------------------------------------------------------------------


def test_salary_filter(client):
    """min_salary and max_salary params forwarded to repo.query."""
    with _patch_query([]) as mock_q:
        resp = client.get("/jobs?min_salary=50000&max_salary=100000")
    assert resp.status_code == 200
    call_kwargs = mock_q.call_args.kwargs
    assert call_kwargs["min_salary"] == 50000
    assert call_kwargs["max_salary"] == 100000


# ---------------------------------------------------------------------------
# JobListItem shape
# ---------------------------------------------------------------------------


def test_job_list_item_fields(client):
    """JobListItem contains expected baseline fields after Stage-2/3 hard-delete."""
    rows = [_make_job_row()]
    with _patch_query(rows):
        resp = client.get("/jobs")
    assert resp.status_code == 200
    job = resp.json()["jobs"][0]
    assert "id" in job
    assert "title" in job
    assert "score_stage_1" in job
    assert "final_score" in job
    assert "archetype" in job
