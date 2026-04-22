"""
test_removed_endpoints.py
Phase 4 AC-003 / AC-004 verification: removed endpoints must return 404.

Asserts that the following paths — removed in Phase 1 — are not registered
in the FastAPI app and return 404 for all relevant HTTP methods.

Removed endpoints:
  POST /profiles
  POST /profiles/sync
  GET  /profiles/{id}
  POST /score/batch
  POST /discover/opportunities
  GET  /discover/opportunities
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.dependencies import get_supabase, get_consumer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    """TestClient with auth and supabase overridden to avoid live DB."""
    from unittest.mock import MagicMock

    app.dependency_overrides[get_supabase] = lambda: MagicMock()
    app.dependency_overrides[get_consumer] = lambda: MagicMock(id="test", name="Test", scopes=[])
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _assert_404(client: TestClient, method: str, path: str) -> None:
    """Assert that an HTTP request returns 404 (endpoint removed)."""
    func = getattr(client, method.lower())
    kwargs = {"json": {}} if method.upper() not in ("GET", "DELETE", "HEAD") else {}
    resp = func(path, **kwargs)
    assert resp.status_code == 404, (
        f"Expected 404 for {method} {path}, got {resp.status_code}. "
        f"Endpoint should have been deleted in Phase 1."
    )


# ---------------------------------------------------------------------------
# Tests: Profile endpoints (AC-003)
# ---------------------------------------------------------------------------

def test_post_profiles_returns_404(client):
    """POST /profiles was removed in Phase 1 — must return 404."""
    _assert_404(client, "POST", "/profiles")


def test_post_profiles_sync_returns_404(client):
    """POST /profiles/sync was removed in Phase 1 — must return 404."""
    _assert_404(client, "POST", "/profiles/sync")


def test_get_profiles_by_id_returns_404(client):
    """GET /profiles/{id} was removed in Phase 1 — must return 404."""
    _assert_404(client, "GET", "/profiles/some-profile-id")


def test_put_profiles_returns_404(client):
    """PUT /profiles/{id} (if it existed) — must return 404."""
    _assert_404(client, "PUT", "/profiles/some-profile-id")


def test_delete_profiles_returns_404(client):
    """DELETE /profiles/{id} — must return 404."""
    _assert_404(client, "DELETE", "/profiles/some-profile-id")


# ---------------------------------------------------------------------------
# Tests: Score batch endpoint (AC-003)
# ---------------------------------------------------------------------------

def test_post_score_batch_returns_404(client):
    """POST /score/batch was removed in Phase 1 — must return 404."""
    _assert_404(client, "POST", "/score/batch")


def test_get_score_batch_returns_404(client):
    """GET /score/batch — must return 404."""
    _assert_404(client, "GET", "/score/batch")


# ---------------------------------------------------------------------------
# Tests: Discover endpoints (AC-003)
# ---------------------------------------------------------------------------

def test_post_discover_opportunities_returns_404(client):
    """POST /discover/opportunities was removed in Phase 1 — must return 404."""
    _assert_404(client, "POST", "/discover/opportunities")


def test_get_discover_opportunities_returns_404(client):
    """GET /discover/opportunities was removed in Phase 1 — must return 404."""
    _assert_404(client, "GET", "/discover/opportunities")


# ---------------------------------------------------------------------------
# Tests: Confirm surviving endpoints still work (regression guard)
# ---------------------------------------------------------------------------

def test_health_still_returns_200(client):
    """GET /health must still return 200 — not accidentally deleted."""
    resp = client.get("/health")
    assert resp.status_code == 200, (
        f"GET /health returned {resp.status_code} — health endpoint broken"
    )


def test_jobs_api_still_exists(client):
    """GET /jobs must still exist (not accidentally removed with profiles)."""
    resp = client.get("/jobs")
    # 200 or 503 (if mock DB not full) — just not 404
    assert resp.status_code != 404, (
        f"GET /jobs returned 404 — jobs endpoint accidentally removed"
    )
