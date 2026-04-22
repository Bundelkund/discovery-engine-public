"""Tests for RulesEngine.classify() — basic cases."""
from datetime import date

import pytest

from app.data_quality.rules import RulesEngine


# A past activation date means reject IS active
_PAST_DATE = date(2000, 1, 1)
# A future activation date means flag-only
_FUTURE_DATE = date(2099, 1, 1)

_CONFIG = {
    "flag": ["boilerplate_short", "no_description"],
    "reject": ["no_title", "no_url"],
}


@pytest.fixture()
def engine_reject_active():
    return RulesEngine(_CONFIG, activation_date=_PAST_DATE)


@pytest.fixture()
def engine_flag_only():
    return RulesEngine(_CONFIG, activation_date=_FUTURE_DATE)


# ---------------------------------------------------------------------------
# Short description
# ---------------------------------------------------------------------------


def test_classify_short_description_flagged(engine_reject_active: RulesEngine) -> None:
    job = {
        "title": "Software Engineer",
        "url": "https://example.com/job/1",
        "description": "Short desc.",  # < 200 chars
    }
    verdict, flags = engine_reject_active.classify(job)
    assert verdict == "keep"  # short desc is flag, not reject
    assert "boilerplate_short" in flags


def test_classify_no_description_flagged(engine_reject_active: RulesEngine) -> None:
    job = {
        "title": "Software Engineer",
        "url": "https://example.com/job/2",
        "description": "",
    }
    verdict, flags = engine_reject_active.classify(job)
    assert verdict == "keep"
    assert "no_description" in flags


# ---------------------------------------------------------------------------
# Missing title → reject
# ---------------------------------------------------------------------------


def test_classify_no_title_rejected(engine_reject_active: RulesEngine) -> None:
    job = {
        "title": "",
        "url": "https://example.com/job/3",
        "description": "A" * 300,
    }
    verdict, flags = engine_reject_active.classify(job)
    assert verdict == "reject"
    assert "no_title" in flags


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_classify_happy_path(engine_reject_active: RulesEngine) -> None:
    job = {
        "title": "Senior Python Engineer",
        "url": "https://example.com/job/4",
        "description": "A" * 300,
    }
    verdict, flags = engine_reject_active.classify(job)
    assert verdict == "keep"
    assert flags == {}
