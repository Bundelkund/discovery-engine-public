#!/usr/bin/env python3
"""Generalized ATS company-slug scanner via Common Crawl, with live feed validation.

Why: each slug-based ATS hosts every customer under one shared domain
(`{slug}.jobs.personio.de`, `jobs.lever.co/{slug}`, ...). Common Crawl has already
indexed those URLs, so a CDX prefix-search over the shared domain enumerates the
customer slugs for free — no per-board crawling, no auth. This is the engine behind
scripts/enumerate_personio.py, generalized to a provider registry so every slug-ATS
(personio, recruitee, breezy, factorial, greenhouse, lever, ashby) shares one pipeline.

Pipeline (per provider):
  1. CDX pull   -> query last N CC indexes for the provider's shared domain, extract
                   distinct customer slugs (subdomain label or first path segment).
  2. Validate   -> hit each provider's public jobs feed/API per slug in parallel; keep
                   the ones that return a parseable, non-empty job list. (--no-validate
                   skips this: raw CDX slug counts only, fast.)
  3. Output     -> scripts/out/{ats}-enumeration.json (full report) + a candidates YAML.

Personio is the reference provider: `--ats personio` reproduces the original board set.
enumerate_personio.py stays as the standalone Personio entry point.

NOTE on volume: personio/factorial are DE-leaning (.de/.com slugs); recruitee, breezy,
greenhouse, lever, ashby are GLOBAL — a full CDX walk can be huge. --max-pages bounds it
and the report flags `truncated: true` when the cap is hit (no silent truncation).

Usage:
  python scripts/ats_scanner.py --ats personio                 # one provider, validate
  python scripts/ats_scanner.py --ats greenhouse --no-validate # CDX slug counts only
  python scripts/ats_scanner.py --all --no-validate --crawls 2 # enumerate every provider
  python scripts/ats_scanner.py --ats lever --max-pages 20     # cap CDX paging
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
from urllib.parse import parse_qs, urlparse

import httpx

OUT_DIR = Path(__file__).resolve().parent / "out"
CDX_COLLINFO = "https://index.commoncrawl.org/collinfo.json"
USER_AGENT = "discovery-engine-ats-scanner/1.0 (+job-board discovery)"

# --------------------------------------------------------------------------- #
# Provider registry
#   mode      : "subdomain" -> slug = label left of `suffix`
#               "path"      -> slug = first path segment of `host`
#   feed      : URL template, {slug} substituted
#   kind      : feed validator (see validate_feed)
#   skip      : path-mode reserved first-segments that are not company slugs
# --------------------------------------------------------------------------- #
PROVIDERS: dict[str, dict] = {
    "personio": {
        "cdx_domains": ["jobs.personio.de"],
        "mode": "subdomain", "suffix": ".jobs.personio.de",
        "feed": "https://{slug}.jobs.personio.de/xml", "kind": "xml-position",
    },
    "recruitee": {
        "cdx_domains": ["recruitee.com"],
        "mode": "subdomain", "suffix": ".recruitee.com",
        "feed": "https://{slug}.recruitee.com/api/offers/", "kind": "json:offers",
    },
    "breezy": {
        "cdx_domains": ["breezy.hr"],
        "mode": "subdomain", "suffix": ".breezy.hr",
        "feed": "https://{slug}.breezy.hr/json", "kind": "json:list",
    },
    "factorial": {
        "cdx_domains": ["factorialhr.com", "factorialhr.de", "factorialhr.es"],
        "mode": "subdomain", "suffix": ".factorialhr.com",
        "feed": "https://{slug}.factorialhr.com/job_posting", "kind": "head-200",
    },
    "greenhouse": {
        "cdx_domains": ["boards.greenhouse.io", "job-boards.greenhouse.io"],
        "mode": "path", "skip": {"embed", "api"},
        "feed": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        "kind": "json:jobs",
    },
    "lever": {
        "cdx_domains": ["jobs.lever.co"],
        "mode": "path", "skip": set(),
        "feed": "https://api.lever.co/v0/postings/{slug}?mode=json", "kind": "json:list",
    },
    "ashby": {
        "cdx_domains": ["jobs.ashbyhq.com"],
        "mode": "path", "skip": {"api", "embed"},
        "feed": "https://api.ashbyhq.com/posting-api/job-board/{slug}", "kind": "json:jobs",
    },
}

# slugs are single DNS/path labels: lowercase alphanum + hyphen, no dots
_SLUG_BAD = set('. /?#&=%@:"\'\\')


def _clean_slug(raw: str, skip: set[str]) -> str | None:
    s = raw.lower().strip().strip("-")
    if not s or s in skip or any(ch in _SLUG_BAD for ch in s):
        return None
    return s


# --------------------------------------------------------------------------- #
# Step 1: CDX enumeration (shared with personio enumerator's logic)
# --------------------------------------------------------------------------- #
def _get_with_retry(client, url, *, params=None, timeout=120.0, retries=5, label=""):
    """GET with exponential backoff on 5xx/timeout. CC's index 504s under load."""
    delay = 4.0
    for attempt in range(1, retries + 1):
        try:
            resp = client.get(url, params=params, timeout=timeout)
            if resp.status_code in (502, 503, 504):
                raise httpx.HTTPStatusError(f"HTTP {resp.status_code}",
                                            request=resp.request, response=resp)
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


def latest_crawls(n: int, client) -> list[str]:
    resp = _get_with_retry(client, CDX_COLLINFO, timeout=30.0, label="collinfo")
    if resp is None:
        raise RuntimeError("could not fetch Common Crawl crawl list")
    return [c["id"] for c in resp.json()[:n]]


def slug_from_url(url: str, prov: dict) -> str | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if prov["mode"] == "subdomain":
        suffix = prov["suffix"]
        if not host.endswith(suffix):
            return None
        prefix = host[: -len(suffix)]
        if not prefix:
            return None
        return _clean_slug(prefix.split(".")[-1], set())
    # path mode
    skip = prov.get("skip", set())
    segs = [s for s in parsed.path.split("/") if s]
    if not segs:
        return None
    first = segs[0]
    # greenhouse embed: slug lives in ?for=<slug>
    if first in skip:
        if first == "embed":
            for key in ("for", "token"):
                vals = parse_qs(parsed.query).get(key)
                if vals:
                    return _clean_slug(vals[0], set())
        return None
    return _clean_slug(first, skip)


def enumerate_slugs(prov, crawls, client, page_size=10000, retries=5, max_pages=0):
    """Pull distinct slugs per crawl via resumeKey pagination. {slug: {crawl_ids}}.
    Returns (seen, truncated) — truncated=True if max_pages cap hit on any crawl."""
    seen: dict[str, set[str]] = {}
    truncated = False
    for domain in prov["cdx_domains"]:
        for crawl in crawls:
            index = f"https://index.commoncrawl.org/{crawl}-index"
            base = {"url": domain, "matchType": "domain", "output": "json",
                    "fl": "url", "limit": str(page_size), "showResumeKey": "true"}
            crawl_slugs: set[str] = set()
            resume = None
            page = 0
            while True:
                if max_pages and page >= max_pages:
                    truncated = True
                    print(f"  [{crawl}|{domain}] max-pages {max_pages} hit", file=sys.stderr)
                    break
                params = dict(base)
                if resume:
                    params["resumeKey"] = resume
                resp = _get_with_retry(client, index, params=params, timeout=120.0,
                                       retries=retries, label=f"[{crawl}|{domain}] p{page}")
                if resp is None:
                    break
                next_resume = None
                for line in resp.text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        url = json.loads(line).get("url", "")
                    except json.JSONDecodeError:
                        next_resume = line  # trailing non-JSON = resume key
                        continue
                    slug = slug_from_url(url, prov)
                    if slug:
                        crawl_slugs.add(slug)
                page += 1
                if not next_resume or next_resume == resume:
                    break
                resume = next_resume
            for slug in crawl_slugs:
                seen.setdefault(slug, set()).add(crawl)
            print(f"  [{crawl}|{domain}] {len(crawl_slugs)} slugs ({page} pages)",
                  file=sys.stderr)
    return seen, truncated


# --------------------------------------------------------------------------- #
# Step 2: feed validation
# --------------------------------------------------------------------------- #
def validate_feed(slug, prov, client, retries=3):
    url = prov["feed"].format(slug=slug)
    kind = prov["kind"]
    out = {"slug": slug, "feed_url": url, "active": False, "job_count": 0,
           "sample_titles": [], "error": None}
    delay = 2.0
    for attempt in range(1, retries + 1):
        try:
            resp = client.get(url, timeout=20.0, follow_redirects=True)
            if resp.status_code == 429:
                out["error"] = "HTTP 429"
                if attempt < retries:
                    time.sleep(delay); delay *= 2; continue
                return out
            if resp.status_code != 200:
                out["error"] = f"HTTP {resp.status_code}"
                return out
            return _parse_feed(resp, kind, out)
        except ET.ParseError as e:
            out["error"] = f"xml parse: {e}"; return out
        except Exception as e:  # noqa: BLE001
            out["error"] = f"{type(e).__name__}: {e}"; return out
    return out


def _parse_feed(resp, kind, out):
    if kind == "head-200":
        out["active"] = True
        return out
    if kind == "xml-position":
        if "<position" not in resp.text:
            out["error"] = "no <position>"; return out
        positions = ET.fromstring(resp.text).findall("position")
        out.update(active=True, job_count=len(positions),
                   sample_titles=[p.findtext("name", "").strip() for p in positions[:3]])
        return out
    # json kinds
    data = resp.json()
    if kind == "json:list":
        jobs = data if isinstance(data, list) else []
    elif kind.startswith("json:"):
        jobs = data.get(kind.split(":", 1)[1], []) if isinstance(data, dict) else []
    else:
        jobs = []
    if not jobs:
        out["error"] = "empty job list"; return out
    out.update(active=True, job_count=len(jobs),
               sample_titles=[_job_title(j) for j in jobs[:3]])
    return out


def _job_title(j):
    if not isinstance(j, dict):
        return ""
    for k in ("title", "name", "text", "position"):
        v = j.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def validate_all(slugs, prov, workers):
    results = []
    with httpx.Client(headers={"User-Agent": USER_AGENT}) as client:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(validate_feed, s, prov, client): s for s in slugs}
            done = 0
            for fut in as_completed(futs):
                results.append(fut.result())
                done += 1
                if done % 50 == 0 or done == len(slugs):
                    print(f"  validated {done}/{len(slugs)}", file=sys.stderr)
    return results


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def write_outputs(ats, report, candidates):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUT_DIR / f"{ats}-enumeration.json"
    yaml_path = OUT_DIR / f"{ats}-candidates.yaml"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    lines = [
        f"# {ats} slugs discovered via Common Crawl{' + feed validation' if report['validated'] else ' (CDX only, unvalidated)'}.",
        f"# Generated: {report['generated_at']}  |  crawls: {', '.join(report['crawls'])}"
        + ("  |  TRUNCATED (max-pages hit)" if report.get("truncated") else ""),
        f"# distinct slugs: {report['total_slugs_seen']}"
        + (f"  active feeds: {report['active_feeds']}" if report["validated"] else ""),
        "slugs:",
    ]
    key = (lambda x: -(x.get("job_count") or 0)) if report["validated"] else (lambda x: x["slug"])
    for c in sorted(candidates, key=key):
        titles = "; ".join(t for t in c.get("sample_titles", []) if t)[:120]
        suffix = f"  # {c['job_count']} jobs: {titles}" if report["validated"] and c.get("job_count") else ""
        lines.append(f"  - {c['slug']}{suffix}")
    yaml_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, yaml_path


def scan(ats, args, client):
    prov = PROVIDERS[ats]
    print(f"\n=== {ats} ({prov['mode']}) ===", file=sys.stderr)
    crawls = latest_crawls(args.crawls, client)
    seen, truncated = enumerate_slugs(prov, crawls, client, retries=args.retries,
                                      max_pages=args.max_pages)
    all_slugs = sorted(seen)
    print(f"  -> {len(all_slugs)} distinct slugs", file=sys.stderr)
    if args.limit:
        all_slugs = all_slugs[: args.limit]

    if args.no_validate:
        validations = [{"slug": s, "active": None, "job_count": None,
                        "sample_titles": [], "error": "not validated",
                        "feed_url": prov["feed"].format(slug=s)} for s in all_slugs]
        active = []
    else:
        print(f"  validating {len(all_slugs)} feeds ...", file=sys.stderr)
        validations = validate_all(all_slugs, prov, args.workers)
        active = [v for v in validations if v["active"]]

    by_slug = {v["slug"]: v for v in validations}
    candidates = [{**by_slug[s], "crawls": sorted(seen.get(s, []))} for s in all_slugs
                  if args.no_validate or by_slug[s]["active"]]
    report = {
        "ats": ats, "generated_at": datetime.now(timezone.utc).isoformat(),
        "crawls": crawls, "cdx_domains": prov["cdx_domains"],
        "total_slugs_seen": len(seen), "validated": (not args.no_validate),
        "truncated": truncated,
        "active_feeds": len(active) if not args.no_validate else None,
        "candidates": candidates, "all_validations": validations,
    }
    jp, _ = write_outputs(ats, report, candidates)
    tag = "TRUNCATED " if truncated else ""
    av = f" active={len(active)}" if not args.no_validate else ""
    print(f"  {tag}slugs={len(seen)}{av}  -> {jp.name}", file=sys.stderr)
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ats", choices=sorted(PROVIDERS), help="single provider")
    ap.add_argument("--all", action="store_true", help="scan every provider")
    ap.add_argument("--crawls", type=int, default=2, help="recent CC crawls (default 2)")
    ap.add_argument("--limit", type=int, default=0, help="cap slugs before validate (0=all)")
    ap.add_argument("--workers", type=int, default=12, help="parallel feed probes")
    ap.add_argument("--no-validate", action="store_true", help="CDX slugs only, skip feeds")
    ap.add_argument("--retries", type=int, default=5, help="CDX retries on 5xx/timeout")
    ap.add_argument("--max-pages", type=int, default=0, help="cap CDX pages per crawl (0=all)")
    args = ap.parse_args()

    if not args.ats and not args.all:
        ap.error("pass --ats <name> or --all")
    targets = sorted(PROVIDERS) if args.all else [args.ats]

    summary = []
    with httpx.Client(headers={"User-Agent": USER_AGENT}) as client:
        for ats in targets:
            try:
                r = scan(ats, args, client)
                summary.append((ats, r["total_slugs_seen"], r.get("active_feeds"),
                                r["truncated"]))
            except Exception as e:  # noqa: BLE001
                print(f"  {ats} FAILED: {e}", file=sys.stderr)
                summary.append((ats, "ERR", None, False))

    print("\n=== summary ===", file=sys.stderr)
    print(f"{'ats':12} {'slugs':>8} {'active':>8}  trunc", file=sys.stderr)
    for ats, slugs, active, trunc in summary:
        print(f"{ats:12} {str(slugs):>8} {str(active if active is not None else '-'):>8}  "
              f"{'YES' if trunc else ''}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
