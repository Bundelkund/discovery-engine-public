"""discover-lever stage: stage registration + DB-union/file-union helpers."""
from pathlib import Path
from unittest.mock import MagicMock

from app.routes.scan import STAGES, _all_lever_slugs, _union_into_file


def test_discover_lever_is_registered_stage():
    assert "discover-lever" in STAGES
    assert "revalidate" in STAGES and "discover" in STAGES


def test_union_into_file_dedups_and_preserves_header(tmp_path: Path):
    p = tmp_path / "lever.txt"
    p.write_text("# header line\nmistral\nqonto\n", encoding="utf-8")
    total = _union_into_file(p, ["qonto", "finn", "valpeo.com"])
    lines = p.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# header line"
    body = [ln for ln in lines if not ln.startswith("#")]
    assert body == ["finn", "mistral", "qonto", "valpeo.com"]  # sorted, deduped
    assert total == 4


def test_union_into_file_creates_when_absent(tmp_path: Path):
    p = tmp_path / "sub" / "lever.txt"
    total = _union_into_file(p, ["finn"])
    assert p.exists()
    assert total == 1
    assert "finn" in p.read_text(encoding="utf-8")


def _mock_supabase(pages: list[list[dict]]):
    """Supabase stub whose .range() returns successive pages."""
    client = MagicMock()
    q = client.table.return_value.select.return_value.eq.return_value
    q.range.return_value.execute.side_effect = [MagicMock(data=p) for p in pages]
    return client


def test_all_lever_slugs_single_page():
    sb = _mock_supabase([[{"slug": "mistral"}, {"slug": "finn"}]])
    assert _all_lever_slugs(sb) == ["mistral", "finn"]


def test_all_lever_slugs_skips_empty():
    sb = _mock_supabase([[{"slug": "mistral"}, {"slug": ""}, {}]])
    assert _all_lever_slugs(sb) == ["mistral"]


def test_all_lever_slugs_empty():
    sb = _mock_supabase([[]])
    assert _all_lever_slugs(sb) == []
