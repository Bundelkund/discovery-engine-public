"""State-machine integrity for the refine pipeline.

Every raw_job fetched (status='new') must end in EXACTLY ONE terminal state
(refined | rejected | duplicate) — none silently dropped — and a re-run must
only touch status='new' rows (idempotency).
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.models.job import NormalizedJob, ScoredJob
from app.services.refine_pipeline import (
    DUPLICATE,
    REFINED,
    REJECTED,
    RefinePipeline,
    parse_raw,
)


def _row(rid: str, **over) -> dict:
    base = {
        "id": rid,
        "title": "Agile Coach",
        "url": f"https://example.com/{rid}",
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


def _make_pipeline() -> RefinePipeline:
    """Build a pipeline with all collaborators replaced by permissive fakes.

    Defaults make every job sail through to 'refined'; individual tests override
    one collaborator to force a duplicate/rejected branch.
    """
    p = RefinePipeline(MagicMock())

    # raw repo
    p.raw_repo.fetch_new = AsyncMock(return_value=[])
    p.raw_repo.mark_status = AsyncMock()

    # exact dedup: nothing is a dup by default
    p.dedup.filter_batch = AsyncMock(
        side_effect=lambda jobs: (list(jobs), 0, set())
    )

    # near dedup: never near-dup; add() is a no-op (sync, wrapped in to_thread)
    p.minhash.is_near_duplicate = MagicMock(return_value=False)
    p.minhash.add = MagicMock()

    # dq rules: flag-only, keep everything, no flags
    p.rules_engine = MagicMock()
    p.rules_engine.mode = "flag-only"
    p.rules_engine.classify = MagicMock(return_value=("keep", {}))

    # location: identity (no normalization fields)
    p.location_normalizer = MagicMock()
    p.location_normalizer.normalize = MagicMock(return_value={})

    # empty profile -> title_gate keeps all, scores 0
    from app.scoring.types import ScoringProfile

    p.profile = ScoringProfile(id="")

    # upsert + enrich succeed / no-op
    p.job_repo.upsert = AsyncMock(return_value=0)
    p._enrich = AsyncMock()

    return p


# ---------------------------------------------------------------------------
# parse / external_id guarantee
# ---------------------------------------------------------------------------


def test_parse_raw_guarantees_external_id_from_url():
    row = _row("1", external_id="", url="https://x.io/job/9")
    nj = parse_raw(row)
    assert nj.external_id == "https://x.io/job/9"
    assert nj.content_hash  # computed


def test_parse_raw_falls_back_to_content_hash_when_no_url():
    row = _row("1", external_id="", url="")
    nj = parse_raw(row)
    assert nj.external_id == nj.content_hash
    assert nj.external_id


def test_parse_raw_preserves_existing_external_id():
    nj = parse_raw(_row("1", external_id="gh-42"))
    assert nj.external_id == "gh-42"


# ---------------------------------------------------------------------------
# every raw_job ends in exactly one terminal state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_rows_reach_refined_on_happy_path():
    p = _make_pipeline()
    p.raw_repo.fetch_new = AsyncMock(return_value=[_row("a"), _row("b"), _row("c")])

    summary = await p.run()

    assert summary["fetched"] == 3
    assert summary["refined"] == 3
    assert summary["rejected"] == 0
    assert summary["duplicate"] == 0
    marked = {c.args[0]: c.args[1] for c in p.raw_repo.mark_status.call_args_list}
    assert marked == {"a": REFINED, "b": REFINED, "c": REFINED}


@pytest.mark.asyncio
async def test_exact_duplicate_marked_duplicate():
    p = _make_pipeline()
    p.raw_repo.fetch_new = AsyncMock(return_value=[_row("a"), _row("b")])
    # index 1 (job 'b') is an exact dup of an existing record
    p.dedup.filter_batch = AsyncMock(return_value=([], 1, {1}))

    summary = await p.run()

    marked = {c.args[0]: c.args[1] for c in p.raw_repo.mark_status.call_args_list}
    assert marked["b"] == DUPLICATE
    assert marked["a"] == REFINED
    assert summary["duplicate"] == 1
    assert summary["refined"] == 1


@pytest.mark.asyncio
async def test_near_duplicate_marked_duplicate():
    p = _make_pipeline()
    p.raw_repo.fetch_new = AsyncMock(return_value=[_row("a"), _row("b")])
    # 'a' is a near-dup, 'b' is not
    p.minhash.is_near_duplicate = MagicMock(side_effect=[True, False])

    summary = await p.run()

    marked = {c.args[0]: c.args[1] for c in p.raw_repo.mark_status.call_args_list}
    assert marked["a"] == DUPLICATE
    assert marked["b"] == REFINED
    assert summary["duplicate"] == 1


@pytest.mark.asyncio
async def test_dq_reject_marks_rejected_when_reject_active():
    p = _make_pipeline()
    p.raw_repo.fetch_new = AsyncMock(return_value=[_row("a"), _row("b")])
    p.rules_engine.mode = "flag+reject"
    p.rules_engine.classify = MagicMock(
        side_effect=[("reject", {"spam": True}), ("keep", {})]
    )

    summary = await p.run()

    marked = {c.args[0]: c.args[1] for c in p.raw_repo.mark_status.call_args_list}
    assert marked["a"] == REJECTED
    assert marked["b"] == REFINED
    assert summary["rejected"] == 1


@pytest.mark.asyncio
async def test_dq_reject_kept_when_reject_inactive():
    """flag-only mode: a reject verdict cannot occur, but even a flagged job stays."""
    p = _make_pipeline()
    p.raw_repo.fetch_new = AsyncMock(return_value=[_row("a")])
    p.rules_engine.mode = "flag-only"
    p.rules_engine.classify = MagicMock(return_value=("keep", {"flagged": True}))

    summary = await p.run()
    assert summary["refined"] == 1
    assert summary["rejected"] == 0


@pytest.mark.asyncio
async def test_parse_failure_isolates_one_row(monkeypatch):
    """A row that fails to parse is rejected; the rest of the batch still processes."""
    p = _make_pipeline()
    p.raw_repo.fetch_new = AsyncMock(return_value=[_row("bad"), _row("ok")])

    import app.services.refine_pipeline as mod

    real_parse = mod.parse_raw

    def flaky_parse(row, default_source=""):
        if row.get("id") == "bad":
            raise ValueError("boom")
        return real_parse(row, default_source=default_source)

    monkeypatch.setattr(mod, "parse_raw", flaky_parse)

    summary = await p.run()

    marked = {c.args[0]: c.args[1] for c in p.raw_repo.mark_status.call_args_list}
    assert marked["ok"] == REFINED
    # 'bad' was rejected; its mark_status was attempted
    assert marked.get("bad") == REJECTED
    assert summary["rejected"] == 1


@pytest.mark.asyncio
async def test_no_row_silently_dropped():
    """fetched == refined + rejected + duplicate (+ errored unaddressable rows)."""
    p = _make_pipeline()
    rows = [_row("a"), _row("b"), _row("c"), _row("d")]
    p.raw_repo.fetch_new = AsyncMock(return_value=rows)
    p.dedup.filter_batch = AsyncMock(return_value=([], 1, {0}))  # 'a' exact dup
    p.minhash.is_near_duplicate = MagicMock(side_effect=[True, False, False])  # 'b' near dup
    p.rules_engine.mode = "flag+reject"
    p.rules_engine.classify = MagicMock(
        side_effect=[("reject", {}), ("keep", {})]  # 'c' rejected, 'd' kept
    )

    summary = await p.run()

    terminal = summary["refined"] + summary["rejected"] + summary["duplicate"]
    assert terminal == summary["fetched"] == 4
    marked = {c.args[0]: c.args[1] for c in p.raw_repo.mark_status.call_args_list}
    assert marked == {"a": DUPLICATE, "b": DUPLICATE, "c": REJECTED, "d": REFINED}


# ---------------------------------------------------------------------------
# idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotent_only_reads_status_new():
    """The pipeline's only input is fetch_new (status='new'); a re-run with no new
    rows is a no-op that marks nothing."""
    p = _make_pipeline()
    p.raw_repo.fetch_new = AsyncMock(return_value=[])

    summary = await p.run()

    assert summary == {
        "fetched": 0,
        "refined": 0,
        "rejected": 0,
        "duplicate": 0,
        "errors": 0,
    }
    p.raw_repo.mark_status.assert_not_called()


@pytest.mark.asyncio
async def test_upsert_failure_leaves_row_new_for_retry():
    """If the clean-shelf upsert fails, survivors are NOT marked terminal — they
    stay 'new' so the next pass retries them (no silent loss)."""
    p = _make_pipeline()
    p.raw_repo.fetch_new = AsyncMock(return_value=[_row("a")])
    p.job_repo.upsert = AsyncMock(side_effect=RuntimeError("supabase down"))

    summary = await p.run()

    assert summary["refined"] == 0
    # 'a' was never marked terminal
    assert p.raw_repo.mark_status.call_count == 0
