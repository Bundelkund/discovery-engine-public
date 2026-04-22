"""
Tests for ScrapeOrchestrator — focuses on the optional profile_id entry-point
(Worker-A refactored: profile_id: str | None = None).
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.responses import ScrapeResponse
from app.services.scrape_orchestrator import ScrapeOrchestrator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_orchestrator() -> ScrapeOrchestrator:
    """Return a ScrapeOrchestrator with a mock supabase client."""
    mock_client = MagicMock()
    return ScrapeOrchestrator(mock_client)


# ---------------------------------------------------------------------------
# profile_id optional — entry-point signature test
# ---------------------------------------------------------------------------


def test_run_signature_accepts_none_profile_id():
    """ScrapeOrchestrator.run() signature allows profile_id: None."""
    import inspect

    sig = inspect.signature(ScrapeOrchestrator.run)
    param = sig.parameters.get("profile_id")
    assert param is not None, "profile_id parameter must exist"
    assert param.default is None, "profile_id must default to None"


@pytest.mark.asyncio
async def test_run_with_none_profile_id_does_not_raise():
    """run() with profile_id=None returns a ScrapeResponse (no AttributeError)."""
    orch = _make_orchestrator()

    # Patch the heavy dependencies — we only care that the entry-point works
    with (
        patch("app.services.scrape_orchestrator.load_sources_config", return_value={"sources": {}}),
        patch("app.services.scrape_orchestrator.SourceRegistry.get") as mock_get,
    ):
        mock_scraper = MagicMock()
        mock_scraper.fetch = AsyncMock(return_value=[])
        mock_get.return_value = lambda: mock_scraper

        result = await orch.run(source_id="greenhouse", profile_id=None)

    assert isinstance(result, ScrapeResponse)
    assert result.profile_id == ""  # empty string when None passed


@pytest.mark.asyncio
async def test_run_with_explicit_profile_id_preserved():
    """run() with an explicit profile_id preserves it in the response."""
    orch = _make_orchestrator()

    with (
        patch("app.services.scrape_orchestrator.load_sources_config", return_value={"sources": {}}),
        patch("app.services.scrape_orchestrator.SourceRegistry.get") as mock_get,
    ):
        mock_scraper = MagicMock()
        mock_scraper.fetch = AsyncMock(return_value=[])
        mock_get.return_value = lambda: mock_scraper

        result = await orch.run(source_id="greenhouse", profile_id="profile-abc")

    assert result.profile_id == "profile-abc"


@pytest.mark.asyncio
async def test_run_returns_zero_jobs_on_empty_source():
    """run() with empty source returns ScrapeResponse with 0 jobs_found."""
    orch = _make_orchestrator()

    with (
        patch("app.services.scrape_orchestrator.load_sources_config", return_value={"sources": {}}),
        patch("app.services.scrape_orchestrator.SourceRegistry.get") as mock_get,
    ):
        mock_scraper = MagicMock()
        mock_scraper.fetch = AsyncMock(return_value=[])
        mock_get.return_value = lambda: mock_scraper

        result = await orch.run(source_id="greenhouse")

    assert result.jobs_found == 0
    assert result.jobs_new == 0
    assert result.jobs_stored == 0
