"""Refine pipeline: ordered steps, profile-agnostic shelf, upsert shape.

The engine is profile-agnostic — Step 6 has NO scoring and NO enrichment. A job
that survives parse/dedup/dq/quality-gate is refined and upserted to the shared
shelf regardless of any user's profile (per-profile scoring lives in the tenant).
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.models.job import ScoredJob
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


def _pipeline(rows: list[dict]) -> RefinePipeline:
    p = RefinePipeline(MagicMock())
    p.raw_repo.fetch_new = AsyncMock(return_value=rows)
    p.raw_repo.mark_status = AsyncMock()
    p.dedup.filter_batch = AsyncMock(side_effect=lambda jobs: (list(jobs), 0, set()))
    p.minhash.is_near_duplicate = MagicMock(return_value=False)
    p.minhash.add = MagicMock()
    p.minhash.purge_old = MagicMock(return_value=0)
    p.rules_engine = MagicMock(mode="flag-only")
    p.rules_engine.classify = MagicMock(return_value=("keep", {}))
    p.location_normalizer = MagicMock()
    p.location_normalizer.normalize = MagicMock(
        return_value={"location_normalized": "Berlin", "is_remote": False}
    )
    # upsert returns a per-row success flag list (default: all rows succeed).
    p.job_repo.upsert = AsyncMock(side_effect=lambda jobs: [True] * len(jobs))
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


def test_content_hash_collapses_cross_source_duplicate():
    """The same amberra posting from 5 boards (different url/company-legal-form/
    location) must hash to ONE canonical content_hash."""
    from app.services.refine_pipeline import _content_hash

    title = "Agile AI Transformation Coach - all genders welcome"
    pairs = [
        (title, "amberra GmbH"),   # adzuna / careerjet / jooble
        (title, "amberra GmbH"),
        (title, "amberra GmbH"),
        (title, "amberra"),        # linkedin / personio
        (title, "amberra"),
    ]
    hashes = {_content_hash(t, c) for t, c in pairs}
    assert len(hashes) == 1, f"cross-source dup must collapse, got {hashes}"


def test_content_hash_ignores_url_and_gender_suffix():
    from app.services.refine_pipeline import _content_hash

    # url is not an input at all; gender/bracket suffix variants collapse
    assert _content_hash("AI Coach (m/w/d)", "Foo AG") == _content_hash(
        "AI Coach", "Foo"
    )
    # genuinely different roles stay distinct
    assert _content_hash("AI Coach", "Foo") != _content_hash("Data Engineer", "Foo")


# --- dedup-company-noise-escape: adzuna company-echo + garbage-company label ---

# The four REAL jobs_v2 rows of ONE Capco posting (prod, 2026-07-06). Adzuna
# shipped it as four feed variants: title truncated at 64 raw chars or not,
# company echoed into the title or not, company field carrying the apply-page
# label "Bewerbung als" or the real employer.
_CAPCO_FULL = "(Senior) Consultant* / Transformation Manager* – Asset Management"
_CAPCO_CUT = "(Senior) Consultant* / Transformation Manager* – Asset Managemen"


def test_content_hash_strips_company_echo_from_title():
    """Some adzuna feed variants render 'Title - Capco', others 'Title' + company
    field. The echo must not split the hash of the SAME posting."""
    from app.services.refine_pipeline import _content_hash

    assert _content_hash(_CAPCO_FULL + " - Capco", "Capco") == _content_hash(
        _CAPCO_FULL, "Capco"
    )
    # legal-form variant of the company still strips its echo
    assert _content_hash("AI Coach - amberra", "amberra GmbH") == _content_hash(
        "AI Coach", "amberra"
    )


def test_content_hash_treats_apply_label_company_as_empty():
    """'Bewerbung als …' in the company field is scrape garbage, not an employer."""
    from app.services.refine_pipeline import _norm_company

    assert _norm_company("Bewerbung als") == ""
    assert _norm_company("Bewerbung als Senior Transformation Manager") == ""
    assert _norm_company("Capco") == "capco"


def test_content_hash_no_overmerge():
    """Gegen-Tests: genuinely different jobs must NOT collapse at hash level."""
    from app.services.refine_pipeline import _content_hash

    # same title, different companies → distinct (company stays in the hash)
    assert _content_hash("Werkstudent HR", "BMW") != _content_hash(
        "Werkstudent HR", "Siemens"
    )
    # REGRESSION GUARD (shelf, 2026-07): 50 per-city postings of one company
    # share their first ~60 chars — long same-company titles diverging only in
    # the tail must stay distinct. (This is why truncation tolerance is NOT a
    # hash cap but a comparison in DeduplicationService keyed on the exact
    # 64-char truncation signature.)
    assert _content_hash(
        "Sales Manager (m/w/d) für intelligente Energiesysteme - 1KOMMA5° Hamburg",
        "1komma5grad",
    ) != _content_hash(
        "Sales Manager (m/w/d) für intelligente Energiesysteme - 1KOMMA5° Bamberg",
        "1komma5grad",
    )
    # garbage company does NOT unify with a real company at hash level —
    # that bridge is comparison-based (dedup Tier 3b / backfill migration),
    # because a per-row hash unifying ''<->'capco' would require dropping the
    # company for ALL rows (cross-company over-merge).
    assert _content_hash(_CAPCO_CUT, "Bewerbung als") != _content_hash(
        _CAPCO_CUT, "Capco"
    )


# --- agnostic: a job that matched no old profile signal is now REFINED ---


@pytest.mark.asyncio
async def test_profile_foreign_job_is_refined():
    """The engine no longer gates on a person's profile. A title that the old
    florian profile would have rejected ('Software Engineer') is now refined and
    upserted — the shared shelf is user-agnostic."""
    p = _pipeline([_row("a", title="Software Engineer")])
    summary = await p.run()
    marked = {c.args[0]: c.args[1] for c in p.raw_repo.mark_status.call_args_list}
    assert marked["a"] == "refined"
    assert summary["refined"] == 1


# --- quality_gate: an empty/garbage title is rejected (profile-free flood cap) ---


@pytest.mark.asyncio
async def test_garbage_title_rejected_by_quality_gate():
    p = _pipeline([_row("a", title="  "), _row("b", title="Data Engineer")])
    summary = await p.run()
    marked = {c.args[0]: c.args[1] for c in p.raw_repo.mark_status.call_args_list}
    assert marked["a"] == "rejected"   # blank title -> quality gate drop
    assert marked["b"] == "refined"
    assert summary["rejected"] == 1
    assert summary["refined"] == 1


# --- ordered: an exact dup never reaches the shelf ---


@pytest.mark.asyncio
async def test_exact_dup_not_upserted():
    p = _pipeline([_row("a"), _row("b")])
    p.dedup.filter_batch = AsyncMock(return_value=([], 1, {0}))  # 'a' is a dup

    captured = {}

    async def capture_upsert(jobs):
        captured["jobs"] = jobs
        return [True] * len(jobs)

    p.job_repo.upsert = capture_upsert
    await p.run()
    # Only the non-dup survivor reached the shelf.
    assert len(captured["jobs"]) == 1


# --- final ScoredJob carries location + dq_flags into the upsert (no score) ---


@pytest.mark.asyncio
async def test_upsert_receives_location_and_flags():
    p = _pipeline([_row("a")])
    p.rules_engine.classify = MagicMock(return_value=("keep", {"junior_title": True}))

    captured = {}

    async def capture_upsert(jobs):
        captured["jobs"] = jobs
        return [True] * len(jobs)

    p.job_repo.upsert = capture_upsert
    await p.run()
    job = captured["jobs"][0]
    assert isinstance(job, ScoredJob)
    assert job.location_normalized == "Berlin"
    assert job.dq_flags == {"junior_title": True}


# --- #4: a per-row upsert failure must NOT mark that raw_job 'refined' ---


@pytest.mark.asyncio
async def test_upsert_partial_failure_leaves_failed_row_new():
    """When upsert reports row 'b' failed, only 'a' is marked refined; 'b' stays
    'new' (no mark_status) so the next pass retries it — never silently refined."""
    p = _pipeline([_row("a", title="Coach A"), _row("b", title="Coach B")])
    p.job_repo.upsert = AsyncMock(side_effect=lambda jobs: [True, False])

    summary = await p.run()
    marked = {c.args[0]: c.args[1] for c in p.raw_repo.mark_status.call_args_list}
    assert marked.get("a") == "refined"
    assert "b" not in marked  # failed row left 'new' for retry
    assert summary["refined"] == 1
    assert summary["errors"] >= 1


# --- F7: near-dedup bands persisted ONLY after a successful upsert ---


@pytest.mark.asyncio
async def test_minhash_add_only_for_refined_rows():
    """F7 regression: minhash.add() must run ONLY for rows that reached the shelf.
    A row whose upsert later failed must NOT leave its band hashes behind."""
    p = _pipeline([_row("a", title="Coach A"), _row("b", title="Coach B")])
    p.job_repo.upsert = AsyncMock(side_effect=lambda jobs: [True, False])

    summary = await p.run()

    assert p.minhash.add.call_count == 1  # only the refined row
    assert summary["refined"] == 1


# --- AUDIT-P1-02: retention is owned by pg_cron, NOT the refine hot loop ---


@pytest.mark.asyncio
async def test_run_has_no_inline_purges():
    """run() must NOT purge inline (no purge_raw_jobs RPC, no dedup purge_old).

    Retention (dedup_memory, raw_jobs, jobs_v2) moved to nightly pg_cron jobs
    per AUDIT-P1-02 — inline purges were wasted DELETE scans on every pass."""
    p = _pipeline([_row("a")])
    await p.run()
    assert not any(
        c.args and c.args[0] == "purge_raw_jobs" for c in p.supabase.rpc.call_args_list
    ), "hot loop must not call purge_raw_jobs"
    p.minhash.purge_old.assert_not_called()


# --- #5: a classify failure on one row rejects it and the pass continues ---


@pytest.mark.asyncio
async def test_classify_failure_isolated_per_row():
    """A rules-engine exception on one row marks it 'rejected' and does not abort
    the batch — the sibling row still refines."""
    p = _pipeline([_row("a", title="Coach A"), _row("b", title="Coach B")])

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
