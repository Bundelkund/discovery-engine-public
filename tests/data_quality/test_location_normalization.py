"""Tests for LocationNormalizer using the real GeoNames CSV."""
import math
from pathlib import Path

import pytest

from app.data_quality.location import LocationNormalizer

# Use the real CSV bundled in the repo
_CSV_PATH = (
    Path(__file__).parent.parent.parent / "data" / "geonames-de-subset.csv"
)


@pytest.fixture(scope="module")
def normalizer():
    if not _CSV_PATH.exists():
        pytest.skip("geonames-de-subset.csv not found")
    return LocationNormalizer(_CSV_PATH)


# ---------------------------------------------------------------------------
# City lookup
# ---------------------------------------------------------------------------


def test_berlin_mitte_resolves_to_berlin(normalizer: LocationNormalizer) -> None:
    result = normalizer.normalize("Berlin-Mitte")
    assert result["location_normalized"] == "Berlin"
    assert result["location_lat"] is not None
    assert math.isclose(result["location_lat"], 52.52, abs_tol=0.5)


def test_munich_with_hybrid_parens(normalizer: LocationNormalizer) -> None:
    result = normalizer.normalize("München (Hybrid)")
    # Name in CSV could be "Munich" or "München" depending on GeoNames record
    assert result["location_normalized"] in ("Munich", "München")
    assert result["is_hybrid"] is True
    assert result["location_lat"] is not None
    assert math.isclose(result["location_lat"], 48.13, abs_tol=0.5)


def test_unknown_city_returns_null(normalizer: LocationNormalizer) -> None:
    result = normalizer.normalize("Springfield")
    assert result["location_normalized"] is None
    assert result["location_lat"] is None
    assert result["location_lon"] is None


# ---------------------------------------------------------------------------
# Remote detection
# ---------------------------------------------------------------------------


def test_remote_is_detected(normalizer: LocationNormalizer) -> None:
    result = normalizer.normalize("Remote")
    assert result["is_remote"] is True


def test_homeoffice_is_remote(normalizer: LocationNormalizer) -> None:
    result = normalizer.normalize("Homeoffice")
    assert result["is_remote"] is True


def test_fully_remote_is_remote(normalizer: LocationNormalizer) -> None:
    result = normalizer.normalize("Fully Remote")
    assert result["is_remote"] is True


def test_work_from_home_is_remote(normalizer: LocationNormalizer) -> None:
    result = normalizer.normalize("Work from Home")
    assert result["is_remote"] is True


# ---------------------------------------------------------------------------
# Hybrid detection
# ---------------------------------------------------------------------------


def test_hybrid_keyword_detected(normalizer: LocationNormalizer) -> None:
    result = normalizer.normalize("Hybrid")
    assert result["is_hybrid"] is True


def test_hybrid_case_insensitive(normalizer: LocationNormalizer) -> None:
    result = normalizer.normalize("HYBRID")
    assert result["is_hybrid"] is True


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_string_returns_nulls(normalizer: LocationNormalizer) -> None:
    result = normalizer.normalize("")
    assert result["location_normalized"] is None
    assert result["is_remote"] is False
    assert result["is_hybrid"] is False


def test_non_remote_city_not_flagged_remote(normalizer: LocationNormalizer) -> None:
    result = normalizer.normalize("Hamburg")
    assert result["is_remote"] is False
    assert result["is_hybrid"] is False
