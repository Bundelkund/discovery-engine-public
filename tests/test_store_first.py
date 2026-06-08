"""
Store-first invariants: the fetch path must insert raw_jobs with status='new'
and must NOT call normalize/dedup/score/enrich.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.job import RawJob
from app.services.scrape_orchestrator import ScrapeOrchestrator


def _make_orchestrator() -> ScrapeOrchestrator:
    return ScrapeOrchestrator(MagicMock())


# ---------------------------------------------------------------------------
# Raw jobs land with status='new'
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_path_produces_status_new():
    """Jobs inserted via the fetch path must have status='new'."""
    orch = _make_orchestrator()

    raw_job = RawJob(
        title="Coach Role",
        url="https://example.com/job/1",
        source="greenhouse",
        external_id="gh-1",
        raw_data={"id": "gh-1", "title": "Coach Role"},
    )

    captured: list[list[RawJob]] = []

    async def _capture(jobs: list[RawJob]) -> int:
        captured.append(jobs)
        return len(jobs)

    with (
        patch("app.services.scrape_orchestrator.load_sources_config", return_value={"sources": {"greenhouse": {}}}),
        patch("app.services.scrape_orchestrator.SourceRegistry.get") as mock_get,
        patch.object(orch.raw_job_repo, "insert_batch", new=_capture),
    ):
        mock_scraper = MagicMock()
        mock_scraper.fetch = AsyncMock(return_value=[raw_job])
        mock_get.return_value = lambda: mock_scraper

        await orch.run(source_id="greenhouse", store=True)

    assert len(captured) == 1
    job = captured[0][0]
    assert job.status == "new"


# ---------------------------------------------------------------------------
# Normalize is NOT called in the fetch path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_path_does_not_call_normalize():
    """scraper.normalize() must NOT be invoked in the store-first fetch path."""
    orch = _make_orchestrator()

    raw_job = RawJob(
        title="Test",
        url="https://example.com/job/2",
        source="adzuna",
        external_id="az-1",
        raw_data={"id": "az-1"},
    )

    with (
        patch("app.services.scrape_orchestrator.load_sources_config", return_value={"sources": {"adzuna": {}}}),
        patch("app.services.scrape_orchestrator.SourceRegistry.get") as mock_get,
        patch.object(orch.raw_job_repo, "insert_batch", new=AsyncMock(return_value=1)),
    ):
        mock_scraper = MagicMock()
        mock_scraper.fetch = AsyncMock(return_value=[raw_job])
        mock_get.return_value = lambda: mock_scraper

        await orch.run(source_id="adzuna", store=True)

    mock_scraper.normalize.assert_not_called()


# ---------------------------------------------------------------------------
# No dedup / score / enrich imports in the orchestrator module
# ---------------------------------------------------------------------------


def test_orchestrator_has_no_dedup_import():
    """DeduplicationService must NOT be imported by the fetch-path orchestrator."""
    import app.services.scrape_orchestrator as mod

    assert not hasattr(mod, "DeduplicationService"), (
        "DeduplicationService is imported in scrape_orchestrator — "
        "it belongs in refine_pipeline (A3), not the fetch path"
    )


def test_orchestrator_has_no_scoring_pipeline_import():
    """ScoringPipeline must NOT be imported by the fetch-path orchestrator."""
    import app.services.scrape_orchestrator as mod

    assert not hasattr(mod, "ScoringPipeline"), (
        "ScoringPipeline is imported in scrape_orchestrator — "
        "it belongs in refine_pipeline (A3), not the fetch path"
    )


def test_orchestrator_has_no_enrichment_pipeline_import():
    """EnrichmentPipeline must NOT be imported by the fetch-path orchestrator."""
    import app.services.scrape_orchestrator as mod

    assert not hasattr(mod, "EnrichmentPipeline"), (
        "EnrichmentPipeline is imported in scrape_orchestrator — "
        "it belongs in refine_pipeline (A3), not the fetch path"
    )
