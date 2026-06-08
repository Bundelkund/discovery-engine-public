"""Tests for app/services/terms_provider.py.

Coverage:
  - Default (local) provider reads from sources.yaml search_terms
  - UnionTermsProvider deduplicates overlapping terms, returns list[str] with no profile_id
  - Config flag selects the provider
"""
import pytest
from unittest.mock import patch, MagicMock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAKE_SOURCES_CFG = {
    "sources": {
        "adzuna": {
            "search_terms": ["AI Consultant", "KI Berater", "AI Consultant"],  # intentional dup
        },
        "indeed": {
            "search_terms": ["AI Consultant", "Agile Coach"],
        },
    }
}

FAKE_SOURCES_CFG_UNION_PROVIDER = {
    **FAKE_SOURCES_CFG,
    "terms_provider": "union",
    "terms_provider_url": "http://tenant.local",
}

FAKE_SOURCES_CFG_LOCAL_EXPLICIT = {
    **FAKE_SOURCES_CFG,
    "terms_provider": "local",
}


# ---------------------------------------------------------------------------
# LocalTermsProvider — default behaviour
# ---------------------------------------------------------------------------


def test_local_provider_returns_source_terms():
    """Default provider reads search_terms from sources.yaml for a source_id."""
    from app.services.terms_provider import LocalTermsProvider

    provider = LocalTermsProvider()
    with patch("app.services.terms_provider.load_sources_config", return_value=FAKE_SOURCES_CFG):
        terms = provider.get_terms(source_id="adzuna")

    assert isinstance(terms, list)
    assert all(isinstance(t, str) for t in terms)
    assert "AI Consultant" in terms
    assert "KI Berater" in terms


def test_local_provider_deduplicates():
    """LocalTermsProvider returns a distinct list even when sources.yaml has dupes."""
    from app.services.terms_provider import LocalTermsProvider

    provider = LocalTermsProvider()
    with patch("app.services.terms_provider.load_sources_config", return_value=FAKE_SOURCES_CFG):
        terms = provider.get_terms(source_id="adzuna")

    # "AI Consultant" appears twice in the fixture — should appear only once
    assert terms.count("AI Consultant") == 1


def test_local_provider_unknown_source_returns_empty():
    """Unknown source_id returns empty list, not an error."""
    from app.services.terms_provider import LocalTermsProvider

    provider = LocalTermsProvider()
    with patch("app.services.terms_provider.load_sources_config", return_value=FAKE_SOURCES_CFG):
        terms = provider.get_terms(source_id="nonexistent_source")

    assert terms == []


def test_local_provider_no_source_id_returns_union_across_all():
    """source_id=None returns a deduplicated union across all configured sources."""
    from app.services.terms_provider import LocalTermsProvider

    provider = LocalTermsProvider()
    with patch("app.services.terms_provider.load_sources_config", return_value=FAKE_SOURCES_CFG):
        terms = provider.get_terms(source_id=None)

    # "AI Consultant" appears in both adzuna and indeed — must not duplicate
    assert terms.count("AI Consultant") == 1
    assert "KI Berater" in terms
    assert "Agile Coach" in terms


# ---------------------------------------------------------------------------
# UnionTermsProvider — inert behind default flag
# ---------------------------------------------------------------------------


def test_union_provider_deduplicates_two_overlapping_profiles():
    """UnionTermsProvider deduplicates terms from two overlapping server responses."""
    from app.services.terms_provider import UnionTermsProvider

    # Server returns two profiles' terms merged (with overlap)
    server_terms = ["AI Consultant", "KI Berater", "AI Consultant", "Agile Coach"]
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"terms": server_terms}
    mock_resp.raise_for_status = MagicMock()

    provider = UnionTermsProvider(base_url="http://tenant.local")
    with patch("httpx.get", return_value=mock_resp):
        terms = provider.get_terms()

    assert isinstance(terms, list)
    assert all(isinstance(t, str) for t in terms)
    assert terms.count("AI Consultant") == 1
    # No profile_id anywhere in the returned data
    for t in terms:
        assert "profile_id" not in t


def test_union_provider_returns_list_str_no_profile_id():
    """UnionTermsProvider return type is list[str] with no profile_id key."""
    from app.services.terms_provider import UnionTermsProvider

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"terms": ["AI Consultant", "KI Berater"]}
    mock_resp.raise_for_status = MagicMock()

    provider = UnionTermsProvider(base_url="http://tenant.local")
    with patch("httpx.get", return_value=mock_resp):
        result = provider.get_terms(source_id="adzuna")

    assert isinstance(result, list)
    assert all(isinstance(t, str) for t in result)


def test_union_provider_network_error_returns_empty():
    """UnionTermsProvider returns [] on network error — adapters degrade gracefully."""
    from app.services.terms_provider import UnionTermsProvider

    provider = UnionTermsProvider(base_url="http://tenant.local")
    with patch("httpx.get", side_effect=Exception("connection refused")):
        terms = provider.get_terms()

    assert terms == []


# ---------------------------------------------------------------------------
# Flag selects provider
# ---------------------------------------------------------------------------


def test_resolve_search_terms_default_uses_local():
    """resolve_search_terms() defaults to local provider when no flag is set."""
    from app.services.terms_provider import resolve_search_terms

    with patch("app.services.terms_provider.load_sources_config", return_value=FAKE_SOURCES_CFG):
        terms = resolve_search_terms("adzuna")

    assert "AI Consultant" in terms
    assert isinstance(terms, list)


def test_resolve_search_terms_local_flag_explicit():
    """resolve_search_terms() uses local provider when terms_provider=local."""
    from app.services.terms_provider import resolve_search_terms

    with patch(
        "app.services.terms_provider.load_sources_config",
        return_value=FAKE_SOURCES_CFG_LOCAL_EXPLICIT,
    ):
        terms = resolve_search_terms("indeed")

    assert "Agile Coach" in terms


def test_resolve_search_terms_union_flag_selects_union_provider():
    """resolve_search_terms() selects UnionTermsProvider when terms_provider=union."""
    from app.services.terms_provider import resolve_search_terms, UnionTermsProvider

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"terms": ["AI Consultant"]}
    mock_resp.raise_for_status = MagicMock()

    with patch(
        "app.services.terms_provider.load_sources_config",
        return_value=FAKE_SOURCES_CFG_UNION_PROVIDER,
    ):
        with patch("httpx.get", return_value=mock_resp) as mock_get:
            terms = resolve_search_terms("adzuna")

    # httpx.get was called (union path exercised)
    mock_get.assert_called_once()
    assert "AI Consultant" in terms


def test_resolve_search_terms_union_without_url_falls_back_to_local():
    """If terms_provider=union but no URL configured, falls back to local."""
    from app.services.terms_provider import resolve_search_terms

    cfg_no_url = {**FAKE_SOURCES_CFG, "terms_provider": "union"}
    with patch("app.services.terms_provider.load_sources_config", return_value=cfg_no_url):
        terms = resolve_search_terms("adzuna")

    # Falls back to local -> reads from FAKE_SOURCES_CFG sources.adzuna.search_terms
    assert isinstance(terms, list)
    assert "AI Consultant" in terms


def test_resolve_search_terms_never_raises():
    """resolve_search_terms() swallows all exceptions and returns []."""
    from app.services.terms_provider import resolve_search_terms

    with patch(
        "app.services.terms_provider.load_sources_config",
        side_effect=RuntimeError("yaml exploded"),
    ):
        terms = resolve_search_terms("adzuna")

    assert terms == []
