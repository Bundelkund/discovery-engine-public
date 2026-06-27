from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.sources.greenhouse  # noqa: F401

from app.sources.greenhouse import GreenhouseScraper
from app.registry.source_registry import SourceRegistry


def test_greenhouse_registered():
    assert "greenhouse" in SourceRegistry.registered_ids()


def test_greenhouse_has_fetch():
    scraper = GreenhouseScraper()
    assert hasattr(scraper, "fetch")
    assert scraper.source_id == "greenhouse"


def test_greenhouse_extract_location():
    scraper = GreenhouseScraper()
    assert scraper._extract_location({"location": {"name": "Berlin"}}) == "Berlin"
    assert scraper._extract_location({"location": "Remote"}) == "Remote"
    assert scraper._extract_location({}) == ""


def _resp(text: str, jobs: list[dict]):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.text = text
    r.json = MagicMock(return_value={"jobs": jobs})
    return r


@pytest.mark.asyncio
async def test_greenhouse_checksum_skip_skips_unchanged_board(tmp_path):
    """2 boards: unchanged -> skip (no parse/record); changed -> parse + record."""
    resp_unchanged = _resp("body-unchanged", [{"id": 1, "title": "Old"}])
    resp_changed = _resp(
        "body-changed",
        [{"id": 99, "title": "New Eng", "absolute_url": "http://x/99", "content": "c"}],
    )

    http_client = MagicMock()
    http_client.get = AsyncMock(side_effect=[resp_unchanged, resp_changed])
    acm = MagicMock()
    acm.__aenter__ = AsyncMock(return_value=http_client)
    acm.__aexit__ = AsyncMock(return_value=False)

    cache = MagicMock()
    cache.seen_unchanged = AsyncMock(side_effect=[True, False])  # board1 skip, board2 parse
    cache.record = AsyncMock()

    with (
        patch("app.sources.greenhouse.merge_slugs", return_value=["b-unchanged", "b-changed"]),
        patch("app.sources.greenhouse.httpx.AsyncClient", return_value=acm),
        patch("app.sources.greenhouse.FetchCache", return_value=cache),
    ):
        portals = tmp_path / "portals.yaml"
        portals.write_text("tracked_companies: []\n")
        jobs = await GreenhouseScraper().fetch({"portals_file": str(portals)})

    # only the changed board's job is parsed; unchanged board produced nothing
    assert [j.external_id for j in jobs] == ["99"]
    assert cache.seen_unchanged.await_count == 2
    # record called only for the changed board, with its raw body
    cache.record.assert_awaited_once_with("greenhouse", "b-changed", "body-changed")
    # unchanged board never reached JSON parse
    resp_unchanged.json.assert_not_called()
