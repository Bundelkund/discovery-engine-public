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

    # Title must pass the T6 storage-gate (Florian profile is loaded) so the
    # jobs reach the MinHash stage — "AI Coach" is a primary target role.
    job_a = NormalizedJob(
        title="AI Coach",
        url="https://linkedin.com/jobs/1",
        source="linkedin",
        external_id="li-1",
        description=desc,
    )
    job_b = NormalizedJob(
        title="AI Coach",
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


# ---------------------------------------------------------------------------
# DE-FOLLOWUP-04: scoring-profile.local.yaml is loaded when present
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_uses_loaded_profile_when_local_file_present():
    """When config/scoring-profile.local.yaml exists, its archetypes/keywords
    drive Stage-1 scoring instead of the empty default profile."""
    from app.scoring.types import ScoringProfile

    orch = _make_orchestrator()
    loaded_profile = ScoringProfile(
        id="florian",
        archetypes={"bridge-builder": 0.9},
        keywords_positive=["AI", "Coaching"],
    )

    captured: dict = {}

    async def _capture_run_stage1(jobs, profile):
        captured["profile"] = profile
        return []

    with (
        patch(
            "app.services.scrape_orchestrator.load_scoring_profile",
            return_value=loaded_profile,
        ),
        patch(
            "app.services.scrape_orchestrator.load_sources_config",
            return_value={"sources": {"greenhouse": {}}},
        ),
        patch("app.services.scrape_orchestrator.SourceRegistry.get") as mock_get,
        patch(
            "app.services.scrape_orchestrator.ScoringPipeline.run_stage1",
            new=AsyncMock(side_effect=_capture_run_stage1),
        ),
    ):
        from app.models.job import NormalizedJob

        job = NormalizedJob(
            title="AI Coaching Lead",
            url="https://example.com/jobs/1",
            source="greenhouse",
            external_id="gh-1",
            description="We are hiring an AI coach.",
        )
        mock_scraper = MagicMock()
        mock_scraper.fetch = AsyncMock(return_value=[object()])
        mock_scraper.normalize = MagicMock(return_value=job)
        mock_get.return_value = lambda: mock_scraper

        async def _pass_through(batch):
            return batch, 0

        from app.deduplication.dedup import DeduplicationService

        with patch.object(
            DeduplicationService, "filter_batch", new=AsyncMock(side_effect=_pass_through)
        ):
            await orch.run(source_id="greenhouse", store=False)

    used = captured.get("profile")
    assert used is not None, "ScoringPipeline.run_stage1 was not called"
    assert used.id == "florian"
    assert used.archetypes == {"bridge-builder": 0.9}
    assert "AI" in used.keywords_positive


@pytest.mark.asyncio
async def test_run_falls_back_to_empty_profile_when_no_local_file():
    """When the local profile file is absent, run() keeps the previous
    empty-default behavior — no errors, profile_id passed through."""
    orch = _make_orchestrator()

    with (
        patch(
            "app.services.scrape_orchestrator.load_scoring_profile",
            return_value=None,
        ),
        patch(
            "app.services.scrape_orchestrator.load_sources_config",
            return_value={"sources": {}},
        ),
        patch("app.services.scrape_orchestrator.SourceRegistry.get") as mock_get,
    ):
        mock_scraper = MagicMock()
        mock_scraper.fetch = AsyncMock(return_value=[])
        mock_get.return_value = lambda: mock_scraper

        result = await orch.run(source_id="greenhouse", profile_id="consumer-x")

    assert result.profile_id == "consumer-x"
    assert result.jobs_found == 0


# ---------------------------------------------------------------------------
# Slice B: step 4a description resolution runs and reports count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_resolves_descriptions_before_scoring():
    """run() invokes DescriptionResolver.resolve_batch on new jobs and records
    the count on the response."""
    from app.deduplication.dedup import DeduplicationService
    from app.models.job import NormalizedJob

    orch = _make_orchestrator()

    job = NormalizedJob(
        title="AI Coach",
        url="https://acme.softgarden.io/job/1",
        source="adzuna",
        external_id="ad-1",
        description="",  # thin -> resolution target
    )

    async def _pass_through(batch):
        return batch, 0

    async def _fake_resolve(jobs):
        for j in jobs:
            j.description = "backfilled full description from origin"
        return len(jobs)

    with (
        patch(
            "app.services.scrape_orchestrator.load_sources_config",
            return_value={"sources": {"adzuna": {}}},
        ),
        patch(
            "app.services.scrape_orchestrator.load_resolution_config",
            return_value={"resolution": {"enabled": True}},
        ),
        patch("app.services.scrape_orchestrator.SourceRegistry.get", return_value=lambda: _mk_scraper(job)),
        patch.object(DeduplicationService, "filter_batch", new=AsyncMock(side_effect=_pass_through)),
        patch(
            "app.services.scrape_orchestrator.DescriptionResolver.resolve_batch",
            new=AsyncMock(side_effect=_fake_resolve),
        ),
    ):
        result = await orch.run(source_id="adzuna", store=False)

    assert result.descriptions_resolved == 1


def _mk_scraper(job):
    s = MagicMock()
    s.fetch = AsyncMock(return_value=[object()])
    s.normalize = MagicMock(return_value=job)
    return s


def test_load_scoring_profile_returns_none_when_no_file_exists(tmp_path, monkeypatch):
    """load_scoring_profile() returns None when neither the .local.yaml
    override NOR the committed scoring-profile.yaml default exists.
    (DE-FOLLOWUP-11: resolution order is now .local -> .yaml -> None.)"""
    from app import config as config_module

    config_module.load_scoring_profile.cache_clear()
    monkeypatch.setattr(config_module, "REPO_ROOT", tmp_path)

    assert config_module.load_scoring_profile() is None
    config_module.load_scoring_profile.cache_clear()


def test_load_scoring_profile_parses_local_yaml_when_present(tmp_path, monkeypatch):
    """load_scoring_profile() returns the .local.yaml override when present
    even if a sibling scoring-profile.yaml default also exists."""
    from app import config as config_module

    config_module.load_scoring_profile.cache_clear()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "scoring-profile.local.yaml").write_text(
        "id: florian\n"
        "name: Florian\n"
        "archetypes:\n"
        "  coach: 0.7\n"
        "keywords_positive:\n"
        "  - Agile\n",
        encoding="utf-8",
    )
    # Sanity: a default also exists but .local must win
    (config_dir / "scoring-profile.yaml").write_text(
        "id: default\nname: Default\narchetypes: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "REPO_ROOT", tmp_path)

    profile = config_module.load_scoring_profile()
    assert profile is not None
    assert profile.id == "florian"
    assert profile.archetypes == {"coach": 0.7}
    assert profile.keywords_positive == ["Agile"]
    config_module.load_scoring_profile.cache_clear()


def test_load_scoring_profile_falls_back_to_committed_default(tmp_path, monkeypatch):
    """When only the committed scoring-profile.yaml exists (no .local override),
    that default profile is returned. (DE-FOLLOWUP-11 unblock: production
    Coolify deploys without a .local file now get a populated profile.)"""
    from app import config as config_module

    config_module.load_scoring_profile.cache_clear()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "scoring-profile.yaml").write_text(
        "id: default\n"
        "name: Default Profile\n"
        "archetypes:\n"
        "  trainer: 0.5\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "REPO_ROOT", tmp_path)

    profile = config_module.load_scoring_profile()
    assert profile is not None
    assert profile.id == "default"
    assert profile.archetypes == {"trainer": 0.5}
    config_module.load_scoring_profile.cache_clear()
