"""Curated-slug seed path (lever): slug extraction, list-mode loader, seed source tag."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ats_scanner  # noqa: E402
import fetch_lever_theirstack as ftk  # noqa: E402
import seed_ats_companies  # noqa: E402


# --- fetch_lever_theirstack.slug_from_url -------------------------------------
def test_slug_from_url_keeps_dotted_slug():
    assert ftk.slug_from_url("https://jobs.lever.co/valpeo.com/c3e1?x=1") == "valpeo.com"


def test_slug_from_url_lowercases_and_strips_job_id():
    assert ftk.slug_from_url("https://jobs.lever.co/Emma-Sleep/b8d9-413") == "emma-sleep"


def test_slug_from_url_non_lever_returns_none():
    assert ftk.slug_from_url("https://boards.greenhouse.io/foo") is None
    assert ftk.slug_from_url("") is None


# --- ats_scanner._load_slugs_file ---------------------------------------------
def test_load_slugs_file_skips_comments_blanks_lowercases(tmp_path: Path):
    p = tmp_path / "lever.txt"
    p.write_text("# header\n\nMistral\nvalpeo.com\n  Qonto  \n", encoding="utf-8")
    assert ats_scanner._load_slugs_file(str(p)) == ["mistral", "valpeo.com", "qonto"]


def test_load_slugs_file_preserves_dots(tmp_path: Path):
    p = tmp_path / "lever.txt"
    p.write_text("valpeo.com\n", encoding="utf-8")
    assert ats_scanner._load_slugs_file(str(p)) == ["valpeo.com"]


# --- seed_ats_companies.row_from provenance tag (col renamed source->origin) ---
# Canonical rename: row key `source` now holds the provider (= the `ats` arg);
# provenance (cc/scrape/manual/curated) moved to `origin`. See docs/adr/sources-dimension.md.
_V = {"slug": "qonto", "active": True, "job_count": 4, "feed_url": "u",
      "de_flag": "de", "sample_titles": ["AI Eng"]}


def test_row_from_canonical_source_is_provider():
    assert seed_ats_companies.row_from("lever", _V, [], "curated")["source"] == "lever"


def test_row_from_honors_curated_origin():
    assert seed_ats_companies.row_from("lever", _V, [], "curated")["origin"] == "curated"


def test_row_from_origin_defaults_to_cc():
    assert seed_ats_companies.row_from("lever", _V, [])["origin"] == "cc"


def test_row_from_active_sets_monitor_and_status():
    r = seed_ats_companies.row_from("lever", _V, [], "curated")
    assert r["status"] == "active"
    assert r["monitor"] is True
    assert r["last_job_count"] == 4
