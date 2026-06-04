import pytest

import app.sources.careerjet  # noqa: F401
from app.registry.source_registry import SourceRegistry
from app.sources.careerjet import CareerjetScraper


def test_careerjet_registered():
    assert "careerjet" in SourceRegistry.registered_ids()


def test_careerjet_has_fetch():
    scraper = CareerjetScraper()
    assert hasattr(scraper, "fetch")
    assert scraper.source_id == "careerjet"


@pytest.mark.asyncio
async def test_careerjet_fetch_empty_without_affid(monkeypatch):
    # No affid in config and none in settings -> skip gracefully, no network call.
    from types import SimpleNamespace

    import app.sources.careerjet as mod

    monkeypatch.setattr(mod, "get_settings", lambda: SimpleNamespace(careerjet_affid=""))
    scraper = CareerjetScraper()
    result = await scraper.fetch({"affid": ""})
    assert result == []
