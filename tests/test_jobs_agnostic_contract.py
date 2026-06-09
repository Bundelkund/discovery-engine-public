"""Agnostik contract tests for GET /jobs.

Verifies:
  - GET /jobs rejects (422) or ignores an unknown ``profile_id`` query param
  - Response shape has no ``person`` field (agnostik invariant)
  - Response shape has no ``profile_id`` field at the top level or inside jobs
"""
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from app.main import app
from app.dependencies import get_consumer, get_supabase


# ---------------------------------------------------------------------------
# Fixtures — mirror conftest pattern from test_jobs_api.py
# ---------------------------------------------------------------------------


def _make_job_row(**kwargs) -> dict:
    defaults = {
        "id": "job-1",
        "title": "AI Consultant",
        "company": "Acme GmbH",
        "location": "Berlin",
        "remote": True,
        "description": "We build AI tooling.",
        "url": "https://acme.de/jobs/1",
        "source": "adzuna",
        "external_id": "ext-1",
        "content_hash": "abc123",
        "archetype": "consultant",
        "company_domain": "acme.de",
        "salary_min": 60000,
        "salary_max": 90000,
        "scraped_at": "2026-04-20T10:00:00+00:00",
        "keywords": ["ai", "consulting"],
        "metadata": {},
    }
    defaults.update(kwargs)
    return defaults


@pytest.fixture
def mock_supabase():
    return MagicMock()


@pytest.fixture
def client(mock_supabase):
    from app.dependencies import ConsumerIdentity

    app.dependency_overrides[get_supabase] = lambda: mock_supabase
    app.dependency_overrides[get_consumer] = lambda: ConsumerIdentity(
        id="test-consumer", name="Test", scopes=["jobs:read"]
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def _patch_query(rows: list[dict], total: int | None = None):
    if total is None:
        total = len(rows)
    return patch(
        "app.routes.jobs_api.JobRepository.query",
        return_value=(rows, total),
    )


# ---------------------------------------------------------------------------
# profile_id rejection / ignore
# ---------------------------------------------------------------------------


def test_get_jobs_ignores_unknown_profile_id_param(client):
    """GET /jobs with ?profile_id=xxx either returns 200 (ignores) or 422 (rejects).

    The response must never echo profile_id back in the body.
    Passing an unknown query param is a FastAPI 422 when strict param checking
    is enabled, or silently ignored otherwise.  Both outcomes satisfy the
    agnostik contract.
    """
    rows = [_make_job_row()]
    with _patch_query(rows, total=1):
        resp = client.get("/jobs?profile_id=some-person-id")

    # Either ignored (200) or rejected (422) — both satisfy agnostik contract
    assert resp.status_code in (200, 422), f"Unexpected status: {resp.status_code}"

    if resp.status_code == 200:
        body = resp.json()
        # Response MUST NOT contain profile_id at top level
        assert "profile_id" not in body, "Response body leaked profile_id at top level"
        # Jobs list items must not contain profile_id
        for job in body.get("jobs", []):
            assert "profile_id" not in job, f"Job item leaked profile_id: {job}"


def test_get_jobs_response_has_no_person_field(client):
    """GET /jobs response shape has no 'person' field anywhere."""
    rows = [_make_job_row()]
    with _patch_query(rows, total=1):
        resp = client.get("/jobs")

    assert resp.status_code == 200
    body = resp.json()
    assert "person" not in body, "Top-level 'person' field found in response"
    for job in body.get("jobs", []):
        assert "person" not in job, f"Job item contains 'person' field: {job}"


def test_get_jobs_response_has_no_profile_id_field(client):
    """GET /jobs response body contains no profile_id field (top-level or in jobs)."""
    rows = [_make_job_row()]
    with _patch_query(rows, total=1):
        resp = client.get("/jobs")

    assert resp.status_code == 200
    body = resp.json()
    assert "profile_id" not in body
    for job in body.get("jobs", []):
        assert "profile_id" not in job, f"Job item contains profile_id: {job}"


# ---------------------------------------------------------------------------
# Response shape sanity
# ---------------------------------------------------------------------------


def test_get_jobs_response_shape(client):
    """GET /jobs returns {jobs, total, limit, offset} — no person, no profile_id."""
    rows = [_make_job_row()]
    with _patch_query(rows, total=1):
        resp = client.get("/jobs")

    assert resp.status_code == 200
    body = resp.json()
    required_keys = {"jobs", "total", "limit", "offset"}
    assert required_keys.issubset(body.keys()), (
        f"Missing keys: {required_keys - set(body.keys())}"
    )
    forbidden_keys = {"person", "profile_id", "user_id", "applicant_id"}
    overlap = forbidden_keys & set(body.keys())
    assert not overlap, f"Forbidden keys in response: {overlap}"


def test_get_jobs_empty_result_no_profile_id(client):
    """Empty result set also contains no profile_id or person field."""
    with _patch_query([], total=0):
        resp = client.get("/jobs")

    assert resp.status_code == 200
    body = resp.json()
    assert "profile_id" not in body
    assert "person" not in body
    assert body["jobs"] == []
