"""Integration tests: flag-only mode → kept; post-activation → rejected."""
from datetime import date, datetime, timezone


from app.data_quality.rules import RulesEngine

_CONFIG = {
    "flag": ["boilerplate_short"],
    "reject": ["no_title"],
}

_JOB_WITH_VIOLATIONS = {
    "title": "",          # triggers no_title (reject rule)
    "url": "https://example.com/job/bad",
    "description": "Short",  # triggers boilerplate_short (flag rule)
}

_JOB_CLEAN = {
    "title": "Software Engineer",
    "url": "https://example.com/job/good",
    "description": "A" * 300,
}


def test_job_with_violations_kept_in_flag_only_mode() -> None:
    """Before activation: job with rule violations must be KEPT (only flagged)."""
    engine = RulesEngine(_CONFIG, activation_date=date(2099, 1, 1))
    verdict, flags = engine.classify(_JOB_WITH_VIOLATIONS)
    assert verdict == "keep"
    assert "no_title" in flags
    assert "boilerplate_short" in flags


def test_job_with_violations_rejected_after_activation() -> None:
    """After activation date: job violating reject rule must be REJECTED."""
    activation = date(2020, 1, 1)
    now_fn = lambda: datetime(2024, 6, 1, tzinfo=timezone.utc)  # noqa: E731
    engine = RulesEngine(_CONFIG, activation_date=activation, now_fn=now_fn)
    verdict, flags = engine.classify(_JOB_WITH_VIOLATIONS)
    assert verdict == "reject"
    assert "no_title" in flags


def test_clean_job_always_kept() -> None:
    """A job without violations is always kept regardless of activation."""
    # Before
    engine_before = RulesEngine(_CONFIG, activation_date=date(2099, 1, 1))
    verdict, flags = engine_before.classify(_JOB_CLEAN)
    assert verdict == "keep"
    assert flags == {}

    # After activation
    activation = date(2020, 1, 1)
    now_fn = lambda: datetime(2024, 6, 1, tzinfo=timezone.utc)  # noqa: E731
    engine_after = RulesEngine(_CONFIG, activation_date=activation, now_fn=now_fn)
    verdict2, flags2 = engine_after.classify(_JOB_CLEAN)
    assert verdict2 == "keep"
    assert flags2 == {}
