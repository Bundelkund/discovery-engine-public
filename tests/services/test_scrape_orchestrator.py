"""ScrapeOrchestrator ingest cap (Bounded-Active-Market).

A single source may not store more than its ingest_cap per run (greenhouse 2026-07-02
dumped 25,609 rows → 500-MB free-tier overflow). jobs_found stays the TRUE fetched count;
only what reaches raw_jobs is capped. Per-source override sources.yaml <src>.ingest_cap,
else global default; 0 opts a source out.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.job import RawJob
from app.services.scrape_orchestrator import ScrapeOrchestrator


def _raw(i: int) -> RawJob:
    return RawJob(
        title=f"t{i}", url=f"u{i}", company="c", location="",
        description="", salary="", source="greenhouse", external_id=str(i),
    )


class _StubScraper:
    _batch: list = []

    async def fetch(self, config):
        return list(type(self)._batch)


def _orchestrator_capturing(captured: dict) -> ScrapeOrchestrator:
    orch = ScrapeOrchestrator(MagicMock())
    orch.raw_job_repo.insert_batch = AsyncMock(
        side_effect=lambda jobs: captured.update(n=len(jobs)) or len(jobs)
    )
    return orch


@pytest.mark.asyncio
async def test_per_source_cap_truncates_but_reports_true_found():
    _StubScraper._batch = [_raw(i) for i in range(2000)]
    captured: dict = {}
    orch = _orchestrator_capturing(captured)

    with patch(
        "app.services.scrape_orchestrator.load_sources_config",
        return_value={"sources": {"greenhouse": {"ingest_cap": 800}}},
    ), patch(
        "app.services.scrape_orchestrator.SourceRegistry.get", return_value=_StubScraper
    ):
        resp = await orch.run("greenhouse")

    assert resp.jobs_found == 2000   # true count preserved
    assert captured["n"] == 800      # only cap stored
    assert resp.jobs_stored == 800


@pytest.mark.asyncio
async def test_global_default_applies_without_source_override():
    _StubScraper._batch = [_raw(i) for i in range(1600)]
    captured: dict = {}
    orch = _orchestrator_capturing(captured)

    with patch(
        "app.services.scrape_orchestrator.load_sources_config",
        return_value={"sources": {"greenhouse": {}}},
    ), patch(
        "app.services.scrape_orchestrator.SourceRegistry.get", return_value=_StubScraper
    ), patch(
        "app.services.scrape_orchestrator.get_settings"
    ) as gs:
        gs.return_value.ingest_cap_default = 1500
        await orch.run("greenhouse")

    assert captured["n"] == 1500


@pytest.mark.asyncio
async def test_under_cap_stores_everything():
    _StubScraper._batch = [_raw(i) for i in range(50)]
    captured: dict = {}
    orch = _orchestrator_capturing(captured)

    with patch(
        "app.services.scrape_orchestrator.load_sources_config",
        return_value={"sources": {"greenhouse": {"ingest_cap": 800}}},
    ), patch(
        "app.services.scrape_orchestrator.SourceRegistry.get", return_value=_StubScraper
    ):
        resp = await orch.run("greenhouse")

    assert captured["n"] == 50
    assert resp.jobs_stored == 50


@pytest.mark.asyncio
async def test_cap_zero_opts_source_out():
    _StubScraper._batch = [_raw(i) for i in range(3000)]
    captured: dict = {}
    orch = _orchestrator_capturing(captured)

    with patch(
        "app.services.scrape_orchestrator.load_sources_config",
        return_value={"sources": {"greenhouse": {"ingest_cap": 0}}},
    ), patch(
        "app.services.scrape_orchestrator.SourceRegistry.get", return_value=_StubScraper
    ):
        await orch.run("greenhouse")

    assert captured["n"] == 3000   # cap=0 → no truncation
