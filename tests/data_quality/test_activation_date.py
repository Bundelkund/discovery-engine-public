"""Tests for compute_activation_date."""
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.data_quality.rules import compute_activation_date

_CONFIG = {"grace_period_days": 7}
_TODAY = date(2024, 6, 1)


# ---------------------------------------------------------------------------
# File missing → computes and writes
# ---------------------------------------------------------------------------


def test_file_missing_computes_and_writes(tmp_path: Path) -> None:
    activation_file = tmp_path / "activation.txt"
    assert not activation_file.exists()

    result = compute_activation_date(_CONFIG, _TODAY, activation_file)

    expected = _TODAY + timedelta(days=7)
    assert result == expected
    assert activation_file.exists()
    assert activation_file.read_text().strip() == expected.isoformat()


def test_file_missing_custom_grace_period(tmp_path: Path) -> None:
    activation_file = tmp_path / "activation.txt"
    cfg = {"grace_period_days": 14}
    result = compute_activation_date(cfg, _TODAY, activation_file)
    assert result == _TODAY + timedelta(days=14)


# ---------------------------------------------------------------------------
# File exists → reads and returns
# ---------------------------------------------------------------------------


def test_file_exists_reads_stored_date(tmp_path: Path) -> None:
    activation_file = tmp_path / "activation.txt"
    stored_date = date(2025, 3, 15)
    activation_file.write_text(stored_date.isoformat())

    result = compute_activation_date(_CONFIG, _TODAY, activation_file)
    assert result == stored_date


def test_file_exists_does_not_overwrite(tmp_path: Path) -> None:
    """When file already exists the grace_period_days must be ignored."""
    activation_file = tmp_path / "activation.txt"
    stored_date = date(2025, 3, 15)
    activation_file.write_text(stored_date.isoformat())

    # Even with different grace period, should return stored date
    cfg = {"grace_period_days": 999}
    result = compute_activation_date(cfg, _TODAY, activation_file)
    assert result == stored_date


# ---------------------------------------------------------------------------
# File corrupt → ValueError
# ---------------------------------------------------------------------------


def test_file_corrupt_raises_value_error(tmp_path: Path) -> None:
    activation_file = tmp_path / "activation.txt"
    activation_file.write_text("NOT-A-DATE")

    with pytest.raises(ValueError, match="invalid ISO date"):
        compute_activation_date(_CONFIG, _TODAY, activation_file)


def test_file_corrupt_empty_raises_value_error(tmp_path: Path) -> None:
    activation_file = tmp_path / "activation.txt"
    activation_file.write_text("")

    with pytest.raises(ValueError):
        compute_activation_date(_CONFIG, _TODAY, activation_file)


def test_file_corrupt_partial_date_raises(tmp_path: Path) -> None:
    activation_file = tmp_path / "activation.txt"
    activation_file.write_text("2024-13-99")  # invalid month/day

    with pytest.raises(ValueError):
        compute_activation_date(_CONFIG, _TODAY, activation_file)
