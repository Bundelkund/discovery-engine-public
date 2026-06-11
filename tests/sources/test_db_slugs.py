"""Unit tests for the DB-driven slug source (T5 / db-driven-slugs)."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.sources import db_slugs


def _mock_client(slug_rows):
    """Build a chained MagicMock supabase client returning slug_rows."""
    client = MagicMock()
    # query chain: select -> eq(source) -> eq(status) -> eq(monitor) -> in_(de_flag) -> execute
    chain = (
        client.table.return_value.select.return_value
        .eq.return_value.eq.return_value.eq.return_value
        .in_.return_value
    )
    chain.execute.return_value = SimpleNamespace(data=slug_rows)
    return client


def _settings(url="https://x.supabase.co", key="anon"):
    return SimpleNamespace(supabase_url=url, supabase_key=key)


def test_load_active_slugs_returns_db_slugs():
    client = _mock_client([{"slug": "acme"}, {"slug": "globex"}])
    with (
        patch("app.config.get_settings", return_value=_settings()),
        patch("supabase.create_client", return_value=client),
    ):
        assert db_slugs.load_active_slugs("personio") == ["acme", "globex"]


def test_load_active_slugs_skips_empty_slugs():
    client = _mock_client([{"slug": "acme"}, {"slug": ""}, {"other": "x"}])
    with (
        patch("app.config.get_settings", return_value=_settings()),
        patch("supabase.create_client", return_value=client),
    ):
        assert db_slugs.load_active_slugs("lever") == ["acme"]


def test_load_active_slugs_empty_creds_returns_empty():
    with patch("app.config.get_settings", return_value=_settings(url="", key="")):
        assert db_slugs.load_active_slugs("personio") == []


def test_load_active_slugs_db_error_returns_empty():
    with (
        patch("app.config.get_settings", return_value=_settings()),
        patch("supabase.create_client", side_effect=RuntimeError("boom")),
    ):
        assert db_slugs.load_active_slugs("personio") == []


def test_merge_slugs_unions_and_dedups():
    with patch.object(db_slugs, "load_active_slugs", return_value=["acme", "vercel"]):
        # 'vercel' is in both -> appears once, yaml order first.
        assert db_slugs.merge_slugs(["vercel", "anthropic"], "greenhouse") == [
            "vercel",
            "anthropic",
            "acme",
        ]


def test_merge_slugs_db_error_yields_yaml_only():
    with patch.object(db_slugs, "load_active_slugs", return_value=[]):
        assert db_slugs.merge_slugs(["vercel"], "greenhouse") == ["vercel"]
