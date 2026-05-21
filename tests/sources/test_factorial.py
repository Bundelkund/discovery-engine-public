from pathlib import Path

import pytest
import yaml

import app.sources.factorial  # noqa: F401
from app.registry.source_registry import SourceRegistry
from app.sources.factorial import FactorialScraper

SAMPLE_SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
<url>
<loc>https://emission-partner.factorialhr.de</loc>
<priority>0.9</priority>
<lastmod>2026-05-20</lastmod>
</url>
<url>
<loc>https://emission-partner.factorialhr.de/job_posting/servicetechniker-im-aussendienst-m-w-d-257603</loc>
<priority>0.3</priority>
<lastmod>2026-03-05</lastmod>
</url>
<url>
<loc>https://emission-partner.factorialhr.de/job_posting/elektroniker-oder-elektrokonstrukteur-m-w-d-257435</loc>
<priority>0.3</priority>
<lastmod>2026-05-07</lastmod>
</url>
<url>
<loc>https://emission-partner.factorialhr.de/some-other-page</loc>
<lastmod>2026-04-01</lastmod>
</url>
</urlset>
"""

SAMPLE_DETAIL_HTML = """<!DOCTYPE html><html lang='de'><head>
<title>Servicetechniker im Außendienst für den Raum Jena</title>
<meta content='Servicetechniker im Außendienst' property='og:title'>
</head><body>
<div class='wrap'>
<h1 class='heading'>Servicetechniker im Außendienst für den Raum Jena/ Gotha/ Eisenach (m/w/d)</h1>
<div class='sidebar'>
  <ul>
    <li><span class='label'>Unbefristet</span></li>
    <li><span class='label'>Vollzeit</span></li>
    <li><span class='label'>Remote</span></li>
    <li><span class='label'>Service</span></li>
  </ul>
</div>
<div class='content'>
<p>An unserem Standort in Strücklingen entwickeln wir Katalysatoren für die Abgasreinigung.</p>
<p>Möchtest du die Energiewende voranbringen?</p>
<p>Deine Aufgaben: Montage und Inbetriebnahme der Anlagen vor Ort beim Kunden.</p>
</div>
<button>Jetzt bewerben</button>
<footer><p>Cookie-Hinweis: Wir verwenden Cookies.</p></footer>
</body></html>
"""


def test_factorial_registered():
    assert "factorial" in SourceRegistry.registered_ids()


def test_factorial_has_fetch():
    scraper = FactorialScraper()
    assert hasattr(scraper, "fetch")
    assert scraper.source_id == "factorial"


def test_factorial_load_slugs_extracts_de_and_com(tmp_path: Path):
    portals = {
        "tracked_companies": [
            {"name": "Emission", "careers_url": "https://emission-partner.factorialhr.de/", "enabled": True},
            {"name": "DeepImmo", "careers_url": "https://deepimmo.factorialhr.de", "enabled": True},
            {"name": "ExampleCom", "careers_url": "https://acme.factorialhr.com/", "enabled": True},
            {"name": "Other", "careers_url": "https://foo.ashbyhq.com/", "enabled": True},
            {"name": "Disabled", "careers_url": "https://x.factorialhr.de", "enabled": False},
        ]
    }
    p = tmp_path / "portals.yaml"
    p.write_text(yaml.dump(portals))
    entries = FactorialScraper()._load_slugs(p)
    assert entries == [
        ("emission-partner", "de"),
        ("deepimmo", "de"),
        ("acme", "com"),
    ]


def test_factorial_parse_sitemap_filters_job_postings():
    scraper = FactorialScraper()
    jobs = scraper._parse_sitemap(SAMPLE_SITEMAP, "emission-partner")
    assert len(jobs) == 2
    titles = [j.title for j in jobs]
    ids = [j.external_id for j in jobs]
    urls = [j.url for j in jobs]
    assert "257603" in ids
    assert "257435" in ids
    assert all("/job_posting/" in u for u in urls)
    assert "Servicetechniker" in titles[0]
    assert all(j.company == "emission-partner" for j in jobs)
    assert all(j.source == "factorial" for j in jobs)


def test_factorial_slug_to_title():
    s = FactorialScraper()
    assert (
        s._slug_to_title("servicetechniker-im-aussendienst-m-w-d")
        == "Servicetechniker Im Aussendienst M W D"
    )
    assert s._slug_to_title("") == ""


def test_factorial_parse_sitemap_handles_malformed_xml():
    scraper = FactorialScraper()
    assert scraper._parse_sitemap("not xml at all", "x") == []


def test_factorial_extract_h1_preserves_umlauts():
    s = FactorialScraper()
    assert (
        s._extract_h1(SAMPLE_DETAIL_HTML)
        == "Servicetechniker im Außendienst für den Raum Jena/ Gotha/ Eisenach (m/w/d)"
    )


def test_factorial_extract_sidebar_returns_4_metadata_items():
    s = FactorialScraper()
    items = s._extract_sidebar(SAMPLE_DETAIL_HTML)
    assert items == ["Unbefristet", "Vollzeit", "Remote", "Service"]


def test_factorial_pick_location_matches_work_model_keyword():
    s = FactorialScraper()
    assert s._pick_location(["Unbefristet", "Vollzeit", "Remote", "Service"]) == "Remote"
    assert s._pick_location(["Unbefristet", "Vor Ort", "Service"]) == "Vor Ort"
    assert s._pick_location(["Unbefristet", "Vollzeit"]) == ""


def test_factorial_pick_location_falls_back_to_postal_code_or_country():
    s = FactorialScraper()
    assert (
        s._pick_location(
            ["Unbefristet", "Vollzeit", "96047, Bamberg, Bayern, Deutschland", "Engineering"]
        )
        == "96047, Bamberg, Bayern, Deutschland"
    )
    assert (
        s._pick_location(["Unbefristet", "Vollzeit", "Wien, Österreich", "Sales"])
        == "Wien, Österreich"
    )


def test_factorial_pick_location_prefers_work_model_over_address():
    s = FactorialScraper()
    assert (
        s._pick_location(
            ["Unbefristet", "10405, Berlin", "Hybrid (10405, Berlin)", "Product"]
        )
        == "Hybrid (10405, Berlin)"
    )


def test_factorial_extract_description_strips_sidebar_and_footer():
    s = FactorialScraper()
    sidebar = ["Unbefristet", "Vollzeit", "Remote", "Service"]
    desc = s._extract_description(SAMPLE_DETAIL_HTML, sidebar)
    assert "Cookie-Hinweis" not in desc, "footer after end marker must be excluded"
    assert "Jetzt bewerben" not in desc, "end marker text must be excluded"
    assert "Strücklingen" in desc, "umlauts preserved"
    assert "Energiewende" in desc
    assert "Aufgaben" in desc
    for item in sidebar:
        assert item not in desc, f"sidebar item '{item}' should be stripped"


def test_factorial_merge_detail_promotes_real_title_and_metadata():
    s = FactorialScraper()
    stubs = s._parse_sitemap(SAMPLE_SITEMAP, "emission-partner")
    enriched = s._merge_detail(stubs[0], SAMPLE_DETAIL_HTML)
    assert "Außendienst" in enriched.title
    assert enriched.location == "Remote"
    assert "Energiewende" in enriched.description
    assert enriched.raw_data["sidebar"] == ["Unbefristet", "Vollzeit", "Remote", "Service"]
    assert enriched.raw_data["work_model"] == "Remote"
    assert enriched.external_id == "257603"


def test_factorial_merge_detail_falls_back_to_stub_title_when_no_h1():
    s = FactorialScraper()
    stubs = s._parse_sitemap(SAMPLE_SITEMAP, "emission-partner")
    enriched = s._merge_detail(stubs[0], "<html><body>nothing useful</body></html>")
    assert enriched.title == stubs[0].title
    assert enriched.description == ""
    assert enriched.location == ""


@pytest.mark.asyncio
async def test_factorial_fetch_returns_empty_on_no_slugs(tmp_path: Path):
    p = tmp_path / "portals.yaml"
    p.write_text(yaml.dump({"tracked_companies": []}))
    scraper = FactorialScraper()
    result = await scraper.fetch({"portals_file": str(p)})
    assert result == []
