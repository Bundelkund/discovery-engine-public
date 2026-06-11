#!/usr/bin/env python3
"""Export de_flag-null ats_companies slugs per provider -> scripts/out/null-<source>.txt.

P5 recovery helper: the null rows are gated out of monitoring (de_flag IS NULL = the
feed carried no location at scan time). To recompute their de_flag they must be re-fed
through the scanner, but the normal --revalidate hydrate only pulls monitor=true slugs.
This writes slug-files the scanner can consume via --slugs-file (monitor-gate bypass):

  python scripts/export_null_slugs.py [--ats personio]
  python scripts/ats_scanner.py --ats personio --slugs-file scripts/out/null-personio.txt
  python scripts/seed_ats_companies.py --ats personio

Reads SUPABASE_URL/SUPABASE_KEY from .env (same loader as seed_ats_companies).
"""
from __future__ import annotations

import argparse
from pathlib import Path

import httpx

from seed_ats_companies import OUT, load_env  # reuse env loader + out dir

TABLE = "ats_companies"


def fetch_null(client: httpx.Client, base: str, hdr: dict, ats: str | None) -> dict[str, list[str]]:
    by: dict[str, list[str]] = {}
    off = 0
    params = {"select": "source,slug", "de_flag": "is.null", "limit": 1000}
    if ats:
        params["source"] = f"eq.{ats}"
    while True:
        r = client.get(f"{base}/{TABLE}", headers=hdr, params={**params, "offset": off})
        r.raise_for_status()
        batch = r.json()
        for row in batch:
            if row.get("slug"):
                by.setdefault(row["source"], []).append(row["slug"])
        if len(batch) < 1000:
            return by
        off += 1000


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ats", help="single provider (default: all)")
    args = ap.parse_args()

    url, key = load_env()
    base = f"{url}/rest/v1"
    hdr = {"apikey": key, "Authorization": f"Bearer {key}"}
    OUT.mkdir(parents=True, exist_ok=True)

    with httpx.Client(timeout=60) as client:
        by = fetch_null(client, base, hdr, args.ats)

    for source, slugs in sorted(by.items()):
        path = OUT / f"null-{source}.txt"
        path.write_text("\n".join(sorted(slugs)) + "\n", encoding="utf-8")
        print(f"{source}: {len(slugs)} null slugs -> {path}")
    if not by:
        print("no null slugs found")


if __name__ == "__main__":
    main()
