import pytest

import app.sources.arbeitsagentur  # noqa: F401
from app.registry.source_registry import SourceRegistry
from app.sources.arbeitsagentur import ArbeitsagenturScraper


def test_arbeitsagentur_registered():
    assert "arbeitsagentur" in SourceRegistry.registered_ids()


def test_arbeitsagentur_has_fetch():
    scraper = ArbeitsagenturScraper()
    assert hasattr(scraper, "fetch")
    assert scraper.source_id == "arbeitsagentur"


def test_parse_listing_builds_portal_url_when_no_external():
    s = ArbeitsagenturScraper()
    item = {
        "titel": "Agile Coach",
        "refnr": "10000-1234567890-S",
        "arbeitgeber": "HDI AG",
        "arbeitsort": {"plz": "30659", "ort": "Hannover", "region": "Niedersachsen"},
        "aktuelleVeroeffentlichungsdatum": "2026-05-20",
    }
    raw = s._parse_listing(item, item["refnr"], "Agile Coach")
    assert raw.title == "Agile Coach"
    assert raw.company == "HDI AG"
    assert raw.location == "30659 Hannover Niedersachsen"
    assert raw.external_id == "ba_10000-1234567890-S"
    assert raw.url == "https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-1234567890-S"
    assert raw.posted_at is not None


def test_parse_listing_prefers_external_url():
    s = ArbeitsagenturScraper()
    item = {
        "titel": "KI Trainer",
        "refnr": "ABC-1",
        "arbeitgeber": "X",
        "externeUrl": "https://x.example/job/1",
    }
    raw = s._parse_listing(item, "ABC-1", "KI Trainer")
    assert raw.url == "https://x.example/job/1"
    assert raw.raw_data["externeUrl"] == "https://x.example/job/1"


def test_parse_date():
    s = ArbeitsagenturScraper()
    assert s._parse_date("") is None
    assert s._parse_date("not-a-date") is None
    assert s._parse_date("2026-05-20") is not None


@pytest.mark.asyncio
async def test_fetch_search_then_detail(monkeypatch):
    """Search yields refnr metadata; detail backfills description."""
    import app.sources.arbeitsagentur as mod

    search_payload = {
        "stellenangebote": [
            {
                "titel": "Agile Coach",
                "refnr": "R1",
                "arbeitgeber": "HDI AG",
                "arbeitsort": {"ort": "Hannover"},
            },
            # duplicate refnr across a second term -> deduped
            {
                "titel": "Agile Coach",
                "refnr": "R1",
                "arbeitgeber": "HDI AG",
                "arbeitsort": {"ort": "Hannover"},
            },
        ]
    }
    detail_payload = {"stellenangebotsBeschreibung": "Volltext HDI SAFe Scrum"}

    class FakeResp:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            if "/jobdetails/" in url:
                return FakeResp(detail_payload)
            return FakeResp(search_payload)

    monkeypatch.setattr(mod.httpx, "AsyncClient", FakeClient)

    scraper = ArbeitsagenturScraper()
    jobs = await scraper.fetch({"search_terms": ["Agile Coach"], "size": 10})

    assert len(jobs) == 1  # deduped by refnr
    assert jobs[0].company == "HDI AG"
    assert jobs[0].description == "Volltext HDI SAFe Scrum"


@pytest.mark.asyncio
async def test_fetch_returns_empty_on_total_failure(monkeypatch):
    import app.sources.arbeitsagentur as mod

    class BoomClient:
        def __init__(self, *a, **k):
            raise RuntimeError("boom")

    monkeypatch.setattr(mod.httpx, "AsyncClient", BoomClient)
    scraper = ArbeitsagenturScraper()
    assert await scraper.fetch({"search_terms": ["x"]}) == []
