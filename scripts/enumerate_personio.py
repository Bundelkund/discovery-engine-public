#!/usr/bin/env python3
"""Enumerate Personio company job boards via Common Crawl, then validate live feeds.

Why: the Personio scraper (app/sources/personio.py) is a curated allow-list, not a
crawler. It only fetches slugs already listed in config/portals*.yaml. This script
discovers ALL `{slug}.jobs.personio.de` boards Common Crawl has seen, verifies which
still serve a live `/xml` feed, diffs them against what we already track, and emits a
paste-ready YAML snippet of NEW candidates for manual curation.

Pipeline:
  1. CDX pull   -> query last N Common Crawl indexes for domain jobs.personio.de,
                   extract distinct slugs (free, no auth, slug delivered directly).
  2. Validate   -> GET {slug}.jobs.personio.de/xml in parallel; keep HTTP 200 feeds
                   that parse and contain <position> entries. Dead/typo slugs drop out.
  3. Diff       -> subtract slugs already present in portals.yaml + portals.local.yaml.
  4. Output     -> JSON report + candidates.yaml (tracked_companies entries).

Deterministic + idempotent: re-running overwrites the two output files, no side effects
on the live config. Curation (ranking, renaming, enabling) stays manual.

Usage:
  python scripts/enumerate_personio.py                  # last 4 crawls, validate, write out/
  python scripts/enumerate_personio.py --crawls 6       # widen coverage
  python scripts/enumerate_personio.py --no-validate    # CDX slugs only, skip /xml probe
  python scripts/enumerate_personio.py --limit 50       # cap slugs (smoke test)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
OUT_DIR = Path(__file__).resolve().parent / "out"

CDX_COLLINFO = "https://index.commoncrawl.org/collinfo.json"
PERSONIO_DOMAIN = "jobs.personio.de"
FEED_URL = "https://{slug}.jobs.personio.de/xml"
SUFFIX = ".jobs.personio.de"

USER_AGENT = "discovery-engine-personio-enumerator/1.0 (+job-board discovery)"


# --------------------------------------------------------------------------- #
# Step 1: Common Crawl CDX enumeration
# --------------------------------------------------------------------------- #
def _get_with_retry(
    client: httpx.Client, url: str, *, params: dict | None = None,
    timeout: float = 90.0, retries: int = 5, label: str = "",
) -> httpx.Response | None:
    """GET with exponential backoff on 5xx / timeouts. CC's index server is flaky
    (504s on heavy domain queries are common); retrying after a pause usually works.
    Returns the response, or None if all attempts fail."""
    delay = 4.0
    for attempt in range(1, retries + 1):
        try:
            resp = client.get(url, params=params, timeout=timeout)
            if resp.status_code in (502, 503, 504):
                raise httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}", request=resp.request, response=resp
                )
            resp.raise_for_status()
            return resp
        except Exception as e:  # noqa: BLE001
            if attempt == retries:
                print(f"  {label} gave up after {retries} tries: {e}", file=sys.stderr)
                return None
            print(f"  {label} attempt {attempt} failed ({e}); retry in {delay:.0f}s",
                  file=sys.stderr)
            time.sleep(delay)
            delay *= 2
    return None


def latest_crawls(n: int, client: httpx.Client) -> list[str]:
    """Return the IDs of the n most recent monthly Common Crawl indexes."""
    resp = _get_with_retry(client, CDX_COLLINFO, timeout=30.0, label="collinfo")
    if resp is None:
        raise RuntimeError("could not fetch Common Crawl crawl list")
    return [c["id"] for c in resp.json()[:n]]


def slug_from_host(host: str) -> str | None:
    """Extract the Personio slug from a hostname, or None if not a board host."""
    host = host.lower().strip().rstrip(".")
    if not host.endswith(SUFFIX):
        return None
    prefix = host[: -len(SUFFIX)]
    if not prefix:  # bare jobs.personio.de
        return None
    # slug is the label immediately left of .jobs.personio.de; slugs never contain dots
    slug = prefix.split(".")[-1]
    return slug or None


def enumerate_slugs(
    crawls: list[str], client: httpx.Client, page_size: int = 10000, retries: int = 5
) -> dict[str, set[str]]:
    """Pull distinct slugs per crawl from the CDX API. Returns {slug: {crawl_ids}}.

    Uses resumeKey pagination instead of showNumPages: a limited fetch streams from the
    index without the expensive full-count step that consistently 504s on big domains.
    Each page returns up to page_size records plus a trailing plain-text resume key.
    """
    seen: dict[str, set[str]] = {}
    for crawl in crawls:
        index = f"https://index.commoncrawl.org/{crawl}-index"
        base = {
            "url": PERSONIO_DOMAIN,
            "matchType": "domain",
            "output": "json",
            "fl": "url",
            "limit": str(page_size),
            "showResumeKey": "true",
        }
        crawl_slugs: set[str] = set()
        resume: str | None = None
        page = 0
        while True:
            params = dict(base)
            if resume:
                params["resumeKey"] = resume
            resp = _get_with_retry(
                client, index, params=params, timeout=120.0, retries=retries,
                label=f"[{crawl}] page {page}",
            )
            if resp is None:
                break
            next_resume: str | None = None
            for line in resp.text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    url = json.loads(line).get("url", "")
                except json.JSONDecodeError:
                    next_resume = line  # trailing non-JSON line = resume key
                    continue
                host = urlparse(url).hostname or ""
                slug = slug_from_host(host)
                if slug:
                    crawl_slugs.add(slug)
            page += 1
            if not next_resume or next_resume == resume:
                break
            resume = next_resume

        for slug in crawl_slugs:
            seen.setdefault(slug, set()).add(crawl)
        print(f"  [{crawl}] {len(crawl_slugs)} slugs ({page} pages)", file=sys.stderr)
    return seen


# --------------------------------------------------------------------------- #
# Step 2: live /xml feed validation
# --------------------------------------------------------------------------- #
def validate_slug(slug: str, client: httpx.Client, retries: int = 3) -> dict:
    """Probe {slug}.jobs.personio.de/xml. Return status + job count + sample titles.

    Retries on HTTP 429 (rate limit) with backoff so that active boards are not
    falsely dropped when many slugs are probed concurrently.
    """
    url = FEED_URL.format(slug=slug)
    result = {"slug": slug, "feed_url": url, "active": False, "job_count": 0,
              "sample_titles": [], "error": None}
    delay = 2.0
    for attempt in range(1, retries + 1):
        try:
            resp = client.get(url, timeout=20.0, follow_redirects=True)
            if resp.status_code == 429:
                result["error"] = "HTTP 429"
                if attempt < retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                return result
            if resp.status_code != 200:
                result["error"] = f"HTTP {resp.status_code}"
                return result
            if "<position" not in resp.text:
                result["error"] = "no <position> in feed"
                return result
            root = ET.fromstring(resp.text)
            positions = root.findall("position")
            result["active"] = True
            result["job_count"] = len(positions)
            result["sample_titles"] = [
                p.findtext("name", "").strip() for p in positions[:3]
            ]
            result["error"] = None
            return result
        except ET.ParseError as e:
            result["error"] = f"xml parse: {e}"
            return result
        except Exception as e:  # noqa: BLE001
            result["error"] = f"{type(e).__name__}: {e}"
            return result
    return result


def validate_all(slugs: list[str], workers: int) -> list[dict]:
    results: list[dict] = []
    with httpx.Client(headers={"User-Agent": USER_AGENT}) as client:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(validate_slug, s, client): s for s in slugs}
            done = 0
            for fut in as_completed(futures):
                results.append(fut.result())
                done += 1
                if done % 25 == 0 or done == len(slugs):
                    print(f"  validated {done}/{len(slugs)}", file=sys.stderr)
    return results


# --------------------------------------------------------------------------- #
# Step 3: diff against already-tracked slugs
# --------------------------------------------------------------------------- #
def tracked_slugs() -> set[str]:
    """Collect Personio slugs already present in portals.yaml + portals.local.yaml."""
    tracked: set[str] = set()
    for fname in ("portals.yaml", "portals.local.yaml"):
        path = CONFIG_DIR / fname
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        for company in data.get("tracked_companies", []):
            careers = (company.get("careers_url") or "")
            if "personio.de" not in careers:
                continue
            host = urlparse(careers).hostname or ""
            slug = slug_from_host(host)
            if slug:
                tracked.add(slug)
    return tracked


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def write_outputs(report: dict, candidates: list[dict]) -> tuple[Path, Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUT_DIR / "personio-enumeration.json"
    yaml_path = OUT_DIR / "personio-candidates.yaml"

    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = [
        "# Personio candidates discovered via Common Crawl + /xml validation.",
        f"# Generated: {report['generated_at']}  |  crawls: {', '.join(report['crawls'])}",
        "# NEW active boards not yet tracked. Curate (rank, rename, enable) before merging",
        "# into portals.local.yaml -> tracked_companies. enabled:false by default.",
        "",
        "tracked_companies:",
    ]
    for c in sorted(candidates, key=lambda x: -x["job_count"]):
        name = c["slug"].replace("-", " ").title()
        titles = "; ".join(t for t in c["sample_titles"] if t)[:120]
        lines.append(f"  - name: {name}")
        lines.append(f"    careers_url: https://{c['slug']}.jobs.personio.de")
        lines.append(f"    notes: \"{c['job_count']} jobs. {titles}\"")
        lines.append("    enabled: false")
        lines.append("")
    yaml_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, yaml_path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--crawls", type=int, default=4, help="number of recent CC crawls (default 4)")
    ap.add_argument("--limit", type=int, default=0, help="cap slugs before validation (0=all)")
    ap.add_argument("--workers", type=int, default=10, help="parallel /xml probes (default 10)")
    ap.add_argument("--no-validate", action="store_true", help="skip /xml probe, emit raw slugs")
    ap.add_argument("--retries", type=int, default=5, help="CDX retries on 5xx/timeout (default 5)")
    args = ap.parse_args()

    started = datetime.now(timezone.utc).isoformat()
    with httpx.Client(headers={"User-Agent": USER_AGENT}) as client:
        crawls = latest_crawls(args.crawls, client)
        print(f"Crawls: {', '.join(crawls)}", file=sys.stderr)
        print("Step 1: CDX enumeration ...", file=sys.stderr)
        seen = enumerate_slugs(crawls, client, retries=args.retries)

    all_slugs = sorted(seen)
    print(f"  -> {len(all_slugs)} distinct slugs", file=sys.stderr)
    if args.limit:
        all_slugs = all_slugs[: args.limit]
        print(f"  -> capped to {len(all_slugs)}", file=sys.stderr)

    already = tracked_slugs()
    print(f"Already tracked: {len(already)}", file=sys.stderr)

    if args.no_validate:
        validations = [{"slug": s, "active": None, "job_count": None,
                        "sample_titles": [], "error": "not validated",
                        "feed_url": FEED_URL.format(slug=s)} for s in all_slugs]
    else:
        print(f"Step 2: validating {len(all_slugs)} /xml feeds ...", file=sys.stderr)
        validations = validate_all(all_slugs, args.workers)

    by_slug = {v["slug"]: v for v in validations}
    active = [v for v in validations if v["active"]]
    candidates = [
        {**by_slug[s], "crawls": sorted(seen.get(s, []))}
        for s in all_slugs
        if s not in already and (args.no_validate or by_slug[s]["active"])
    ]

    report = {
        "generated_at": started,
        "crawls": crawls,
        "total_slugs_seen": len(seen),
        "validated": (not args.no_validate),
        "active_feeds": len(active) if not args.no_validate else None,
        "already_tracked": sorted(already),
        "new_candidates_count": len(candidates),
        "candidates": candidates,
        "all_validations": validations,
    }
    json_path, yaml_path = write_outputs(report, candidates)

    print("", file=sys.stderr)
    print(f"distinct slugs   : {len(seen)}", file=sys.stderr)
    if not args.no_validate:
        print(f"active feeds     : {len(active)}", file=sys.stderr)
    print(f"already tracked  : {len(already)}", file=sys.stderr)
    print(f"NEW candidates   : {len(candidates)}", file=sys.stderr)
    print(f"report  -> {json_path}", file=sys.stderr)
    print(f"snippet -> {yaml_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
