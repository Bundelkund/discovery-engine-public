from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

import app.sources.softgarden  # noqa: F401
from app.registry.source_registry import SourceRegistry
from app.sources.softgarden import SoftgardenScraper

SAMPLE_JP = {
    "@type": "JobPosting",
    "title": "Fertigungskoordinator (m/w/d)",
    "url": "https://abeking.career.softgarden.de/jobs/60111660/Fertigungskoordinator/",
    "datePosted": "2026-05-19T16:28:12.877+02:00",
    "identifier": {"name": "ABEKING & RASMUSSEN SE", "@type": "PropertyValue", "value": 60111660},
    "description": "<b>WIR BAUEN GESCHICHTE</b>",
    "employmentType": "FULL_TIME",
    "hiringOrganization": {"@type": "Organization", "name": "ABEKING & RASMUSSEN"},
    "jobLocation": {
        "@type": "Place",
        "address": {
            "@type": "PostalAddress",
            "postalCode": "26725",
            "addressRegion": "Niedersachsen",
            "addressCountry": "Deutschland",
            "addressLocality": "Emden",
        },
    },
}


def test_softgarden_registered():
    assert "softgarden" in SourceRegistry.registered_ids()


def test_softgarden_has_fetch():
    scraper = SoftgardenScraper()
    assert hasattr(scraper, "fetch")
    assert scraper.source_id == "softgarden"


def test_softgarden_load_slugs_extracts_subdomain(tmp_path: Path):
    portals = {
        "tracked_companies": [
            {"name": "Abeking", "careers_url": "https://abeking.career.softgarden.de/", "enabled": True},
            {"name": "Other", "careers_url": "https://something.ashbyhq.com/", "enabled": True},
            {"name": "Disabled", "careers_url": "https://foo.career.softgarden.de/", "enabled": False},
            {"name": "Alloheim", "careers_url": "https://alloheim.career.softgarden.de", "enabled": True},
        ]
    }
    p = tmp_path / "portals.yaml"
    p.write_text(yaml.dump(portals))
    slugs = SoftgardenScraper()._load_slugs(p)
    assert slugs == ["abeking", "alloheim"]


def test_softgarden_load_slugs_missing_file_returns_empty(tmp_path: Path):
    p = tmp_path / "nonexistent.yaml"
    assert SoftgardenScraper()._load_slugs(p) == []


def test_softgarden_format_location_locality_region():
    s = SoftgardenScraper()
    assert s._format_location(SAMPLE_JP["jobLocation"]) == "Emden, Niedersachsen"


def test_softgarden_format_location_fallbacks():
    s = SoftgardenScraper()
    assert s._format_location({"address": {"addressCountry": "Deutschland"}}) == "Deutschland"
    assert s._format_location({"address": {"postalCode": "10115"}}) == "10115"
    assert s._format_location(None) == ""
    assert s._format_location({}) == ""


def test_softgarden_to_raw_maps_fields():
    s = SoftgardenScraper()
    raw = s._to_raw(SAMPLE_JP, "abeking")
    assert raw.title == "Fertigungskoordinator (m/w/d)"
    assert raw.company == "ABEKING & RASMUSSEN SE"  # identifier.name preferred
    assert raw.external_id == "60111660"
    assert raw.location == "Emden, Niedersachsen"
    assert raw.source == "softgarden"
    assert raw.posted_at is not None
    assert raw.raw_data["employmentType"] == "FULL_TIME"


def test_softgarden_to_raw_falls_back_to_slug_for_company():
    s = SoftgardenScraper()
    jp = {"title": "X", "url": "u", "identifier": {}, "jobLocation": {}}
    raw = s._to_raw(jp, "acme")
    assert raw.company == "acme"
    assert raw.external_id == ""


def test_softgarden_normalize_sets_content_hash():
    s = SoftgardenScraper()
    raw = s._to_raw(SAMPLE_JP, "abeking")
    norm = s.normalize(raw)
    assert norm.content_hash
    assert norm.source == "softgarden"


@pytest.mark.asyncio
async def test_softgarden_fetch_returns_empty_on_no_slugs(tmp_path: Path):
    p = tmp_path / "portals.yaml"
    p.write_text(yaml.dump({"tracked_companies": []}))
    scraper = SoftgardenScraper()
    # Isolate from ats_companies — DB-slug union is covered by test_db_slugs.
    with patch("app.sources.db_slugs.load_active_slugs", return_value=[]):
        result = await scraper.fetch({"portals_file": str(p)})
    assert result == []


def _resp(text: str, elements: list[dict]):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.text = text
    r.json = MagicMock(return_value={"dataFeedElement": elements})
    return r


@pytest.mark.asyncio
async def test_softgarden_checksum_skip_skips_unchanged_board(tmp_path: Path):
    """2 boards: unchanged -> skip (no parse/record); changed -> parse + record."""
    resp_unchanged = _resp("body-unchanged", [{"item": SAMPLE_JP}])
    changed_jp = {**SAMPLE_JP, "identifier": {"name": "Acme", "value": 777}}
    resp_changed = _resp("body-changed", [{"item": changed_jp}])

    http_client = MagicMock()
    http_client.get = AsyncMock(side_effect=[resp_unchanged, resp_changed])
    acm = MagicMock()
    acm.__aenter__ = AsyncMock(return_value=http_client)
    acm.__aexit__ = AsyncMock(return_value=False)

    cache = MagicMock()
    cache.seen_unchanged = AsyncMock(side_effect=[True, False])  # board1 skip, board2 parse
    cache.record = AsyncMock()

    with (
        patch("app.sources.softgarden.merge_slugs", return_value=["b-unchanged", "b-changed"]),
        patch("app.sources.softgarden.httpx.AsyncClient", return_value=acm),
        patch("app.sources.softgarden.FetchCache", return_value=cache),
    ):
        portals = tmp_path / "portals.yaml"
        portals.write_text("tracked_companies: []\n")
        jobs = await SoftgardenScraper().fetch({"portals_file": str(portals)})

    # only the changed board's job is parsed; unchanged board produced nothing
    assert [j.external_id for j in jobs] == ["777"]
    assert cache.seen_unchanged.await_count == 2
    cache.record.assert_awaited_once_with("softgarden", "b-changed", "body-changed")
    # unchanged board never reached JSON parse
    resp_unchanged.json.assert_not_called()
