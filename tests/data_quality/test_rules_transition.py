"""Tests for is_reject_active edge cases and time-based transition."""
from datetime import date, datetime, timezone


from app.data_quality.rules import RulesEngine, is_reject_active

# ---------------------------------------------------------------------------
# is_reject_active unit tests
# ---------------------------------------------------------------------------


def test_reject_active_after_activation() -> None:
    activation = date(2020, 6, 15)
    now = datetime(2020, 6, 15, 12, 0, tzinfo=timezone.utc)
    assert is_reject_active(now, activation) is True


def test_reject_active_day_of() -> None:
    activation = date(2024, 3, 1)
    now = datetime(2024, 3, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert is_reject_active(now, activation) is True


def test_reject_not_active_before_activation() -> None:
    activation = date(2030, 1, 1)
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert is_reject_active(now, activation) is False


def test_reject_active_one_second_before_midnight() -> None:
    """Still before activation if activation hasn't hit yet."""
    activation = date(2024, 6, 20)
    # One day before activation
    now = datetime(2024, 6, 19, 23, 59, 59, tzinfo=timezone.utc)
    assert is_reject_active(now, activation) is False


# ---------------------------------------------------------------------------
# RulesEngine with mocked time
# ---------------------------------------------------------------------------

_CONFIG = {
    "flag": [],
    "reject": ["no_title"],
}


def test_engine_rejects_after_activation_date() -> None:
    activation = date(2024, 1, 10)
    # Mock time: after activation
    now_fn = lambda: datetime(2024, 1, 11, tzinfo=timezone.utc)  # noqa: E731
    engine = RulesEngine(_CONFIG, activation_date=activation, now_fn=now_fn)

    job = {"title": "", "url": "https://example.com/1", "description": "A" * 300}
    verdict, flags = engine.classify(job)
    assert verdict == "reject"
    assert engine.mode == "flag+reject"


def test_engine_keeps_before_activation_date() -> None:
    activation = date(2099, 1, 10)
    engine = RulesEngine(_CONFIG, activation_date=activation)

    job = {"title": "", "url": "https://example.com/2", "description": "A" * 300}
    verdict, _ = engine.classify(job)
    assert verdict == "keep"
    assert engine.mode == "flag-only"


def test_engine_mode_no_activation_is_flag_only() -> None:
    engine = RulesEngine(_CONFIG, activation_date=None)
    assert engine.mode == "flag-only"
