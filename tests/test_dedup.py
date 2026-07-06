import asyncio
from unittest.mock import MagicMock

from app.deduplication.dedup import DeduplicationService
from app.models.job import NormalizedJob


def _make_job(
    url="http://test.com/1",
    title="Test Job",
    company="TestCo",
    external_id="",
    content_hash=None,
):
    # content_hash defaults to one DERIVED FROM url so distinct jobs get distinct
    # hashes — otherwise the intra-batch collapse (Tier 4) would treat unrelated
    # fixtures as duplicates. Pass content_hash explicitly to simulate a real
    # cross-source duplicate (same canonical hash, different url).
    return NormalizedJob(
        title=title,
        url=url,
        source="test",
        company=company,
        description="desc",
        content_hash=content_hash or f"hash-{url}",
        external_id=external_id,
    )


# ---------------------------------------------------------------------------
# filter_batch return shape — GAP 2.3b
# ---------------------------------------------------------------------------


def test_filter_batch_returns_three_tuple():
    """filter_batch must return (kept_jobs, dup_count, duplicate_indices)."""
    mock_client = MagicMock()
    mock_client.table.return_value.select.return_value.in_.return_value.execute.return_value.data = []
    service = DeduplicationService(mock_client)
    result = asyncio.run(service.filter_batch([_make_job()]))
    assert len(result) == 3, "filter_batch must return a 3-tuple"
    kept, dup_count, dup_indices = result
    assert isinstance(kept, list)
    assert isinstance(dup_count, int)
    assert isinstance(dup_indices, set)


def test_dedup_filters_known_urls_exposes_indices():
    """Known URLs result in non-empty duplicate_indices."""
    mock_client = MagicMock()

    def mock_select(col):
        select_result = MagicMock()

        def mock_in(in_col, values):
            in_result = MagicMock()
            if in_col == "url":
                in_result.execute.return_value.data = [{"url": "http://test.com/1"}]
            else:
                in_result.execute.return_value.data = []
            return in_result

        select_result.in_ = mock_in
        return select_result

    mock_client.table.return_value.select = mock_select

    service = DeduplicationService(mock_client)
    jobs = [_make_job("http://test.com/1"), _make_job("http://test.com/2")]
    kept, dup_count, dup_indices = asyncio.run(service.filter_batch(jobs))

    assert dup_count >= 1
    assert len(dup_indices) >= 1
    # Index 0 was the known URL — must be in dup_indices
    assert 0 in dup_indices
    # kept list must NOT contain the duplicate
    for job in kept:
        assert job.url != "http://test.com/1"


def test_dedup_passes_new_jobs():
    mock_client = MagicMock()
    mock_client.table.return_value.select.return_value.in_.return_value.execute.return_value.data = []

    service = DeduplicationService(mock_client)
    jobs = [_make_job("http://new.com/1"), _make_job("http://new.com/2")]
    kept, dup_count, dup_indices = asyncio.run(service.filter_batch(jobs))

    assert len(kept) == 2
    assert dup_count == 0
    assert dup_indices == set()


def test_dedup_collapses_intra_batch_same_content_hash():
    """Tier 4: N NEW jobs (none on the shelf) sharing a content_hash but with
    different urls — the same posting from N boards — collapse to ONE kept job."""
    mock_client = MagicMock()
    # nothing exists on the shelf yet (all DB tiers return empty)
    mock_client.table.return_value.select.return_value.in_.return_value.execute.return_value.data = []

    service = DeduplicationService(mock_client)
    jobs = [
        _make_job("http://adzuna.de/1", external_id="adzuna-1", content_hash="canon"),
        _make_job("http://linkedin.de/2", external_id="linkedin-2", content_hash="canon"),
        _make_job("http://personio.de/3", external_id="personio-3", content_hash="canon"),
    ]
    kept, dup_count, dup_indices = asyncio.run(service.filter_batch(jobs))

    assert len(kept) == 1, "cross-source same-hash jobs must collapse to one"
    assert dup_count == 2
    assert dup_indices == {1, 2}  # first index kept, rest dropped


def test_dedup_empty_list():
    mock_client = MagicMock()
    service = DeduplicationService(mock_client)
    kept, dup_count, dup_indices = asyncio.run(service.filter_batch([]))

    assert kept == []
    assert dup_count == 0
    assert dup_indices == set()


def test_duplicate_indices_are_positions_in_original_list():
    """duplicate_indices must be the original positions, not offsets in kept list."""
    mock_client = MagicMock()

    def mock_select(col):
        sr = MagicMock()

        def mock_in(in_col, values):
            ir = MagicMock()
            # external_id "eid-middle" is a known duplicate
            if in_col == "external_id" and "eid-middle" in values:
                ir.execute.return_value.data = [{"external_id": "eid-middle"}]
            else:
                ir.execute.return_value.data = []
            return ir

        sr.in_ = mock_in
        return sr

    mock_client.table.return_value.select = mock_select

    jobs = [
        _make_job("http://a.com/1", external_id="eid-a"),
        _make_job("http://a.com/2", external_id="eid-middle"),
        _make_job("http://a.com/3", external_id="eid-c"),
    ]
    service = DeduplicationService(mock_client)
    kept, dup_count, dup_indices = asyncio.run(service.filter_batch(jobs))

    assert dup_count == 1
    assert dup_indices == {1}  # position 1 in original list
    assert len(kept) == 2
    assert all(j.external_id != "eid-middle" for j in kept)


# ---------------------------------------------------------------------------
# _batch_check targets configured table — GAP 4.5b
# ---------------------------------------------------------------------------


def test_batch_check_uses_active_shelf_by_default():
    """With no pinned table, _batch_check resolves the active shelf from settings
    (the read-switch) rather than a hardcoded default. (F4/F5)"""
    from app.config import get_settings

    mock_client = MagicMock()
    mock_client.table.return_value.select.return_value.in_.return_value.execute.return_value.data = []

    service = DeduplicationService(mock_client)
    asyncio.run(service.filter_batch([_make_job("http://x.com/1")]))

    called_tables = [c.args[0] for c in mock_client.table.call_args_list]
    assert get_settings().jobs_table in called_tables


def test_batch_check_uses_configured_table():
    """When jobs_table='jobs_v2' is passed at construction, _batch_check queries jobs_v2."""
    mock_client = MagicMock()
    mock_client.table.return_value.select.return_value.in_.return_value.execute.return_value.data = []

    service = DeduplicationService(mock_client, jobs_table="jobs_v2")
    asyncio.run(service.filter_batch([_make_job("http://x.com/2")]))

    called_tables = [c.args[0] for c in mock_client.table.call_args_list]
    assert "jobs_v2" in called_tables
    assert "jobs" not in called_tables


# ---------------------------------------------------------------------------
# Tier 3b: company-less title-prefix probe — dedup-company-noise-escape
# ---------------------------------------------------------------------------

_CAPCO_CUT = "(Senior) Consultant* / Transformation Manager* – Asset Managemen"
_CAPCO_FULL = "(Senior) Consultant* / Transformation Manager* – Asset Management"


def _client_with_probe(shelf_has_title_prefix: bool):
    """Mock client: all in_() tiers return empty; the ilike() title probe returns
    a hit iff shelf_has_title_prefix."""
    mock_client = MagicMock()

    def mock_select(col):
        sr = MagicMock()
        in_result = MagicMock()
        in_result.execute.return_value.data = []
        sr.in_ = MagicMock(return_value=in_result)
        ilike_result = MagicMock()
        ilike_result.limit.return_value.execute.return_value.data = (
            [{"id": "shelf-row"}] if shelf_has_title_prefix else []
        )
        sr.ilike = MagicMock(return_value=ilike_result)
        return sr

    mock_client.table.return_value.select = mock_select
    return mock_client


def test_tier3b_flags_companyless_variant_of_shelf_row():
    """The REAL escape (Capco, prod 2026-07): the clean-company row is on the
    shelf; weeks later adzuna re-emits the posting with company='Bewerbung als'
    (→ normalises to empty), a NEW ad id and url, and a 64-char-truncated title.
    eid/url/hash all miss and the MinHash memory has forgotten — Tier 3b must
    catch both variants via the 64-char raw title prefix."""
    client = _client_with_probe(shelf_has_title_prefix=True)
    service = DeduplicationService(client, jobs_table="jobs_v2")
    incoming = [
        _make_job("http://adzuna.de/5708714150", title=_CAPCO_CUT,
                  company="Bewerbung als", external_id="5708714150"),
        _make_job("http://adzuna.de/5708719524", title=_CAPCO_FULL,
                  company="Bewerbung als", external_id="5708719524"),
    ]
    kept, dup_count, dup_indices = asyncio.run(service.filter_batch(incoming))
    assert dup_indices == {0, 1}, "both garbage-company variants must be flagged"
    assert kept == []


def test_tier3b_flags_truncated_real_company_variant_of_shelf_row():
    """Truncation suspect (len == 64 exactly) with a REAL company: hash misses
    the full-title shelf sibling, the prefix probe must catch it."""
    assert len(_CAPCO_CUT) == 64  # the adzuna cut signature
    client = _client_with_probe(shelf_has_title_prefix=True)
    service = DeduplicationService(client, jobs_table="jobs_v2")
    incoming = [
        _make_job("http://adzuna.de/5707238425", title=_CAPCO_CUT,
                  company="Capco", external_id="5707238425"),
    ]
    kept, dup_count, dup_indices = asyncio.run(service.filter_batch(incoming))
    assert dup_indices == {0}
    assert kept == []


def test_tier3b_keeps_companyless_job_with_unknown_title():
    """No shelf row shares the 64-char prefix → the company-less job survives."""
    client = _client_with_probe(shelf_has_title_prefix=False)
    service = DeduplicationService(client, jobs_table="jobs_v2")
    incoming = [
        _make_job("http://adzuna.de/1", title=_CAPCO_CUT,
                  company="Bewerbung als", external_id="adz-1"),
    ]
    kept, dup_count, dup_indices = asyncio.run(service.filter_batch(incoming))
    assert dup_indices == set()
    assert len(kept) == 1


def test_tier3b_skips_short_titles_and_untruncated_real_companies():
    """Guards: short titles never probe (prefix too unspecific); long but
    provably untruncated titles (len > 64) with a usable company are owned by
    the hash tiers — otherwise per-city postings sharing a prefix would merge."""
    client = _client_with_probe(shelf_has_title_prefix=True)
    service = DeduplicationService(client, jobs_table="jobs_v2")
    incoming = [
        # company-less but SHORT title → no probe, kept
        _make_job("http://a.com/1", title="Werkstudent HR", company="Bewerbung als",
                  external_id="e1"),
        # len 66 > 64 (untruncated) + real company → no probe, kept
        _make_job("http://a.com/2", title=_CAPCO_FULL, company="Capco",
                  external_id="e2"),
    ]
    assert len(_CAPCO_FULL) > 64
    kept, dup_count, dup_indices = asyncio.run(service.filter_batch(incoming))
    assert dup_indices == set()
    assert len(kept) == 2


def test_intra_batch_truncation_collapse():
    """The truncated and the full variant of ONE posting arrive in the SAME
    batch (observed 2026-06-19 and 2026-07-03): nothing on the shelf yet, hashes
    differ → the 64-char-prefix group must collapse into the longest title."""
    client = _client_with_probe(shelf_has_title_prefix=False)
    service = DeduplicationService(client, jobs_table="jobs_v2")
    incoming = [
        _make_job("http://adzuna.de/1", title=_CAPCO_CUT,
                  company="Bewerbung als", external_id="a1"),
        _make_job("http://adzuna.de/2", title=_CAPCO_FULL,
                  company="Bewerbung als", external_id="a2"),
    ]
    kept, dup_count, dup_indices = asyncio.run(service.filter_batch(incoming))
    assert dup_indices == {0}, "truncated variant collapses into the full title"
    assert len(kept) == 1
    assert kept[0].title == _CAPCO_FULL


def test_intra_batch_does_not_collapse_per_city_postings():
    """REGRESSION GUARD (shelf, 2026-07): 50 per-city postings of one company
    share their first 64+ title chars but are DIFFERENT vacancies. Untruncated
    (len > 64) real-company rows must never be dropped by the prefix group."""
    base = "Sales Manager (m/w/d) für intelligente Energiesysteme - 1KOMMA5° "
    client = _client_with_probe(shelf_has_title_prefix=False)
    service = DeduplicationService(client, jobs_table="jobs_v2")
    incoming = [
        _make_job("http://p.de/1", title=base + "Hamburg",
                  company="1komma5grad", external_id="p1"),
        _make_job("http://p.de/2", title=base + "Bamberg",
                  company="1komma5grad", external_id="p2"),
        _make_job("http://p.de/3", title=base + "München",
                  company="1komma5grad", external_id="p3"),
    ]
    assert all(len(j.title) > 64 for j in incoming)
    assert len({j.title[:64] for j in incoming}) == 1  # identical 64-char prefix
    kept, dup_count, dup_indices = asyncio.run(service.filter_batch(incoming))
    assert dup_indices == set(), "per-city postings must all survive"
    assert len(kept) == 3


def test_batch_check_never_queries_old_table_when_overridden():
    """Verify hardcoded 'jobs' cannot leak when jobs_table='jobs_v2' is set."""
    mock_client = MagicMock()
    mock_client.table.return_value.select.return_value.in_.return_value.execute.return_value.data = []

    service = DeduplicationService(mock_client, jobs_table="jobs_v2")
    jobs = [
        _make_job("http://x.com/1", external_id="e1"),
        _make_job("http://x.com/2", external_id="e2"),
    ]
    asyncio.run(service.filter_batch(jobs))

    called_tables = {c.args[0] for c in mock_client.table.call_args_list}
    assert "jobs" not in called_tables
    assert "jobs_v2" in called_tables
