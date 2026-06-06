#!/usr/bin/env python3
"""Load ats_scanner enumeration output into a Google Sheet, one tab per ATS.

Reads scripts/out/{ats}-enumeration.json and pushes the discovered slugs into the
target spreadsheet via the `gws` CLI (one worksheet/tab per provider). Rows are
chunked under the Windows ~32KB command-line limit (gws has no stdin/file --json).

Usage:
  python scripts/sheets_load_ats.py <spreadsheetId> [ats ...]
  python scripts/sheets_load_ats.py 1Qrx... ashby breezy factorial greenhouse recruitee
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent / "out"
CHUNK = 120  # rows per gws call -> ~22KB JSON, under 32KB cmdline cap

CAREERS = {
    "ashby":      "https://jobs.ashbyhq.com/{slug}",
    "breezy":     "https://{slug}.breezy.hr",
    "factorial":  "https://{slug}.factorialhr.com",
    "greenhouse": "https://boards.greenhouse.io/{slug}",
    "recruitee":  "https://{slug}.recruitee.com",
    "personio":   "https://{slug}.jobs.personio.de",
    "lever":      "https://jobs.lever.co/{slug}",
}
HEADER = ["slug", "careers_url", "feed_url", "crawls_seen", "active", "job_count", "sample_titles"]


def gws(*args: str, body: str | None = None) -> tuple[int, str]:
    cmd = ["gws", *args]
    if body is not None:
        cmd += ["--json", body]
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr)


def add_tab(sid: str, title: str) -> None:
    req = {"requests": [{"addSheet": {"properties": {"title": title}}}]}
    rc, out = gws("sheets", "spreadsheets", "batchUpdate",
                  "--params", json.dumps({"spreadsheetId": sid}), body=json.dumps(req))
    if rc != 0 and "already exists" not in out:
        print(f"  addSheet {title}: rc={rc} {out[:120]}", file=sys.stderr)


def put(sid: str, rng: str, values: list, how: str) -> bool:
    """how = 'update' (range-anchored) or 'append' (auto-extend)."""
    params = {"spreadsheetId": sid, "range": rng, "valueInputOption": "USER_ENTERED"}
    if how == "append":
        params["insertDataOption"] = "INSERT_ROWS"
    rc, out = gws("sheets", "spreadsheets", "values", how,
                  "--params", json.dumps(params), body=json.dumps({"values": values}))
    if rc != 0:
        print(f"  {how} {rng}: rc={rc} {out[:160]}", file=sys.stderr)
    return rc == 0


def rows_for(ats: str) -> list[list]:
    data = json.loads((OUT_DIR / f"{ats}-enumeration.json").read_text(encoding="utf-8"))
    tmpl = CAREERS.get(ats, "")
    rows = []
    for c in data["candidates"]:
        slug = c["slug"]
        rows.append([
            slug,
            tmpl.format(slug=slug) if tmpl else "",
            c.get("feed_url", ""),
            len(c.get("crawls", [])),
            "" if c.get("active") is None else c.get("active"),
            c.get("job_count") if c.get("job_count") is not None else "",
            "; ".join(t for t in c.get("sample_titles", []) if t)[:200],
        ])
    rows.sort(key=lambda r: r[0])
    return rows


def load(sid: str, ats: str) -> None:
    rows = rows_for(ats)
    print(f"{ats}: {len(rows)} rows", file=sys.stderr)
    add_tab(sid, ats)
    put(sid, f"{ats}!A1", [HEADER], "update")          # header
    for i in range(0, len(rows), CHUNK):
        chunk = rows[i:i + CHUNK]
        ok = put(sid, f"{ats}!A1", chunk, "append")
        print(f"  {ats} {i + len(chunk)}/{len(rows)} {'ok' if ok else 'FAIL'}", file=sys.stderr)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__); return 2
    sid = sys.argv[1]
    targets = sys.argv[2:] or ["ashby", "breezy", "factorial", "greenhouse", "recruitee"]
    for ats in targets:
        if not (OUT_DIR / f"{ats}-enumeration.json").exists():
            print(f"{ats}: no enumeration.json, skip", file=sys.stderr); continue
        load(sid, ats)
    print("done", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
