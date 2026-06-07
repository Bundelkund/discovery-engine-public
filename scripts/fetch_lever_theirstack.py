#!/usr/bin/env python3
"""Pull DE Lever board slugs from TheirStack -> curated slug list.

Lever is the one slug-ATS that Common Crawl can't enumerate (robots-blocked), so its
boards never show up in scripts/ats_scanner.py CDX runs. TheirStack aggregates live job
postings and *does* expose the `jobs.lever.co/{slug}` apply URL per posting, so a job
search filtered to `lever.co` + a country gives us the customer slugs.

Credits: TheirStack charges 1 API credit per JOB returned (not per company). The DE-lever
universe is ~99 companies across ~310 postings, so a full pull would cost ~310 credits and
blow the 200/mo free tier. We therefore page under a --max-credits budget and dedup slugs;
distinct companies plateau fast (long-tail pages are repeat companies). Coverage + a
`truncated` note are logged — never a silent cap. Re-run next cycle to catch the tail.

Output unions into config/curated-slugs/lever.txt (version-controlled SoT, preserves any
manually added slugs). Feed it to (source='scrape' = apply-link-derived, per ats_companies
CHECK cc|scrape|manual — NOT 'cc', which is reserved for CDX-enumerated):
  python scripts/ats_scanner.py --ats lever --slugs-file config/curated-slugs/lever.txt --source scrape
  python scripts/seed_ats_companies.py --ats lever

Reads THEIRSTACK_API_KEY from discovery-engine/.env.

Usage:
  python scripts/fetch_lever_theirstack.py                      # DE, budget 150 credits
  python scripts/fetch_lever_theirstack.py --max-credits 100    # smaller spend
  python scripts/fetch_lever_theirstack.py --country FR --out config/curated-slugs/lever-fr.txt
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.theirstack.com/v1/jobs/search"
DEFAULT_OUT = ROOT / "config" / "curated-slugs" / "lever.txt"
SLUG_RE = re.compile(r"lever\.co/([^/?#]+)", re.IGNORECASE)


def api_key() -> str:
    env = dotenv_values(ROOT / ".env")
    key = env.get("THEIRSTACK_API_KEY")
    if not key:
        sys.exit("ERROR: THEIRSTACK_API_KEY not in .env")
    return key


def slug_from_url(url: str) -> str | None:
    """First path segment after lever.co/. Lever allows dotted slugs (e.g. valpeo.com),
    so we keep dots — only lowercase + drop query/fragment."""
    m = SLUG_RE.search(url or "")
    if not m:
        return None
    seg = m.group(1).strip().lower().strip("-")
    return seg or None


def load_existing(path: Path) -> list[str]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def write_list(path: Path, slugs: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "# Curated Lever board slugs (Lever is NOT Common-Crawl-enumerable).",
        "# Source: TheirStack DE job search (scripts/fetch_lever_theirstack.py) + manual adds.",
        "# One slug per line. Dotted slugs (e.g. valpeo.com) are valid Lever slugs.",
    ]
    body = sorted(slugs)
    path.write_text("\n".join(header + body) + "\n", encoding="utf-8")


def fetch(key: str, country: str, max_age: int, max_credits: int, page_size: int) -> tuple[set[str], int, int]:
    """Returns (slugs, jobs_fetched=credits_spent, total_companies)."""
    hdr = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    slugs: set[str] = set()
    spent = 0
    total_companies = 0
    page = 0
    with httpx.Client(timeout=60) as c:
        while spent < max_credits:
            limit = min(page_size, max_credits - spent)
            body = {
                "page": page,
                "limit": limit,
                "include_total_results": True,
                "posted_at_max_age_days": max_age,
                "job_country_code_or": [country],
                "url_domain_or": ["lever.co"],
            }
            r = c.post(API, headers=hdr, json=body)
            if r.status_code != 200:
                print(f"  API {r.status_code}: {r.text[:300]}", file=sys.stderr)
                break
            d = r.json()
            meta = d.get("metadata") or {}
            total_companies = meta.get("total_companies") or total_companies
            total_results = meta.get("total_results") or 0
            jobs = d.get("data") or []
            spent += len(jobs)
            for j in jobs:
                s = slug_from_url(j.get("url") or j.get("final_url") or j.get("source_url") or "")
                if s:
                    slugs.add(s)
            print(f"  page {page}: +{len(jobs)} jobs ({spent} credits) -> {len(slugs)} distinct slugs",
                  file=sys.stderr)
            if len(jobs) < limit or (page + 1) * page_size >= total_results:
                break
            page += 1
    return slugs, spent, total_companies


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--country", default="DE", help="ISO country code (default DE)")
    ap.add_argument("--max-age-days", type=int, default=90, help="posting recency window (default 90)")
    ap.add_argument("--max-credits", type=int, default=150, help="credit budget = max jobs fetched (default 150, free tier=200/mo)")
    ap.add_argument("--page-size", type=int, default=25,
                    help="results per request (default 25 = TheirStack free-tier max; paid allows more)")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="curated slug file to union into")
    args = ap.parse_args()

    out = Path(args.out)
    before = set(load_existing(out))
    print(f"existing curated slugs: {len(before)}", file=sys.stderr)

    fetched, spent, total_companies = fetch(
        api_key(), args.country, args.max_age_days, args.max_credits, args.page_size
    )
    merged = before | fetched
    new = merged - before
    write_list(out, merged)

    truncated = spent >= args.max_credits and len(fetched) < total_companies
    print(f"\ndone: {spent} credits spent | {len(fetched)} distinct slugs fetched "
          f"(of ~{total_companies} {args.country} companies) | {len(new)} new -> {out}")
    if truncated:
        print(f"TRUNCATED: budget hit before full coverage ({len(fetched)}/{total_companies} companies). "
              f"Re-run next cycle for the tail.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
