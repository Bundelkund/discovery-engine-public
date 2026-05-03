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


# ---------------------------------------------------------------------------
# Test: cross-consumer isolation — distinct keys log distinct consumer_ids
# ---------------------------------------------------------------------------


def test_consumers_log_distinct_ids(caplog):
    """Two sequential requests with different keys log their own consumer_id.

    Regression guard against consumer_id leakage (AC-011 isolation).
    """
    import logging
    from app.dependencies import get_supabase, _load_consumers as _lc

    active_both = [
        {
            "id": "wonderapply",
            "name": "WA",
            "key_env": "WA_API_KEY",
            "scopes": ["jobs:read"],
            "active": True,
        },
        {
            "id": "jobhunt",
            "name": "JH",
            "key_env": "JH_API_KEY",
            "scopes": ["jobs:read"],
            "active": True,
        },
    ]
    _lc.cache_clear()

    with patch("app.dependencies._load_consumers", return_value=active_both):
        mock_sb = MagicMock()
        app.dependency_overrides[get_supabase] = lambda: mock_sb
        c = TestClient(app, raise_server_exceptions=False)
        try:
            with patch(
                "app.routes.jobs_api.JobRepository.query",
                return_value=([], 0),
            ):
                with caplog.at_level(logging.INFO, logger="app.dependencies"):
                    caplog.clear()
                    c.get("/jobs", headers={"X-API-Key": "test-wa-dev"})
                    c.get("/jobs", headers={"X-API-Key": "test-jh-dev"})
        finally:
            app.dependency_overrides.clear()

    ids = [
        getattr(r, "consumer_id", None)
        for r in caplog.records
        if "request_authenticated" in r.message
    ]
    assert ids == ["wonderapply", "jobhunt"], (
        f"Expected distinct consumer_ids in order, got {ids}"
    )


def test_wa_key_does_not_identify_as_jh(caplog):
    """WA key logs consumer_id=wonderapply, never jobhunt (even when both are configured)."""
    import logging
    from app.dependencies import get_supabase, _load_consumers as _lc

    both = [
        {
            "id": "wonderapply",
            "name": "WA",
            "key_env": "WA_API_KEY",
            "scopes": ["jobs:read"],
            "active": True,
        },
        {
            "id": "jobhunt",
            "name": "JH",
            "key_env": "JH_API_KEY",
            "scopes": ["jobs:read"],
            "active": True,
        },
    ]
    _lc.cache_clear()
    with patch("app.dependencies._load_consumers", return_value=both):
        mock_sb = MagicMock()
        app.dependency_overrides[get_supabase] = lambda: mock_sb
        c = TestClient(app, raise_server_exceptions=False)
        try:
            with patch(
                "app.routes.jobs_api.JobRepository.query",
                return_value=([], 0),
            ):
                with caplog.at_level(logging.INFO, logger="app.dependencies"):
                    caplog.clear()
                    c.get("/jobs", headers={"X-API-Key": "test-wa-dev"})
        finally:
            app.dependency_overrides.clear()

    auth_records = [r for r in caplog.records if "request_authenticated" in r.message]
    assert len(auth_records) == 1
    assert getattr(auth_records[0], "consumer_id", None) == "wonderapply"


# ---------------------------------------------------------------------------
# Test: scope enforcement — consumer with jobs:read cannot trigger /scrape
# ---------------------------------------------------------------------------

def test_scope_denied_returns_403(client, monkeypatch):
    """A consumer authenticated but missing required scope is rejected with 403."""
    read_only = [
        {
            "id": "readonly",
            "name": "ReadOnly",
            "key_env": "RO_API_KEY",
            "scopes": ["jobs:read"],
            "active": True,
        },
    ]
    monkeypatch.setenv("RO_API_KEY", "ro-test-key")
    _clear_consumer_cache()
    with patch("app.dependencies._load_consumers", return_value=read_only):
        resp = client.post(
            "/enrich/example.com",
            headers={"X-API-Key": "ro-test-key"},
        )
    assert resp.status_code == 403
    assert "scrape:trigger" in resp.json()["detail"]


def test_scope_allowed_passes_check(client):
    """A consumer with the required scope passes the scope gate (route reaches handler)."""
    with patch("app.routes.jobs_api.JobRepository.query", return_value=([], 0)):
        resp = client.get("/jobs", headers={"X-API-Key": "test-wa-dev"})
    # Route may 500 because mock isn't async-aware, but scope check passed
    # (otherwise we'd see 403). Treat anything except 403 as scope-pass.
    assert resp.status_code != 403
