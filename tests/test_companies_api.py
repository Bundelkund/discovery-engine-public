"""GET /companies/{domain} — enrich-on-read behaviour.

The refine pipeline no longer enriches at scrape time, so the first consumer to
need a company (e.g. an apply flow) triggers enrichment lazily on read.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.routes import companies_api


@pytest.mark.asyncio
async def test_get_company_enriches_on_miss():
    """Miss → enrich_domain runs, then the row is re-fetched and returned."""
    repo = MagicMock()
    repo.get_with_watchlist = AsyncMock(
        side_effect=[None, {"domain": "acme.com", "name": "ACME"}]
    )
    with patch.object(companies_api, "CompanyRepository", return_value=repo), patch.object(
        companies_api, "enrich_domain", new=AsyncMock(return_value={"domain": "acme.com"})
    ) as enrich:
        resp = await companies_api.get_company("acme.com", supabase=MagicMock())

    enrich.assert_awaited_once()
    assert resp.domain == "acme.com"
    assert resp.name == "ACME"


@pytest.mark.asyncio
async def test_get_company_hit_skips_enrich():
    """A cached company is returned without triggering enrichment."""
    repo = MagicMock()
    repo.get_with_watchlist = AsyncMock(
        return_value={"domain": "acme.com", "name": "ACME"}
    )
    with patch.object(companies_api, "CompanyRepository", return_value=repo), patch.object(
        companies_api, "enrich_domain", new=AsyncMock()
    ) as enrich:
        resp = await companies_api.get_company("acme.com", supabase=MagicMock())

    enrich.assert_not_awaited()
    assert resp.name == "ACME"


@pytest.mark.asyncio
async def test_get_company_404_when_enrich_yields_nothing():
    """Still empty after enrich → 404 (never a 500)."""
    repo = MagicMock()
    repo.get_with_watchlist = AsyncMock(return_value=None)
    with patch.object(companies_api, "CompanyRepository", return_value=repo), patch.object(
        companies_api, "enrich_domain", new=AsyncMock(return_value=None)
    ):
        with pytest.raises(HTTPException) as ei:
            await companies_api.get_company("nope.com", supabase=MagicMock())
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_get_company_enrich_failure_falls_through_to_404():
    """An enrich exception must not 500 — it degrades to 404."""
    repo = MagicMock()
    repo.get_with_watchlist = AsyncMock(return_value=None)
    with patch.object(companies_api, "CompanyRepository", return_value=repo), patch.object(
        companies_api, "enrich_domain", new=AsyncMock(side_effect=RuntimeError("boom"))
    ):
        with pytest.raises(HTTPException) as ei:
            await companies_api.get_company("boom.com", supabase=MagicMock())
    assert ei.value.status_code == 404
