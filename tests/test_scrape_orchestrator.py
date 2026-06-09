"""
Tests for ScrapeOrchestrator — store-first design.

Fetch path now: fetch → build RawJob list → insert into raw_jobs (status='new').
All normalize/dedup/score/enrich steps live in the refine pipeline (A3).
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
# Entry-point signature
# ---------------------------------------------------------------------------


def test_run_signature_has_no_profile_id():
    """ScrapeOrchestrator.run() must NOT have a profile_id parameter (agnostik invariant)."""
    import inspect

    sig = inspect.signature(ScrapeOrchestrator.run)
    assert "profile_id" not in sig.parameters, (
        "profile_id must NOT appear in ScrapeOrchestrator.run() — agnostik invariant"
    )


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

    assert isinstance(result, ScrapeResponse)
    assert result.jobs_found == 0
    assert result.jobs_stored == 0


@pytest.mark.asyncio
async def test_run_with_store_false_does_not_call_repo():
    """run(store=False) skips the raw_job_repo.insert_batch call."""
    from app.models.job import RawJob

    orch = _make_orchestrator()

    job = RawJob(
        title="Test Job",
        url="https://example.com/job/1",
        source="greenhouse",
        external_id="gh-1",
        raw_data={"id": "gh-1", "title": "Test Job"},
    )

    with (
        patch("app.services.scrape_orchestrator.load_sources_config", return_value={"sources": {"greenhouse": {}}}),
        patch("app.services.scrape_orchestrator.SourceRegistry.get") as mock_get,
        patch.object(orch.raw_job_repo, "insert_batch", new=AsyncMock(return_value=0)) as mock_insert,
    ):
        mock_scraper = MagicMock()
        mock_scraper.fetch = AsyncMock(return_value=[job])
        mock_get.return_value = lambda: mock_scraper

        result = await orch.run(source_id="greenhouse", store=False)

    assert result.jobs_found == 1
    assert result.jobs_stored == 0
    mock_insert.assert_not_called()


@pytest.mark.asyncio
async def test_run_stores_raw_jobs_with_store_true():
    """run(store=True) calls raw_job_repo.insert_batch and reflects count in response."""
    from app.models.job import RawJob

    orch = _make_orchestrator()

    jobs = [
        RawJob(
            title="Job A",
            url="https://example.com/job/a",
            source="adzuna",
            external_id="az-1",
            raw_data={"id": "az-1", "title": "Job A"},
        ),
        RawJob(
            title="Job B",
            url="https://example.com/job/b",
            source="adzuna",
            external_id="az-2",
            raw_data={"id": "az-2", "title": "Job B"},
        ),
    ]

    with (
        patch("app.services.scrape_orchestrator.load_sources_config", return_value={"sources": {"adzuna": {}}}),
        patch("app.services.scrape_orchestrator.SourceRegistry.get") as mock_get,
        patch.object(orch.raw_job_repo, "insert_batch", new=AsyncMock(return_value=2)) as mock_insert,
    ):
        mock_scraper = MagicMock()
        mock_scraper.fetch = AsyncMock(return_value=jobs)
        mock_get.return_value = lambda: mock_scraper

        result = await orch.run(source_id="adzuna", store=True)

    assert result.jobs_found == 2
    assert result.jobs_stored == 2
    mock_insert.assert_called_once()
    inserted_list = mock_insert.call_args[0][0]
    assert len(inserted_list) == 2


@pytest.mark.asyncio
async def test_run_wraps_dict_results_in_raw_job():
    """Scrapers returning dicts are wrapped into RawJob with raw_data=full dict."""
    from app.models.job import RawJob

    orch = _make_orchestrator()

    raw_dict = {
        "title": "Python Dev",
        "url": "https://example.com/job/py-1",
        "source": "indeed",
        "external_id": "in-99",
        "company": "ACME",
        "location": "Berlin",
        "description": "We need a Python dev.",
    }

    captured: list[list[RawJob]] = []

    async def _capture(raw_jobs: list[RawJob]) -> int:
        captured.append(raw_jobs)
        return len(raw_jobs)

    with (
        patch("app.services.scrape_orchestrator.load_sources_config", return_value={"sources": {"indeed": {}}}),
        patch("app.services.scrape_orchestrator.SourceRegistry.get") as mock_get,
        patch.object(orch.raw_job_repo, "insert_batch", new=_capture),
    ):
        mock_scraper = MagicMock()
        mock_scraper.fetch = AsyncMock(return_value=[raw_dict])
        mock_get.return_value = lambda: mock_scraper

        await orch.run(source_id="indeed", store=True)

    assert len(captured) == 1
    rj = captured[0][0]
    assert isinstance(rj, RawJob)
    # raw_data must hold the full source payload — not '{}'
    assert rj.raw_data == raw_dict
    assert rj.title == "Python Dev"
