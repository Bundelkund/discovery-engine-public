from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.sources.linkedin  # noqa: F401  (registers adapter)
from app.registry.source_registry import SourceRegistry
from app.sources.linkedin import LinkedInScraper


def test_linkedin_registered():
    assert "linkedin" in SourceRegistry.registered_ids()


def test_linkedin_has_fetch():
    scraper = LinkedInScraper()
    assert hasattr(scraper, "fetch")
    assert scraper.source_id == "linkedin"


def test_to_raw_job_maps_apify_fields():
    item = {
        "title": "AI Coach",
        "companyName": "ACME",
        "location": "Berlin, DE",
        "jobUrl": "https://www.linkedin.com/jobs/view/123",
        "jobId": "lk_123",
        "description": "Lead AI enablement.",
    }
    raw = LinkedInScraper._to_raw_job(item)
    assert raw.title == "AI Coach"
    assert raw.company == "ACME"
    assert raw.location == "Berlin, DE"
    assert raw.url == "https://www.linkedin.com/jobs/view/123"
    assert raw.external_id == "lk_123"
    assert raw.source == "linkedin"
    assert raw.raw_data == item


def test_to_raw_job_falls_back_to_url_when_id_missing():
    raw = LinkedInScraper._to_raw_job(
        {"jobTitle": "X", "company": "Y", "link": "https://x.example/j/1"}
    )
    assert raw.title == "X"
    assert raw.company == "Y"
    assert raw.url == "https://x.example/j/1"
    assert raw.external_id == "https://x.example/j/1"


def test_has_job_filters_empty_items():
    assert LinkedInScraper._has_job({"title": "X"}) is True
    assert LinkedInScraper._has_job({"jobTitle": "Y"}) is True
    assert LinkedInScraper._has_job({}) is False
    assert LinkedInScraper._has_job({"_empty": True}) is False


@pytest.mark.asyncio
async def test_fetch_returns_empty_when_token_missing():
    scraper = LinkedInScraper()
    with patch("app.sources.linkedin.get_settings") as mock_settings:
        mock_settings.return_value.apify_api_token = ""
        result = await scraper.fetch({"search_terms": ["AI Coach"]})
    assert result == []


@pytest.mark.asyncio
async def test_fetch_returns_empty_when_no_search_terms():
    scraper = LinkedInScraper()
    with patch("app.sources.linkedin.get_settings") as mock_settings:
        mock_settings.return_value.apify_api_token = "fake-token"
        result = await scraper.fetch({"search_terms": []})
    assert result == []


@pytest.mark.asyncio
async def test_fetch_calls_apify_with_built_urls_and_maps_results():
    scraper = LinkedInScraper()

    apify_response = MagicMock()
    apify_response.raise_for_status = MagicMock()
    apify_response.json = MagicMock(
        return_value=[
            {
                "title": "AI Coach",
                "companyName": "ACME",
                "location": "Berlin",
                "jobUrl": "https://www.linkedin.com/jobs/view/1",
                "jobId": "1",
            },
            {"_empty": True},
        ]
    )

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=apify_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("app.sources.linkedin.get_settings") as mock_settings,
        patch("app.sources.linkedin.httpx.AsyncClient", return_value=mock_client),
    ):
        mock_settings.return_value.apify_api_token = "fake-token"
        result = await scraper.fetch(
            {
                "search_terms": ["AI Coach", "KI Berater"],
                "location": "Berlin",
                "results_per_term": 25,
            }
        )

    assert len(result) == 1
    assert result[0].title == "AI Coach"
    assert result[0].source == "linkedin"

    mock_client.post.assert_awaited_once()
    call = mock_client.post.await_args
    assert call.kwargs["params"] == {"token": "fake-token"}
    body = call.kwargs["json"]
    assert body["count"] == 25
    assert body["scrapeCompany"] is False
    assert len(body["urls"]) == 2
    assert "AI+Coach" in body["urls"][0]
    assert "KI+Berater" in body["urls"][1]
    assert "Berlin" in body["urls"][0]


@pytest.mark.asyncio
async def test_fetch_swallows_http_error():
    scraper = LinkedInScraper()
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=RuntimeError("network down"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("app.sources.linkedin.get_settings") as mock_settings,
        patch("app.sources.linkedin.httpx.AsyncClient", return_value=mock_client),
    ):
        mock_settings.return_value.apify_api_token = "fake-token"
        result = await scraper.fetch({"search_terms": ["X"]})

    assert result == []
