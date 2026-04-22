"""Tests for flag-only mode: violations are flagged but job is kept."""
from datetime import date

import pytest

from app.data_quality.rules import RulesEngine

_FUTURE_DATE = date(2099, 1, 1)

_CONFIG = {
    "flag": ["boilerplate_short"],
    "reject": ["no_title"],
}


@pytest.fixture()
def engine():
    return RulesEngine(_CONFIG, activation_date=_FUTURE_DATE)


def test_short_description_kept_with_flag(engine: RulesEngine) -> None:
    """Short description → flagged but NOT rejected in flag-only mode."""
    job = {
        "title": "Frontend Developer",
        "url": "https://example.com/job/1",
        "description": "Short.",
    }
    verdict, flags = engine.classify(job)
    assert verdict == "keep"
    assert "boilerplate_short" in flags


def test_no_title_kept_in_flag_only(engine: RulesEngine) -> None:
    """Reject-rule violation → verdict is still keep when activation is in future."""
    job = {
        "title": "",
        "url": "https://example.com/job/2",
        "description": "A" * 300,
    }
    verdict, flags = engine.classify(job)
    assert verdict == "keep"
    assert "no_title" in flags


def test_mode_is_flag_only(engine: RulesEngine) -> None:
    assert engine.mode == "flag-only"
