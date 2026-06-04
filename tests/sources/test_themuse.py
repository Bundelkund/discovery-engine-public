import app.sources.themuse  # noqa: F401
from app.registry.source_registry import SourceRegistry
from app.sources.themuse import TheMuseScraper


def test_themuse_registered():
    assert "themuse" in SourceRegistry.registered_ids()


def test_themuse_has_fetch():
    scraper = TheMuseScraper()
    assert hasattr(scraper, "fetch")
    assert scraper.source_id == "themuse"


def test_themuse_parse_date():
    s = TheMuseScraper()
    assert s._parse_date("") is None
    assert s._parse_date("not-a-date") is None
    assert s._parse_date("2025-06-10T10:01:38Z") is not None
