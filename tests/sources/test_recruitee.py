from pathlib import Path

import pytest
import yaml

import app.sources.recruitee  # noqa: F401
from app.registry.source_registry import SourceRegistry
from app.sources.recruitee import RecruiteeScraper


def test_recruitee_registered():
    assert "recruitee" in SourceRegistry.registered_ids()


def test_recruitee_has_fetch():
    scraper = RecruiteeScraper()
    assert hasattr(scraper, "fetch")
    assert scraper.source_id == "recruitee"


def test_recruitee_load_slugs_extracts_from_careers_url(tmp_path: Path):
    portals = {
        "tracked_companies": [
            {"name": "Tellent", "careers_url": "https://jobs.recruitee.com/", "enabled": True},
            {"name": "Other", "careers_url": "https://something.ashbyhq.com/", "enabled": True},
            {"name": "Disabled", "careers_url": "https://foo.recruitee.com/", "enabled": False},
            {"name": "Acme", "careers_url": "https://acme.recruitee.com", "enabled": True},
        ]
    }
    p = tmp_path / "portals.yaml"
    p.write_text(yaml.dump(portals))
    slugs = RecruiteeScraper()._load_slugs(p)
    assert slugs == ["jobs", "acme"]


def test_recruitee_load_slugs_missing_file_returns_empty(tmp_path: Path):
    p = tmp_path / "nonexistent.yaml"
    assert RecruiteeScraper()._load_slugs(p) == []


@pytest.mark.asyncio
async def test_recruitee_fetch_returns_empty_on_no_slugs(tmp_path: Path):
    p = tmp_path / "portals.yaml"
    p.write_text(yaml.dump({"tracked_companies": []}))
    scraper = RecruiteeScraper()
    result = await scraper.fetch({"portals_file": str(p)})
    assert result == []
