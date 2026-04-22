#!/usr/bin/env python3
"""Download and build the GeoNames DE city subset CSV.

Usage::

    python scripts/build_geonames_subset.py [--output data/geonames-de-subset.csv] [--top 2000]

Downloads DE.zip from GeoNames, parses DE.txt, filters for populated places
(feature_class='P'), and writes the top-N cities (by population) to a CSV.

Fallback: if the download fails the script writes a bundled list of ~50 major
German cities with approximate coordinates so the application can still start.
"""
import argparse
import csv
import io
import logging
import urllib.request
import zipfile
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

GEONAMES_URL = "http://download.geonames.org/export/dump/DE.zip"
MAX_SIZE_BYTES = 100 * 1024 * 1024  # 100 MB hard cap

# Bundled fallback: ~50 major DE cities (approximate lat/lon)
_FALLBACK_CITIES: list[tuple[str, float, float, int]] = [
    ("Berlin", 52.5200, 13.4050, 3426354),
    ("Hamburg", 53.5507, 9.9930, 1845229),
    ("Munich", 48.1374, 11.5755, 1260391),
    ("Cologne", 50.9333, 6.9500, 963395),
    ("Frankfurt am Main", 50.1155, 8.6841, 650000),
    ("Stuttgart", 48.7758, 9.1829, 628000),
    ("Düsseldorf", 51.2217, 6.7762, 612000),
    ("Leipzig", 51.3397, 12.3731, 570000),
    ("Dortmund", 51.5153, 7.4656, 580000),
    ("Essen", 51.4556, 7.0116, 580000),
    ("Bremen", 53.0793, 8.8017, 557000),
    ("Dresden", 51.0509, 13.7383, 550000),
    ("Hannover", 52.3744, 9.7386, 530000),
    ("Nuremberg", 49.4478, 11.0683, 515000),
    ("Duisburg", 51.4349, 6.7623, 496000),
    ("Bochum", 51.4818, 7.2162, 365000),
    ("Wuppertal", 51.2562, 7.1508, 357000),
    ("Bielefeld", 52.0302, 8.5325, 333000),
    ("Bonn", 50.7341, 7.0941, 329000),
    ("Münster", 51.9607, 7.6261, 316000),
    ("Karlsruhe", 49.0069, 8.4037, 308000),
    ("Mannheim", 49.4878, 8.4660, 309000),
    ("Augsburg", 48.3717, 10.8983, 295000),
    ("Wiesbaden", 50.0782, 8.2398, 277000),
    ("Gelsenkirchen", 51.5177, 7.0857, 265000),
    ("Mönchengladbach", 51.1805, 6.4428, 261000),
    ("Braunschweig", 52.2689, 10.5268, 249000),
    ("Chemnitz", 50.8278, 12.9214, 247000),
    ("Kiel", 54.3233, 10.1394, 247000),
    ("Aachen", 50.7762, 6.0838, 245000),
    ("Halle", 51.4825, 11.9675, 238000),
    ("Magdeburg", 52.1317, 11.6392, 237000),
    ("Freiburg im Breisgau", 47.9990, 7.8421, 229000),
    ("Krefeld", 51.3388, 6.5853, 227000),
    ("Lübeck", 53.8655, 10.6866, 216000),
    ("Oberhausen", 51.4699, 6.8522, 211000),
    ("Erfurt", 50.9777, 11.0290, 212000),
    ("Mainz", 49.9929, 8.2473, 213000),
    ("Rostock", 54.0887, 12.1400, 209000),
    ("Kassel", 51.3127, 9.4797, 202000),
    ("Hagen", 51.3671, 7.4633, 189000),
    ("Potsdam", 52.3906, 13.0645, 180000),
    ("Saarbrücken", 49.2402, 6.9969, 180000),
    ("Hamm", 51.6739, 7.8152, 179000),
    ("Ludwigshafen am Rhein", 49.4814, 8.4352, 172000),
    ("Mülheim an der Ruhr", 51.4296, 6.8825, 170000),
    ("Oldenburg", 53.1434, 8.2146, 168000),
    ("Osnabrück", 52.2799, 8.0472, 165000),
    ("Leverkusen", 51.0459, 6.9946, 163000),
    ("Heidelberg", 49.4094, 8.6942, 158000),
]


def _parse_de_txt(content: str, top: int) -> list[tuple[str, float, float, int, str]]:
    """Parse GeoNames DE.txt and return top-N cities by population.

    Returns tuples of (name, lat, lon, population, alternate_names_csv).
    """
    cities: list[tuple[str, float, float, int, str]] = []
    for line in content.strip().split("\n"):
        parts = line.split("\t")
        if len(parts) < 15:
            continue
        if parts[6] != "P":  # feature_class
            continue
        name = parts[1]
        alt_names = parts[3] if len(parts) > 3 else ""
        try:
            lat = float(parts[4])
            lon = float(parts[5])
            pop = int(parts[14]) if parts[14] else 0
        except ValueError:
            continue
        cities.append((name, lat, lon, pop, alt_names))
    cities.sort(key=lambda x: x[3], reverse=True)
    return cities[:top]


def _download(url: str, max_bytes: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "GeoNamesFetcher/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(
            f"Download exceeded {max_bytes // 1024 // 1024} MB cap ({len(data)} bytes)"
        )
    return data


def _write_csv(cities: list[tuple], output: Path) -> None:
    """Write city CSV with columns name, lat, lon, population, alternate_names."""
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["name", "lat", "lon", "population", "alternate_names"])
        for row in cities:
            if len(row) == 5:
                name, lat, lon, pop, alt = row
            else:
                name, lat, lon, pop = row[:4]
                alt = ""
            writer.writerow([name, lat, lon, pop, alt])


def build(output: Path, top: int = 2000) -> dict:
    """Download, parse, and write the GeoNames subset.

    Returns a status dict with ``source``, ``city_count``, ``file_size_kb``.
    """
    source = "download"
    cities: list[tuple[str, float, float, int]] = []

    try:
        logger.info("Downloading %s ...", GEONAMES_URL)
        raw = _download(GEONAMES_URL, MAX_SIZE_BYTES)
        logger.info("Downloaded %.1f MB", len(raw) / 1024 / 1024)

        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            with z.open("DE.txt") as f:
                content = f.read().decode("utf-8")

        cities = _parse_de_txt(content, top)
        logger.info("Parsed %d populated places (top %d by population)", len(cities), top)

    except Exception as exc:  # noqa: BLE001
        logger.warning("GeoNames download failed (%s) — using bundled fallback", exc)
        source = "bundled_fallback"
        # Add empty alternate_names for fallback entries
        cities = sorted(
            [(n, la, lo, p, "") for n, la, lo, p in _FALLBACK_CITIES],
            key=lambda x: x[3],
            reverse=True,
        )[:top]

    _write_csv(cities, output)
    size_kb = output.stat().st_size / 1024
    logger.info("Wrote %d cities to %s (%.1f KB)", len(cities), output, size_kb)

    return {
        "source": source,
        "city_count": len(cities),
        "file_size_kb": round(size_kb, 1),
        "output": str(output),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/geonames-de-subset.csv"),
        help="Output CSV path (default: data/geonames-de-subset.csv)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=2000,
        help="Number of top cities to include (default: 2000)",
    )
    args = parser.parse_args()
    result = build(args.output, args.top)
    print(f"Done: {result}")


if __name__ == "__main__":
    main()
