import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.sources.bite  # noqa: F401
from app.registry.source_registry import SourceRegistry
from app.sources.bite import BiteScraper

# Trimmed real SPK posting (schema.org JobPosting), as embedded on the page.
SAMPLE_JP = {
    "@context": "https://schema.org/",
    "@type": "JobPosting",
    "title": "Sachbearbeiter:in für Personalentwicklung / KI (w/d/m)",
    "description": "<div>Wir gestalten Kultur…</div>",
    "identifier": {
        "@type": "PropertyValue",
        "name": "Bite JobPosting",
        "value": "9d5c7431b403ad6d71b7cc47fd0063c16a2e9ef2",
    },
    "datePosted": "2026-06-11",
    "validThrough": "2026-07-08T21:59:00+00:00",
    "employmentType": ["FULL_TIME", "PART_TIME"],
    "hiringOrganization": {"@type": "Organization", "name": "Stiftung Preußischer Kulturbesitz"},
    "jobLocation": {
        "@type": "Place",
        "address": {
            "@type": "PostalAddress",
            "streetAddress": "Kurfürstendamm 40",
            "addressLocality": "Berlin",
            "addressCountry": "de",
            "postalCode": "10719",
            "addressRegion": "BE",
        },
    },
}

JOB_URL = "https://karriere.preussischer-kulturbesitz.de/jobposting/9d5c7431b403ad6d71b7cc47fd0063c16a2e9ef2"


def _page(jp: dict) -> str:
    """Wrap a JobPosting dict in a page like the real b-ite posting HTML."""
    return (
        "<html><head>"
        f'<script type="application/ld+json">{json.dumps(jp, ensure_ascii=False)}</script>'
        "</head><body>…</body></html>"
    )


def test_bite_registered():
    assert "bite" in SourceRegistry.registered_ids()


def test_bite_has_fetch():
    s = BiteScraper()
    assert hasattr(s, "fetch")
    assert s.source_id == "bite"


def test_bite_parse_posting_maps_fields():
    raw = BiteScraper()._parse_posting(_page(SAMPLE_JP), JOB_URL + "?ref=linkedin", "Fallback Name")
    assert raw is not None
    assert raw.title == "Sachbearbeiter:in für Personalentwicklung / KI (w/d/m)"
    assert raw.company == "Stiftung Preußischer Kulturbesitz"
    assert raw.external_id == "9d5c7431b403ad6d71b7cc47fd0063c16a2e9ef2"  # identifier.value
    assert raw.url == JOB_URL  # query string stripped
    assert raw.location == "Berlin, BE"
    assert raw.source == "bite"
    assert raw.posted_at is not None and raw.posted_at.year == 2026
    assert raw.raw_data["validThrough"].startswith("2026-07-08")


def test_bite_external_id_falls_back_to_url_sha():
    jp = {**SAMPLE_JP}
    jp.pop("identifier")
    raw = BiteScraper()._parse_posting(_page(jp), JOB_URL, "SPK")
    assert raw.external_id == "9d5c7431b403ad6d71b7cc47fd0063c16a2e9ef2"


def test_bite_company_falls_back_to_site_name():
    jp = {**SAMPLE_JP}
    jp.pop("hiringOrganization")
    raw = BiteScraper()._parse_posting(_page(jp), JOB_URL, "SPK site name")
    assert raw.company == "SPK site name"


def test_bite_parse_posting_rejects_non_jobposting_and_missing_ldjson():
    s = BiteScraper()
    assert s._parse_posting("<html>no ld+json here</html>", JOB_URL, "x") is None
    org_page = _page({"@type": "Organization", "name": "SPK"})
    assert s._parse_posting(org_page, JOB_URL, "x") is None


def test_bite_format_location():
    s = BiteScraper()
    assert s._format_location(SAMPLE_JP["jobLocation"]) == "Berlin, BE"
    assert s._format_location({"address": {"addressLocality": "Berlin"}}) == "Berlin"
    assert s._format_location({"address": {"addressCountry": "de"}}) == "de"
    assert s._format_location(None) == ""


def test_bite_parse_date_variants():
    s = BiteScraper()
    assert s._parse_date("2026-06-11").year == 2026
    assert s._parse_date("2026-07-08T21:59:00+00:00").month == 7
    assert s._parse_date(None) is None
    assert s._parse_date("not-a-date") is None


def test_bite_sitemap_regex_extracts_job_locs():
    from app.sources.bite import _JOB_LOC_RE

    xml = (
        "<urlset>"
        f"<url><loc>{JOB_URL}</loc></url>"
        "<url><loc>https://karriere.preussischer-kulturbesitz.de/imprint</loc></url>"
        "<url><loc>https://karriere.preussischer-kulturbesitz.de/jobposting/"
        "b23f07066ecdd628bc547cbd22d2a4568749a57e</loc></url>"
        "</urlset>"
    )
    locs = _JOB_LOC_RE.findall(xml)
    assert len(locs) == 2  # only /jobposting/<sha40>, not /imprint
    assert JOB_URL in locs


def test_bite_normalize_sets_content_hash():
    s = BiteScraper()
    raw = s._parse_posting(_page(SAMPLE_JP), JOB_URL, "SPK")
    norm = s.normalize(raw)
    assert norm.content_hash
    assert norm.source == "bite"


def _resp(content: bytes = b"", text: str = ""):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.content = content
    r.text = text
    return r


@pytest.mark.asyncio
async def test_bite_fetch_parses_sitemap_and_postings():
    """sitemap (plain XML) → one posting fetched → one RawJob; sitemap checksum recorded."""
    sitemap_xml = f"<urlset><url><loc>{JOB_URL}</loc></url></urlset>"
    posting = _resp(text=_page(SAMPLE_JP))
    sitemap = _resp(content=sitemap_xml.encode("utf-8"))

    http_client = MagicMock()
    http_client.get = AsyncMock(side_effect=[sitemap, posting])
    acm = MagicMock()
    acm.__aenter__ = AsyncMock(return_value=http_client)
    acm.__aexit__ = AsyncMock(return_value=False)

    cache = MagicMock()
    cache.seen_unchanged = AsyncMock(return_value=False)
    cache.record = AsyncMock()

    with (
        patch("app.sources.bite.httpx.AsyncClient", return_value=acm),
        patch("app.sources.bite.FetchCache", return_value=cache),
    ):
        jobs = await BiteScraper().fetch(
            {"sites": [{"name": "SPK", "base_url": "https://karriere.preussischer-kulturbesitz.de"}]}
        )

    assert [j.external_id for j in jobs] == ["9d5c7431b403ad6d71b7cc47fd0063c16a2e9ef2"]
    cache.record.assert_awaited_once()


@pytest.mark.asyncio
async def test_bite_fetch_skips_unchanged_sitemap():
    """Unchanged sitemap (checksum hit) → skip site entirely, never fetch postings."""
    sitemap = _resp(content=b"<urlset/>")
    http_client = MagicMock()
    http_client.get = AsyncMock(return_value=sitemap)
    acm = MagicMock()
    acm.__aenter__ = AsyncMock(return_value=http_client)
    acm.__aexit__ = AsyncMock(return_value=False)

    cache = MagicMock()
    cache.seen_unchanged = AsyncMock(return_value=True)  # unchanged → skip
    cache.record = AsyncMock()

    with (
        patch("app.sources.bite.httpx.AsyncClient", return_value=acm),
        patch("app.sources.bite.FetchCache", return_value=cache),
    ):
        jobs = await BiteScraper().fetch(
            {"sites": [{"name": "SPK", "base_url": "https://x.de"}]}
        )

    assert jobs == []
    assert http_client.get.await_count == 1  # only the sitemap, no postings
    cache.record.assert_not_awaited()
