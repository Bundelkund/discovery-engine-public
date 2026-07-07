#!/usr/bin/env python3
"""Verify candidate careers domains are b-ite, and emit ready-to-add config entries.

b-ite runs on each employer's OWN domain, so there is no board universe to
enumerate (unlike the slug-ATS in ats_scanner.py). You FIND candidates elsewhere
(source-code search like PublicWWW/BuiltWith for "jobs.b-ite.com", public-sector
job boards, reseller client lists) and feed the domains here to VET them.

Per candidate this does the cheap check only — robots.txt -> job sitemap -> ONE
sample posting's schema.org JobPosting fingerprint — so it costs a handful of
requests even for an employer with thousands of jobs. It never writes anything:
it prints a report + a `bite_sites:` YAML block you paste (after curating!) into
    florian-knowledge/dev/projects/career/profile/search-profile.yaml
then regenerate sources.yaml via scripts/sync-search-profile.js.

Usage:
    python scripts/bite_discover.py https://karriere.acme.de https://jobs.foo.de
    python scripts/bite_discover.py --file candidates.txt      # one URL per line
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root -> import app

from app.sources.bite import BiteScraper  # noqa: E402


def _load_candidates(args: argparse.Namespace) -> list[str]:
    cands = list(args.urls)
    if args.file:
        for line in Path(args.file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                cands.append(line)
    # dedup, preserve order
    return list(dict.fromkeys(cands))


async def _run(candidates: list[str], timeout: float) -> list[dict]:
    scraper = BiteScraper()
    results: list[dict] = []
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for base in candidates:
            results.append(await scraper.probe_site(client, base))
    return results


def _report(results: list[dict]) -> None:
    confirmed = [r for r in results if r["is_bite"]]
    for r in results:
        if r["is_bite"]:
            print(f"✓ {r['base']}  [{r['job_count']} jobs]  {r['employer']}")
        else:
            print(f"✗ {r['base']}  — {r['reason']}")

    print(f"\n{len(confirmed)}/{len(results)} confirmed b-ite.")
    if confirmed:
        print("\n# --- paste into search-profile.yaml `bite_sites:` AFTER curating "
              "(skip huge/irrelevant employers) ---")
        print("bite_sites:")
        for r in confirmed:
            name = r["employer"].replace('"', '\\"')
            note = "  # ⚠ high volume" if r["job_count"] > 500 else ""
            print(f'  - name: "{name}"')
            print(f'    base_url: "{r["base"]}"{note}')


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("urls", nargs="*", help="candidate careers-site base URLs")
    ap.add_argument("--file", help="file with one candidate URL per line (# comments ok)")
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()

    candidates = _load_candidates(args)
    if not candidates:
        print("no candidates — pass URLs or --file", file=sys.stderr)
        return 2

    results = asyncio.run(_run(candidates, args.timeout))
    _report(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
