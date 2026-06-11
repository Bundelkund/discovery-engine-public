#!/usr/bin/env python3
"""Recover de_flag for factorial companies whose sitemap feed carries no location.

factorial's feed is a sitemap.xml of /job_posting/ URLs -> the normal validate pass
counts jobs but extracts no location, so de_flag stays NULL even for active boards
(prov["loc"] is None). This backfills de_flag by reading location from two signals
the sitemap path ignores:

  1. the job-URL slug itself (often ends in a city, e.g. ...-munich-270357)  -- free
  2. the job page's location line, rendered after a 'location-pin' glyph        -- sampled

Both run through ats_scanner._cat_str/_fold (same DE-city gazetteer the other
providers use). Folded per company: any 'de' job -> de; else remote/foreign.
Companies whose sampled jobs expose no place stay NULL (unrecoverable).

Then PATCHes ats_companies.de_flag + monitor (= de_flag IN de/remote) directly.

  python scripts/recover_factorial_de.py [--limit N] [--samples 5] [--workers 8]

Reads SUPABASE_URL/SUPABASE_KEY from .env (same loader as seed_ats_companies).
"""
from __future__ import annotations

import argparse
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

from ats_scanner import _cat_str, _fold  # reuse DE-city classifier + fold
from seed_ats_companies import load_env

TABLE = "ats_companies"
FEED = "https://{slug}.factorialhr.com/sitemap.xml"
PIN = "\U0001f4cd"  # location-pin glyph that precedes the rendered job location
UA = {"User-Agent": "Mozilla/5.0"}


def _url_loc(job_url: str) -> str:
    """Last URL segment minus trailing numeric id -> '... munich' text for _cat_str."""
    seg = job_url.rstrip("/").split("/job_posting/")[-1]
    return re.sub(r"-\d+$", "", seg).replace("-", " ")


def _pin_loc(html: str) -> str | None:
    """Text right after the location pin, tags stripped -> e.g. 'Munich (Hybrid)'."""
    i = html.find(PIN)
    if i < 0:
        return None
    seg = re.sub(r"<[^>]+>", " ", html[i + len(PIN): i + 260])
    seg = re.sub(r"\s+", " ", seg).strip()
    return seg.split("·")[0].split("|")[0].strip()  # cut at middot / pipe


def classify(slug: str, client: httpx.Client, samples: int) -> str | None:
    try:
        sm = client.get(FEED.format(slug=slug), timeout=20, follow_redirects=True)
        if sm.status_code != 200:
            return None
        jobs = [u for u in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", sm.text) if "/job_posting/" in u]
        if not jobs:
            return None
        cats = [_cat_str(_url_loc(u)) for u in jobs]               # free URL signal, all jobs
        for u in jobs[:samples]:                                   # page-confirm a sample
            try:
                pg = client.get(u, timeout=20, follow_redirects=True)
                loc = _pin_loc(pg.text) if pg.status_code == 200 else None
                if loc:
                    cats.append(_cat_str(loc))
            except Exception:  # noqa: BLE001
                continue
        return _fold(cats)
    except Exception:  # noqa: BLE001
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="cap companies (0=all null+active)")
    ap.add_argument("--samples", type=int, default=5, help="job pages fetched per company")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    url, key = load_env()
    base = f"{url}/rest/v1"
    hdr = {"apikey": key, "Authorization": f"Bearer {key}"}

    with httpx.Client(headers={**hdr, **UA}) as client:
        r = client.get(f"{base}/{TABLE}", params={
            "select": "slug", "source": "eq.factorial", "de_flag": "is.null",
            "status": "eq.active", "limit": args.limit or 100000})
        r.raise_for_status()
        slugs = [row["slug"] for row in r.json() if row.get("slug")]
    print(f"factorial null+active: {len(slugs)} companies, {args.samples} pages each")

    results: dict[str, str | None] = {}
    with httpx.Client(headers=UA) as fetch:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(classify, s, fetch, args.samples): s for s in slugs}
            done = 0
            for fut in as_completed(futs):
                results[futs[fut]] = fut.result()
                done += 1
                if done % 25 == 0:
                    print(f"  {done}/{len(slugs)}")

    counts = {"de": 0, "remote": 0, "foreign": 0, "null": 0}
    with httpx.Client(headers={**hdr, "Content-Type": "application/json",
                               "Prefer": "return=minimal"}) as patch:
        for slug, flag in results.items():
            counts["null" if flag is None else flag] += 1
            if flag is None:
                continue
            body = {"de_flag": flag, "monitor": flag in ("de", "remote")}
            resp = patch.patch(f"{base}/{TABLE}", params={
                "source": "eq.factorial", "slug": f"eq.{slug}"}, json=body)
            if resp.status_code >= 300:
                print(f"  PATCH {slug} FAIL {resp.status_code}: {resp.text[:120]}", file=sys.stderr)

    print(f"\ndone: de={counts['de']} remote={counts['remote']} "
          f"foreign={counts['foreign']} still-null={counts['null']}")


if __name__ == "__main__":
    main()
