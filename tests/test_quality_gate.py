"""Unit tests for the profile-free title quality gate (replaces title_gate)."""
from app.data_quality.quality_gate import quality_gate


def test_real_title_kept():
    assert quality_gate("Agile Coach") is True
    # A title the OLD profile would have rejected is now kept — engine-agnostic.
    assert quality_gate("Software Engineer") is True
    assert quality_gate("Heizungsmonteur") is True


def test_empty_or_blank_title_dropped():
    assert quality_gate("") is False
    assert quality_gate("   ") is False
    assert quality_gate(None) is False  # type: ignore[arg-type]


def test_too_short_title_dropped():
    assert quality_gate("a") is False
    assert quality_gate("ab") is True  # >= MIN_TITLE_CHARS


def test_punctuation_only_title_dropped():
    assert quality_gate("--") is False
    assert quality_gate("...") is False
    assert quality_gate("•·") is False
