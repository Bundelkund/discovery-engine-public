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


# ---------------------------------------------------------------------------
# AC-005: MinHash end-to-end integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_minhash_catches_near_duplicates_across_sources():
    """AC-005: 2 jobs with identical description from different sources → only 1 stored."""
    from app.data_quality.context import reset_dq_context
    from app.deduplication.dedup import DeduplicationService
    from app.models.job import NormalizedJob

    # Fresh singleton so LSH starts empty
    reset_dq_context()

    orch = _make_orchestrator()

    desc = (
        "We are looking for a senior Python developer with FastAPI and "
        "Postgres experience. Remote-friendly German SaaS team. "
    ) * 4  # ensure > shingle_size

    job_a = NormalizedJob(
        title="Senior Python",
        url="https://linkedin.com/jobs/1",
        source="linkedin",
        external_id="li-1",
        description=desc,
    )
    job_b = NormalizedJob(
        title="Senior Python",
        url="https://indeed.com/jobs/2",
        source="indeed",
        external_id="in-2",
        description=desc,
    )

    mock_scraper = MagicMock()
    mock_scraper.fetch = AsyncMock(return_value=[object(), object()])
    mock_scraper.normalize = MagicMock(side_effect=[job_a, job_b])

    # Hash dedup lets both through (different URL/source)
    async def _pass_through(batch):
        return batch, 0

    with (
        patch(
            "app.services.scrape_orchestrator.load_sources_config",
            return_value={"sources": {"linkedin": {}}},
        ),
        patch("app.services.scrape_orchestrator.SourceRegistry.get", return_value=lambda: mock_scraper),
        patch.object(DeduplicationService, "filter_batch", new=AsyncMock(side_effect=_pass_through)),
    ):
        result = await orch.run(source_id="linkedin", store=False)

    # Both fetched, dedup passed both, MinHash caught the second.
    assert result.jobs_found == 2
    assert result.jobs_duplicate >= 1, (
        f"MinHash should have caught at least one near-duplicate, got "
        f"jobs_duplicate={result.jobs_duplicate}"
    )

    # Cleanup to not pollute other tests
    reset_dq_context()
