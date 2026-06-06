from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

import app.sources.breezy  # noqa: F401
from app.registry.source_registry import SourceRegistry
from app.sources.breezy import BreezyScraper


def test_breezy_registered():
    assert "breezy" in SourceRegistry.registered_ids()


def test_breezy_has_fetch():
    scraper = BreezyScraper()
    assert hasattr(scraper, "fetch")
    assert scraper.source_id == "breezy"


def test_breezy_load_slugs_extracts_from_careers_url(tmp_path: Path):
    portals = {
        "tracked_companies": [
            {"name": "Breezy", "careers_url": "https://breezy.breezy.hr/", "enabled": True},
            {"name": "Other", "careers_url": "https://something.ashbyhq.com/", "enabled": True},
            {"name": "Disabled", "careers_url": "https://foo.breezy.hr/", "enabled": False},
            {"name": "Acme", "careers_url": "https://acme.breezy.hr", "enabled": True},
        ]
    }
    p = tmp_path / "portals.yaml"
    p.write_text(yaml.dump(portals))
    slugs = BreezyScraper()._load_slugs(p)
    assert slugs == ["breezy", "acme"]


def test_breezy_format_location_full():
    s = BreezyScraper()
    loc = {
        "city": "Berlin",
        "state": {"name": "Berlin"},
        "country": {"name": "Germany"},
    }
    assert s._format_location(loc) == "Berlin, Berlin, Germany"


def test_breezy_format_location_partial():
    s = BreezyScraper()
    assert s._format_location({"city": "Remote"}) == "Remote"
    assert s._format_location({}) == ""
    assert s._format_location({"country": {"name": "Germany"}}) == "Germany"


@pytest.mark.asyncio
async def test_breezy_fetch_returns_empty_on_no_slugs(tmp_path: Path):
    p = tmp_path / "portals.yaml"
    p.write_text(yaml.dump({"tracked_companies": []}))
    scraper = BreezyScraper()
    # Isolate from ats_companies — DB-slug union is covered by test_db_slugs.
    with patch("app.sources.db_slugs.load_active_slugs", return_value=[]):
        result = await scraper.fetch({"portals_file": str(p)})
    assert result == []
