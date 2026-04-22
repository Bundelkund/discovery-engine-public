"""GeoNames-based location normalizer for German job listings."""
import csv
import logging
import re
import unicodedata
from pathlib import Path
from typing import TypedDict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type definitions
# ---------------------------------------------------------------------------


class LocationResult(TypedDict):
    location_normalized: str | None
    location_lat: float | None
    location_lon: float | None
    is_remote: bool
    is_hybrid: bool


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REMOTE_WHITELIST: frozenset[str] = frozenset(
    {
        "remote",
        "homeoffice",
        "home office",
        "fully remote",
        "anywhere",
        "work from home",
    }
)

_HYBRID_KEYWORD = "hybrid"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unaccent(text: str) -> str:
    """Normalize unicode: decompose + strip combining marks + lowercase."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def _strip_noise(text: str) -> str:
    """Remove common parenthetical modifiers and extra whitespace."""
    text = re.sub(r"\([^)]*\)", " ", text)  # remove parentheses
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------


class LocationNormalizer:
    """Normalize job location strings using a GeoNames city database.

    Args:
        csv_path: Path to ``geonames-de-subset.csv`` with columns
                  ``name,lat,lon,population``.
    """

    def __init__(self, csv_path: str | Path) -> None:
        csv_path = Path(csv_path)
        self._index: dict[str, tuple[str, float, float]] = {}  # key -> (name, lat, lon)
        self._loaded = False
        self._load(csv_path)

    def _load(self, csv_path: Path) -> None:
        if not csv_path.exists():
            logger.warning(
                "GeoNames CSV not found — location normalizer will return nulls",
                extra={"csv_path": str(csv_path)},
            )
            return

        count = 0
        try:
            with open(csv_path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    name: str = row["name"].strip()
                    try:
                        lat = float(row["lat"])
                        lon = float(row["lon"])
                    except (KeyError, ValueError):
                        continue

                    canonical = (name, lat, lon)
                    # Index canonical name
                    self._index[_unaccent(name)] = canonical

                    # Index alternate names when present (comma-separated).
                    # Skip compound names (containing hyphens or spaces) to
                    # avoid false matches like "berlin-mitte" → Mitte borough.
                    alt_raw = row.get("alternate_names", "")
                    if alt_raw:
                        for alt in alt_raw.split(","):
                            alt = alt.strip()
                            if not alt:
                                continue
                            # Skip IATA codes, numeric strings, compound names
                            if len(alt) <= 3 and alt.isupper():
                                continue  # skip airport codes like MUC, BER
                            if "-" in alt or " " in alt:
                                continue  # skip compound entries
                            alt_key = _unaccent(alt)
                            # Only store if not already claimed by another city
                            if alt_key not in self._index:
                                self._index[alt_key] = canonical

                    count += 1
        except Exception as exc:
            logger.error(
                "Failed to load GeoNames CSV",
                extra={"csv_path": str(csv_path), "error": str(exc)},
            )
            raise

        self._loaded = count > 0
        logger.info(
            "GeoNames CSV loaded",
            extra={"city_count": count, "csv_path": str(csv_path)},
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_loaded(self) -> bool:
        """True when the city database was loaded successfully."""
        return self._loaded

    def normalize(self, location: str) -> LocationResult:
        """Normalize a raw location string.

        Returns a ``LocationResult`` dict with:
        - ``location_normalized``: canonical city name (or None)
        - ``location_lat`` / ``location_lon``: coordinates (or None)
        - ``is_remote``: True for remote-work keywords
        - ``is_hybrid``: True for hybrid-work keywords
        """
        result: LocationResult = {
            "location_normalized": None,
            "location_lat": None,
            "location_lon": None,
            "is_remote": False,
            "is_hybrid": False,
        }

        if not location:
            return result

        lower = location.lower().strip()

        # --- Remote detection ---
        if any(keyword in lower for keyword in _REMOTE_WHITELIST):
            result["is_remote"] = True

        # --- Hybrid detection ---
        if _HYBRID_KEYWORD in lower:
            result["is_hybrid"] = True

        # --- City lookup ---
        if self._loaded:
            # Strip noise and try multiple candidate strings
            cleaned = _strip_noise(location)
            candidates: list[str] = [cleaned]

            # Also try splitting on "/" "," "-" to handle "Berlin-Mitte"
            for sep in ("-", "/", ",", "|"):
                if sep in cleaned:
                    candidates.extend(p.strip() for p in cleaned.split(sep))

            for candidate in candidates:
                key = _unaccent(candidate)
                if key in self._index:
                    name, lat, lon = self._index[key]
                    result["location_normalized"] = name
                    result["location_lat"] = lat
                    result["location_lon"] = lon
                    break

        return result
