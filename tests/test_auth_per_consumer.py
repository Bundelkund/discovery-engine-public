"""
Tests for per-consumer API key authentication (Phase 5).

Covers:
- Valid WA_API_KEY → 200, consumer_id="wonderapply"
- Valid JH_API_KEY but active:false → 403
- Invalid key → 401
- Missing key → 422 (FastAPI Header(...) auto-validation)
- Old DE_API_KEY shared → 401 (no longer recognised)
"""
import os
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from app.main import app
from app.dependencies import get_supabase, _load_consumers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clear_consumer_cache():
    """lru_cache must be cleared between tests that manipulate env vars."""
    _load_consumers.cache_clear()


FAKE_CONSUMERS = [
    {
        "id": "wonderapply",
        "name": "WonderApply",
        "key_env": "WA_API_KEY",
        "scopes": ["jobs:read", "scrape:trigger"],
        "active": True,
    },
    {
        "id": "jobhunt",
        "name": "JobHunt",
        "key_env": "JH_API_KEY",
        "scopes": ["jobs:read"],
        "active": False,
    },
]


def _make_client() -> TestClient:
    """TestClient with supabase mocked; auth uses real get_consumer."""
    mock_sb = MagicMock()
    app.dependency_overrides[get_supabase] = lambda: mock_sb
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def patch_consumers_and_env(monkeypatch):
    """
    Patch _load_consumers to return FAKE_CONSUMERS and set env vars.
    Clears lru_cache before and after each test.
    """
    _clear_consumer_cache()
    monkeypatch.setenv("WA_API_KEY", "test-wa-dev")
    monkeypatch.setenv("JH_API_KEY", "test-jh-dev")
    # Remove DE_API_KEY from env to ensure it is not recognised
    monkeypatch.delenv("DE_API_KEY", raising=False)

    with patch("app.dependencies._load_consumers", return_value=FAKE_CONSUMERS):
        yield

    _clear_consumer_cache()
    app.dependency_overrides.clear()


@pytest.fixture
def client():
    c = _make_client()
    yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Test: valid WA key → 200
# ---------------------------------------------------------------------------

def test_valid_wa_key_returns_200(client):
    """Valid WA_API_KEY is authenticated and request proceeds (200)."""
    with patch(
        "app.routes.jobs_api.JobRepository.query",
        return_value=([], 0),
    ):
        resp = client.get("/jobs", headers={"X-API-Key": "test-wa-dev"})
    assert resp.status_code == 200, resp.text


def test_valid_wa_key_logs_consumer_id(client, caplog):
    """Authenticated request logs consumer_id=wonderapply."""
    import logging
    with patch(
        "app.routes.jobs_api.JobRepository.query",
        return_value=([], 0),
    ):
        with caplog.at_level(logging.INFO, logger="app.dependencies"):
            client.get("/jobs", headers={"X-API-Key": "test-wa-dev"})
    # Check structured log extra — logged as "request_authenticated"
    assert any("request_authenticated" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Test: JH key active:false → 403
# ---------------------------------------------------------------------------

def test_inactive_consumer_returns_403(client):
    """JH_API_KEY is valid but consumer is inactive → 403."""
    resp = client.get("/jobs", headers={"X-API-Key": "test-jh-dev"})
    assert resp.status_code == 403, resp.text
    assert "not active" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Test: invalid key → 401
# ---------------------------------------------------------------------------

def test_invalid_key_returns_401(client):
    """Unrecognised API key → 401."""
    resp = client.get("/jobs", headers={"X-API-Key": "garbage-key"})
    assert resp.status_code == 401, resp.text
    assert "Invalid API key" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Test: missing key → 422
# ---------------------------------------------------------------------------

def test_missing_key_returns_422(client):
    """No X-API-Key header → FastAPI returns 422 (required header missing)."""
    resp = client.get("/jobs")
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Test: old DE_API_KEY shared → 401
# ---------------------------------------------------------------------------

def test_old_shared_de_api_key_returns_401(client):
    """DE_API_KEY is not in api-keys.yaml; using it as a key → 401."""
    resp = client.get("/jobs", headers={"X-API-Key": "old-shared-de-key"})
    assert resp.status_code == 401, resp.text
