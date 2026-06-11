"""Unit tests for source_meta (sources-dimension type lookup)."""
from unittest.mock import patch

from app.sources import source_meta

_FIXTURE = {
    "greenhouse": "ats",
    "indeed": "aggregator",
    "rss": "feed",
    "company_radar": "internal",
}


def test_source_type_classifies_known_codes():
    with patch.object(source_meta, "_types", return_value=_FIXTURE):
        assert source_meta.source_type("greenhouse") == "ats"
        assert source_meta.source_type("indeed") == "aggregator"
        assert source_meta.source_type("rss") == "feed"
        assert source_meta.source_type("company_radar") == "internal"


def test_source_type_unknown_for_unmapped():
    with patch.object(source_meta, "_types", return_value=_FIXTURE):
        assert source_meta.source_type("does_not_exist") == "unknown"


def test_is_aggregator():
    with patch.object(source_meta, "_types", return_value=_FIXTURE):
        assert source_meta.is_aggregator("indeed") is True
        assert source_meta.is_aggregator("greenhouse") is False


def test_types_empty_on_no_creds():
    source_meta._types.cache_clear()
    from types import SimpleNamespace

    with patch("app.config.get_settings", return_value=SimpleNamespace(supabase_url="", supabase_key="")):
        assert source_meta._types() == {}
    source_meta._types.cache_clear()
