#!/usr/bin/env python3
"""Seed/refresh public.ats_companies from ATS-scanner enumeration JSON.

No-delete registry: every slug ever discovered keeps a row. Idempotent upsert on
(ats, slug). `initial_job_count` + `discovered_at` are set once (on first insert)
and never overwritten on re-run; everything else (status, last_job_count, de_flag,
sample_titles, seen_in_crawls, last_checked_at) reflects the latest validate pass.

Source = enumeration JSON `all_validations` (full universe incl. inactive boards).
Reads SUPABASE_URL + SUPABASE_KEY from discovery-engine/.env.

Usage:
  python scripts/seed_ats_companies.py                 # all *-enumeration.json in out/
  python scripts/seed_ats_companies.py --ats personio  # one provider
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "scripts" / "out"
TABLE = "ats_companies"
CHUNK = 500
NOW_ISO = datetime.now(timezone.utc).isoformat()


def load_env() -> tuple[str, str]:
    env = ROOT / ".env"
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k == "SUPABASE_URL" and not url:
                url = v
            elif k == "SUPABASE_KEY" and not key:
                key = v
    if not url or not key:
        sys.exit("ERROR: SUPABASE_URL / SUPABASE_KEY not set (.env or env)")
    return url.rstrip("/"), key


def status_of(v: dict) -> str:
    if v.get("active"):
        return "active"
    return "dead" if v.get("error") else "paused"  # error=404 dead; empty feed paused


def row_from(ats: str, v: dict, crawls: list[str], source: str = "cc") -> dict:
    st = status_of(v)
    jc = v.get("job_count") or 0
    return {
        "ats": ats,
        "slug": v["slug"],
        "feed_url": v.get("feed_url"),
        "source": source,
        "seen_in_crawls": crawls,
        "status": st,
        "monitor": st != "dead",  # don't daily-poll 404s; Stage A can revive
        "last_job_count": jc,
        "de_flag": v.get("de_flag"),
        "sample_titles": v.get("sample_titles") or [],
        "last_checked_at": NOW_ISO,
    }


def existing_keys(client: httpx.Client, base: str, hdr: dict, ats: str) -> set[str]:
    seen: set[str] = set()
    off = 0
    while True:
        r = client.get(
            f"{base}/{TABLE}",
            headers=hdr,
            params={"select": "slug", "ats": f"eq.{ats}", "limit": 1000, "offset": off},
        )
        r.raise_for_status()
        batch = r.json()
        seen.update(x["slug"] for x in batch)
        if len(batch) < 1000:
            return seen
        off += 1000


def upsert(client: httpx.Client, base: str, hdr: dict, rows: list[dict]) -> None:
    h = dict(hdr)
    h["Prefer"] = "resolution=merge-duplicates,return=minimal"
    for i in range(0, len(rows), CHUNK):
        chunk = rows[i : i + CHUNK]
        resp = client.post(
            f"{base}/{TABLE}",
            headers=h,
            params={"on_conflict": "ats,slug"},
            content=json.dumps(chunk),
        )
        if resp.status_code >= 300:
            sys.exit(f"  upsert FAIL {resp.status_code}: {resp.text[:300]}")
        print(f"  upserted {min(i + CHUNK, len(rows))}/{len(rows)}")


def seed_ats(client, base, hdr, path: Path) -> tuple[int, int]:
    d = json.loads(path.read_text(encoding="utf-8"))
    ats = d["ats"]
    crawls = d.get("crawls") or []
    source = d.get("source") or "cc"  # curated lists tag source; CC JSONs omit it
    vals = d.get("all_validations") or []
    if not vals:
        print(f"{ats}: no all_validations, skip")
        return 0, 0
    have = existing_keys(client, base, hdr, ats)
    new_rows, upd_rows = [], []
    for v in vals:
        if not v.get("slug"):
            continue
        r = row_from(ats, v, crawls, source)
        if v["slug"] in have:
            upd_rows.append(r)  # preserve initial_job_count + discovered_at -> omit them
        else:
            r["initial_job_count"] = v.get("job_count") or 0
            new_rows.append(r)
    print(f"{ats}: {len(vals)} slugs -> {len(new_rows)} new + {len(upd_rows)} update")
    if new_rows:
        upsert(client, base, hdr, new_rows)
    if upd_rows:
        upsert(client, base, hdr, upd_rows)
    return len(new_rows), len(upd_rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ats", help="single provider (default: all enumeration JSON)")
    args = ap.parse_args()

    url, key = load_env()
    base = f"{url}/rest/v1"
    hdr = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    if args.ats:
        files = [OUT / f"{args.ats}-enumeration.json"]
    else:
        files = sorted(Path(p) for p in glob.glob(str(OUT / "*-enumeration.json")))
    files = [f for f in files if f.exists()]
    if not files:
        sys.exit("ERROR: no enumeration JSON found in scripts/out/")

    tot_new = tot_upd = 0
    with httpx.Client(timeout=60) as client:
        for f in files:
            n, u = seed_ats(client, base, hdr, f)
            tot_new += n
            tot_upd += u
    print(f"\ndone: {tot_new} inserted, {tot_upd} updated across {len(files)} ATS")


if __name__ == "__main__":
    main()
