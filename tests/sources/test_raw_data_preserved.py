"""Guard: every source that builds a RawJob must preserve the source's
original response in `raw_data`.

Store-first (raw_jobs) keeps each posting undivided so any field can be
re-extracted later. That promise only holds if EVERY source fills raw_data.
Today adzuna + indeed regressed here; this test stops it recurring on the
next new source.

Static check (parses source files, no network).
"""
from pathlib import Path

import pytest

SOURCES_DIR = Path(__file__).resolve().parents[2] / "app" / "sources"

# Files that legitimately never construct a RawJob.
_SKIP = {"__init__", "base", "db_slugs"}


def _source_files() -> list[Path]:
    return [
        p
        for p in sorted(SOURCES_DIR.glob("*.py"))
        if p.stem not in _SKIP
    ]


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: p.stem)
def test_source_fills_raw_data(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if "RawJob(" not in text:
        pytest.skip(f"{path.stem} builds no RawJob")
    assert "raw_data=" in text, (
        f"{path.stem}.py builds a RawJob but never passes raw_data= "
        f"-> original source payload lost, breaks store-first re-extraction"
    )
