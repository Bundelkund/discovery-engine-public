"""Refine pipeline: ordered steps, score/gate preservation, upsert shape, profile-id absence."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.models.job import ScoredJob
from app.scoring.types import ScoringProfile
from app.services.refine_pipeline import RefinePipeline, parse_raw


def _row(rid: str, **over) -> dict:
    base = {
        "id": rid,
        "title": "Agile Coach",
        "url": f"https://boards.greenhouse.io/acme/jobs/{rid}",
        "company": "ACME",
        "location": "Berlin",
        "description": "x" * 400,
        "salary": "",
        "source": "greenhouse",
        "external_id": f"gh-{rid}",
        "posted_at": None,
        "content_hash": "",
        "status": "new",
    }
    base.update(over)
    return base


def _fake_scoring_pipeline(score_map=None, threshold_keep=None):
    """Build a fake ScoringPipeline class for monkeypatching the module symbol.

    score_map: title -> score (default 50). threshold_keep: predicate(ScoredJob)
    deciding which scored jobs survive filter_by_threshold (default: keep all).
    """
    score_map = score_map or {}

    class _Fake:
        captured_titles: list = []

        def __init__(self, cfg):
            pass

        async def run_stage1(self, jobs, profile):
            type(self).captured_titles = [j.title for j in jobs]
            return [
                ScoredJob(**j.model_dump(), score_stage_1=score_map.get(j.title, 50),
                          archetype="coach")
                for j in jobs
            ]

        def filter_by_threshold(self, jobs):
            pred = threshold_keep or (lambda j: True)
            kept = [j for j in jobs if pred(j)]
            return kept, len(jobs) - len(kept)

    return _Fake


def _pipeline(rows: list[dict]) -> RefinePipeline:
    p = RefinePipeline(MagicMock())
    p.raw_repo.fetch_new = AsyncMock(return_value=rows)
    p.raw_repo.mark_status = AsyncMock()
    p.dedup.filter_batch = AsyncMock(side_effect=lambda jobs: (list(jobs), 0, set()))
    p.minhash.is_near_duplicate = MagicMock(return_value=False)
    p.minhash.add = MagicMock()
    p.rules_engine = MagicMock(mode="flag-only")
    p.rules_engine.classify = MagicMock(return_value=("keep", {}))
    p.location_normalizer = MagicMock()
    p.location_normalizer.normalize = MagicMock(
        return_value={"location_normalized": "Berlin", "is_remote": False}
    )
    p.profile = ScoringProfile(id="")
    # upsert now returns a per-row success flag list (default: all rows succeed).
    p.job_repo.upsert = AsyncMock(side_effect=lambda jobs: [True] * len(jobs))
    p._enrich = AsyncMock()
    return p


# --- parse handles both RawJob and dict ---


def test_parse_raw_from_rawjob_object():
    from app.models.job import RawJob

    nj = parse_raw(RawJob(title="T", url="u", company="C", source="lever"))
    assert nj.source == "lever"
    assert nj.content_hash
    assert nj.external_id  # guaranteed non-empty


def test_parse_raw_backfills_default_source():
    nj = parse_raw(_row("1", source=""), default_source="indeed")
    assert nj.source == "indeed"


# --- ordered: dedup runs before scoring (a dup never reaches the scorer) ---


@pytest.mark.asyncio
async def test_exact_dup_skips_scoring(monkeypatch):
    p = _pipeline([_row("a"), _row("b")])
    p.dedup.filter_batch = AsyncMock(return_value=([], 1, {0}))  # 'a' is a dup

    fake = _fake_scoring_pipeline()
    monkeypatch.setattr("app.services.refine_pipeline.ScoringPipeline", fake)

    await p.run()
    # Only the non-dup survivor 'b' was scored.
    assert len(fake.captured_titles) == 1


# --- title gate drops no-signal titles when a profile is configured ---


@pytest.mark.asyncio
async def test_title_gate_drops_non_matching_when_profile_set(monkeypatch):
    p = _pipeline([_row("a", title="Plumber"), _row("b", title="Agile Coach")])
    p.profile = ScoringProfile(id="x", keywords_positive=["coach"])

    monkeypatch.setattr(
        "app.services.refine_pipeline.ScoringPipeline", _fake_scoring_pipeline()
    )

    summary = await p.run()
    marked = {c.args[0]: c.args[1] for c in p.raw_repo.mark_status.call_args_list}
    assert marked["a"] == "rejected"  # Plumber gated out
    assert marked["b"] == "refined"
    assert summary["rejected"] == 1


# --- below-threshold scored jobs are rejected, not upserted ---


@pytest.mark.asyncio
async def test_below_threshold_rejected(monkeypatch):
    p = _pipeline([_row("a", title="Coach A"), _row("b", title="Coach B")])
    # 'Coach A' scores 0 (below threshold), 'Coach B' scores 100 (kept)
    fake = _fake_scoring_pipeline(
        score_map={"Coach A": 0, "Coach B": 100},
        threshold_keep=lambda j: j.score_stage_1 >= 50,
    )
    monkeypatch.setattr("app.services.refine_pipeline.ScoringPipeline", fake)

    await p.run()
    marked = {c.args[0]: c.args[1] for c in p.raw_repo.mark_status.call_args_list}
    assert marked["a"] == "rejected"
    assert marked["b"] == "refined"


# --- final ScoredJob carries location + dq_flags into the upsert ---


@pytest.mark.asyncio
async def test_upsert_receives_location_and_flags(monkeypatch):
    p = _pipeline([_row("a")])
    p.rules_engine.classify = MagicMock(return_value=("keep", {"junior_title": True}))

    monkeypatch.setattr(
        "app.services.refine_pipeline.ScoringPipeline",
        _fake_scoring_pipeline(score_map={"Agile Coach": 80}),
    )

    captured = {}

    async def capture_upsert(jobs):
        captured["jobs"] = jobs
        return [True] * len(jobs)

    p.job_repo.upsert = capture_upsert

    await p.run()
    job = captured["jobs"][0]
    assert job.location_normalized == "Berlin"
    assert job.dq_flags == {"junior_title": True}
    assert job.score_stage_1 == 80


# --- #4: a per-row upsert failure must NOT mark that raw_job 'refined' ---


@pytest.mark.asyncio
async def test_upsert_partial_failure_leaves_failed_row_new(monkeypatch):
    """When upsert reports row 'b' failed, only 'a' is marked refined; 'b' stays
    'new' (no mark_status) so the next pass retries it — never silently refined."""
    p = _pipeline([_row("a", title="Coach A"), _row("b", title="Coach B")])
    monkeypatch.setattr(
        "app.services.refine_pipeline.ScoringPipeline", _fake_scoring_pipeline()
    )
    # First job reaches the shelf, second fails inside the repo.
    p.job_repo.upsert = AsyncMock(side_effect=lambda jobs: [True, False])

    summary = await p.run()
    marked = {c.args[0]: c.args[1] for c in p.raw_repo.mark_status.call_args_list}
    assert marked.get("a") == "refined"
    assert "b" not in marked  # failed row left 'new' for retry
    assert summary["refined"] == 1
    assert summary["errors"] >= 1


# --- #5: a classify failure on one row rejects it and the pass continues ---


@pytest.mark.asyncio
async def test_classify_failure_isolated_per_row(monkeypatch):
    """A rules-engine exception on one row marks it 'rejected' and does not abort
    the batch — the sibling row still refines."""
    p = _pipeline([_row("a", title="Coach A"), _row("b", title="Coach B")])
    monkeypatch.setattr(
        "app.services.refine_pipeline.ScoringPipeline", _fake_scoring_pipeline()
    )

    def _classify(job_dict):
        if job_dict.get("title") == "Coach A":
            raise RuntimeError("rules engine boom")
        return ("keep", {})

    p.rules_engine.classify = MagicMock(side_effect=_classify)

    summary = await p.run()
    marked = {c.args[0]: c.args[1] for c in p.raw_repo.mark_status.call_args_list}
    assert marked["a"] == "rejected"
    assert marked["b"] == "refined"
    assert summary["errors"] >= 1


# --- enrichment is invoked for survivors (preserved from old orchestrator) ---


@pytest.mark.asyncio
async def test_enrichment_invoked(monkeypatch):
    p = _pipeline([_row("a")])
    monkeypatch.setattr(
        "app.services.refine_pipeline.ScoringPipeline", _fake_scoring_pipeline()
    )

    await p.run()
    p._enrich.assert_awaited_once()


# --- agnostik: EnrichmentContext built without profile_id ---


@pytest.mark.asyncio
async def test_enrich_context_has_no_profile_id(monkeypatch):
    """The real _enrich must construct EnrichmentContext with no profile_id field
    (agnostik) — the model no longer carries one."""
    from app.models.company import EnrichmentContext

    assert not hasattr(EnrichmentContext(), "profile_id")

    p = RefinePipeline(MagicMock())
    p.company_repo.needs_enrichment = AsyncMock(return_value=True)
    p.company_repo.upsert = AsyncMock()

    captured = {}

    class FakePipeline:
        def __init__(self, cfg):
            pass

        async def run(self, companies, ctx):
            captured["ctx"] = ctx
            return companies

    monkeypatch.setattr("app.services.refine_pipeline.EnrichmentPipeline", FakePipeline)

    job = ScoredJob(
        title="T", url="https://acme.io/x", company="ACME",
        source="lever", external_id="1", score_stage_1=50,
    )
    await p._enrich([job])

    # ctx was built and carries no profile_id attribute
    assert "ctx" in captured
    assert not hasattr(captured["ctx"], "profile_id")


# --- _extract_domain behaviour ---


def test_extract_domain_greenhouse():
    p = RefinePipeline.__new__(RefinePipeline)
    job = ScoredJob(
        title="T", url="https://boards.greenhouse.io/acme/jobs/9",
        company="ACME", source="greenhouse", external_id="9",
    )
    assert p._extract_domain(job) == "acme.com"


def test_extract_domain_skips_aggregators():
    p = RefinePipeline.__new__(RefinePipeline)
    job = ScoredJob(
        title="T", url="https://indeed.com/job/9", company="X",
        source="indeed", external_id="9",
    )
    assert p._extract_domain(job) == ""
