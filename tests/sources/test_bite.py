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

    drk_job = "https://jobs.drk.de/job-postings/drk-kv-musterstadt-pflegefachkraft-m-w-d"
    xml = (
        "<urlset>"
        f"<url><loc>{JOB_URL}</loc></url>"                                    # SPK /jobposting/<hex>
        "<url><loc>https://karriere.preussischer-kulturbesitz.de/imprint</loc></url>"  # page → skip
        "<url><loc>https://karriere.preussischer-kulturbesitz.de/jobposting/"
        "b23f07066ecdd628bc547cbd22d2a4568749a57e</loc></url>"               # SPK /jobposting/<hex>
        f"<url><loc>{drk_job}</loc></url>"                                    # DRK /job-postings/<slug>
        "<url><loc>https://jobs.drk.de/job-postings</loc></url>"             # bare listing → skip
        "</urlset>"
    )
    locs = _JOB_LOC_RE.findall(xml)
    assert len(locs) == 3  # 2× SPK hex + 1× DRK slug; NOT /imprint, NOT bare /job-postings
    assert JOB_URL in locs
    assert drk_job in locs


def test_bite_normalize_sets_content_hash():
    s = BiteScraper()
    raw = s._parse_posting(_page(SAMPLE_JP), JOB_URL, "SPK")
    norm = s.normalize(raw)
    assert norm.content_hash
    assert norm.source == "bite"


BASE = "https://karriere.preussischer-kulturbesitz.de"


def _resp(content: bytes = b"", text: str = ""):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.content = content
    r.text = text
    return r


def _fail_resp():
    r = MagicMock()
    r.raise_for_status = MagicMock(side_effect=Exception("404"))
    r.content = b""
    r.text = ""
    return r


def _robots(*sitemap_urls: str):
    body = "User-agent: *\nAllow: /\n" + "".join(f"Sitemap: {u}\n" for u in sitemap_urls)
    return _resp(text=body)


def _urlset(*job_urls: str):
    body = "<urlset>" + "".join(f"<url><loc>{u}</loc></url>" for u in job_urls) + "</urlset>"
    return _resp(content=body.encode("utf-8"))


def _sitemapindex(*sub_urls: str):
    body = "<sitemapindex>" + "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in sub_urls) + "</sitemapindex>"
    return _resp(content=body.encode("utf-8"))


def _client(responses: list):
    """Build a mocked httpx.AsyncClient context manager whose .get() returns
    `responses` in order. Returns (http_client, acm) so tests can assert calls."""
    http_client = MagicMock()
    http_client.get = AsyncMock(side_effect=responses)
    acm = MagicMock()
    acm.__aenter__ = AsyncMock(return_value=http_client)
    acm.__aexit__ = AsyncMock(return_value=False)
    return http_client, acm


def _requested(http_client) -> list[str]:
    return [c.args[0] for c in http_client.get.call_args_list]


async def _run_fetch(responses, base=BASE, seen_unchanged=False):
    http_client, acm = _client(responses)
    cache = MagicMock()
    cache.seen_unchanged = AsyncMock(return_value=seen_unchanged)
    cache.record = AsyncMock()
    with (
        patch("app.sources.bite.httpx.AsyncClient", return_value=acm),
        patch("app.sources.bite.FetchCache", return_value=cache),
    ):
        jobs = await BiteScraper().fetch({"sites": [{"name": "SPK", "base_url": base}]})
    return jobs, http_client, cache


@pytest.mark.asyncio
async def test_bite_fetch_parses_sitemap_and_postings():
    """robots → declared sitemap → one posting → one RawJob; index checksum recorded."""
    jobs, _hc, cache = await _run_fetch([
        _robots(f"{BASE}/sitemap.xml.gz"),
        _urlset(JOB_URL),
        _resp(text=_page(SAMPLE_JP)),
    ])
    assert [j.external_id for j in jobs] == ["9d5c7431b403ad6d71b7cc47fd0063c16a2e9ef2"]
    cache.record.assert_awaited_once()


@pytest.mark.asyncio
async def test_bite_fetch_skips_unchanged_index():
    """Unchanged job-URL set (checksum hit) → skip per-posting fetch entirely."""
    jobs, http_client, cache = await _run_fetch(
        [_robots(f"{BASE}/sitemap.xml.gz"), _urlset(JOB_URL)],
        seen_unchanged=True,
    )
    assert jobs == []
    # robots + sitemap are still read (to build the index key); NO posting fetch.
    assert http_client.get.await_count == 2
    cache.record.assert_not_awaited()


@pytest.mark.asyncio
async def test_bite_discovers_sitemap_via_robots_not_default_path():
    """robots declares a NON-default job sitemap (DRK-style) → follow it, never
    request /sitemap.xml.gz."""
    job_sitemap = f"{BASE}/sitemap-job-postings.xml"
    jobs, http_client, _c = await _run_fetch([
        _robots(f"{BASE}/sitemap-static.xml", job_sitemap),
        _urlset(),                       # static sitemap: no jobs
        _urlset(JOB_URL),                # job sitemap
        _resp(text=_page(SAMPLE_JP)),
    ])
    assert [j.external_id for j in jobs] == ["9d5c7431b403ad6d71b7cc47fd0063c16a2e9ef2"]
    reqs = _requested(http_client)
    assert job_sitemap in reqs
    assert f"{BASE}/sitemap.xml.gz" not in reqs  # never guessed the default


@pytest.mark.asyncio
async def test_bite_robots_missing_falls_back_to_default_path():
    """robots.txt unreachable → fall back to DEFAULT_SITEMAP_PATHS."""
    jobs, http_client, _c = await _run_fetch([
        _fail_resp(),          # robots.txt 404
        _urlset(JOB_URL),      # base + /sitemap.xml.gz
        _urlset(),             # base + /sitemap.xml
        _resp(text=_page(SAMPLE_JP)),
    ])
    assert [j.external_id for j in jobs] == ["9d5c7431b403ad6d71b7cc47fd0063c16a2e9ef2"]
    assert f"{BASE}/sitemap.xml.gz" in _requested(http_client)


@pytest.mark.asyncio
async def test_bite_follows_sitemap_index_nesting():
    """robots → sitemap-INDEX → sub-sitemap with the job loc → posting."""
    sub = f"{BASE}/sub-jobs.xml.gz"
    jobs, _hc, _c = await _run_fetch([
        _robots(f"{BASE}/sitemap-index.xml"),
        _sitemapindex(sub),
        _urlset(JOB_URL),
        _resp(text=_page(SAMPLE_JP)),
    ])
    assert [j.external_id for j in jobs] == ["9d5c7431b403ad6d71b7cc47fd0063c16a2e9ef2"]


# ---- probe_site (verify-and-add helper, scripts/bite_discover.py) -----------

@pytest.mark.asyncio
async def test_probe_site_confirms_bite():
    """robots → sitemap → sample posting with the b-ite fingerprint → confirmed."""
    http_client, _acm = _client([
        _robots(f"{BASE}/sitemap.xml.gz"),
        _urlset(JOB_URL),
        _resp(text=_page(SAMPLE_JP)),  # SAMPLE_JP.identifier.name == "Bite JobPosting"
    ])
    v = await BiteScraper().probe_site(http_client, BASE)
    assert v["is_bite"] is True
    assert v["employer"] == "Stiftung Preußischer Kulturbesitz"
    assert v["job_count"] == 1
    assert v["sample_title"].startswith("Sachbearbeiter")


@pytest.mark.asyncio
async def test_probe_site_rejects_non_bite_posting():
    """Sitemap has a job-shaped URL, but the posting carries no b-ite JobPosting."""
    http_client, _acm = _client([
        _robots(f"{BASE}/sitemap.xml.gz"),
        _urlset(JOB_URL),
        _resp(text="<html><body>generic careers page, no structured data</body></html>"),
    ])
    v = await BiteScraper().probe_site(http_client, BASE)
    assert v["is_bite"] is False
    assert "no b-ite JobPosting" in v["reason"]


@pytest.mark.asyncio
async def test_probe_site_rejects_when_no_job_urls():
    """Reachable sitemap but zero b-ite job URLs → rejected before fetching a posting."""
    http_client, _acm = _client([
        _robots(f"{BASE}/sitemap.xml.gz"),
        _urlset(),  # empty urlset
    ])
    v = await BiteScraper().probe_site(http_client, BASE)
    assert v["is_bite"] is False
    assert v["job_count"] == 0
    assert "no b-ite job URLs" in v["reason"]
    assert http_client.get.await_count == 2  # robots + sitemap only, no posting
