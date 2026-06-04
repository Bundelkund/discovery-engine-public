import pytest

import app.sources.jooble  # noqa: F401
from app.registry.source_registry import SourceRegistry
from app.sources.jooble import JoobleScraper


def test_jooble_registered():
    assert "jooble" in SourceRegistry.registered_ids()


def test_jooble_has_fetch():
    scraper = JoobleScraper()
    assert hasattr(scraper, "fetch")
    assert scraper.source_id == "jooble"


@pytest.mark.asyncio
async def test_jooble_fetch_empty_without_key(monkeypatch):
    # No api_key in config and none in settings -> skip gracefully, no network call.
    from types import SimpleNamespace

    import app.sources.jooble as mod

    monkeypatch.setattr(mod, "get_settings", lambda: SimpleNamespace(jooble_api_key=""))
    scraper = JoobleScraper()
    result = await scraper.fetch({"api_key": ""})
    assert result == []


def test_jooble_parse_date():
    s = JoobleScraper()
    assert s._parse_date("") is None
    assert s._parse_date("not-a-date") is None
    assert s._parse_date("2024-03-12T00:00:00.0000000") is not None
