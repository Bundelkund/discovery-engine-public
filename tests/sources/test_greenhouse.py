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
