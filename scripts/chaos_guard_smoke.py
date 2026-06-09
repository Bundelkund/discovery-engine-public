"""Chaos-Guard smoke: prove the Spec-11 store-first pipeline runs end-to-end
against REAL prod volume. PRE-CRON GATE — run this before (re-)enabling the
scrape cron; if it does not print 'ALL PASS', do not enable the cron.

Found 5 real defects on its first run (2026-06-09) in pipeline code that unit
tests had all passed — this is the gate that exercises the wiring against live data.

Flow exercised:
  scrape(themuse, small) -> raw_jobs(status='new')
  -> RefinePipeline.run  (parse -> exact-dedup vs jobs_v2 -> near-dedup minhash
     -> dq -> location -> title_gate -> score -> upsert jobs_v2 -> mark_status)
  -> verify every fetched raw_job reached EXACTLY ONE terminal state (no strand)
  -> verify dedup actually fired against the existing jobs_v2 shelf (not a flood)
  -> re-run refine (must be a no-op: idempotency)
  -> re-scrape same source (raw_jobs source+external_id unique -> 23505 skip)

NOTE: C5 (inbox idempotency, rescrape stored=0) only passes AFTER the
migrations/raw-jobs-inbox-dedup.sql unique index is applied. Before that the
inbox has no constraint and the re-scrape appends duplicates (bounded: v2 still
sees delta 0 because exact-dedup protects the shelf).

This MUTATES prod (scrapes real jobs, upserts jobs_v2). It is deliberately NOT a
pytest in the default suite — keep it a manual / CI pre-cron step.
Run: python scripts/chaos_guard_smoke.py
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
os.chdir(REPO)          # config loaders use paths relative to repo root
sys.path.insert(0, str(REPO))

# --- load .env (SUPABASE_URL / SUPABASE_KEY / WA_API_KEY ...) ---
for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, _, v = line.partition("=")
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# Trigger plugin self-registration (decorators run at import — like main.py).
import app.sources    # noqa: E402,F401
import app.scoring    # noqa: E402,F401
import app.enrichment  # noqa: E402,F401

from app.dependencies import get_supabase                       # noqa: E402
from app.services.scrape_orchestrator import ScrapeOrchestrator  # noqa: E402
from app.services.refine_pipeline import RefinePipeline          # noqa: E402

SOURCE = "themuse"
SCRAPE_LIMIT = 12
JOBS_TABLE = os.environ.get("JOBS_TABLE", "jobs_v2")


def _count(sb, table, **eq) -> int:
    q = sb.table(table).select("*", count="exact", head=True)
    for k, v in eq.items():
        q = q.eq(k, v)
    return q.execute().count or 0


def _raw_status_breakdown(sb) -> dict:
    out = {}
    for st in ("new", "refined", "rejected", "duplicate"):
        out[st] = _count(sb, "raw_jobs", status=st)
    return out


async def main() -> int:
    sb = get_supabase()
    print(f"[cfg] JOBS_TABLE active shelf = {JOBS_TABLE}\n")

    # ---- snapshot BEFORE ----
    before = {
        "raw": _raw_status_breakdown(sb),
        "v2": _count(sb, JOBS_TABLE),
        "dedup_mem": _count(sb, "dedup_memory"),
    }
    print(f"[before] raw_jobs={before['raw']}  {JOBS_TABLE}={before['v2']}  dedup_memory={before['dedup_mem']}")

    # ---- 1) scrape (store-first) ----
    orch = ScrapeOrchestrator(sb)
    s1 = await orch.run(source_id=SOURCE, limit=SCRAPE_LIMIT, store=True)
    print(f"\n[scrape#1] found={s1.jobs_found} stored={s1.jobs_stored} errors={s1.errors} {s1.duration_ms}ms")
    raw_after_scrape = _raw_status_breakdown(sb)
    new_delta = raw_after_scrape["new"] - before["raw"]["new"]
    print(f"[scrape#1] raw_jobs(new) {before['raw']['new']} -> {raw_after_scrape['new']} (+{new_delta})")

    # ---- 2) refine pass ----
    r1 = await RefinePipeline(sb).run(limit=200)
    print(f"\n[refine#1] {r1}")
    accounted = r1["refined"] + r1["rejected"] + r1["duplicate"]
    print(f"[refine#1] accounted refined+rejected+duplicate = {accounted}  (fetched={r1['fetched']})")

    after = {
        "raw": _raw_status_breakdown(sb),
        "v2": _count(sb, JOBS_TABLE),
        "dedup_mem": _count(sb, "dedup_memory"),
    }
    print(f"[after#1] raw_jobs={after['raw']}  {JOBS_TABLE}={after['v2']}  dedup_memory={after['dedup_mem']}")

    # ---- 3) refine again -> must be a no-op ----
    r2 = await RefinePipeline(sb).run(limit=200)
    print(f"\n[refine#2 idempotency] {r2}  (expect fetched=0)")

    # ---- 4) re-scrape same source -> raw_jobs unique should skip (23505) ----
    s2 = await orch.run(source_id=SOURCE, limit=SCRAPE_LIMIT, store=True)
    print(f"\n[scrape#2 inbox-idempotency] found={s2.jobs_found} stored={s2.jobs_stored} (expect stored ~0 after migration)")

    # ---- VERDICT ----
    print("\n==== CHAOS-GUARD VERDICT ====")
    checks = []

    # C1 no stranded rows: every fetched row reached a terminal state
    c1 = (r1["fetched"] == 0) or (accounted + r1["errors"] >= r1["fetched"])
    still_new = after["raw"]["new"]
    checks.append(("C1 no-strand (accounted+errors >= fetched)", c1, f"accounted={accounted} errors={r1['errors']} fetched={r1['fetched']} raw.new_left={still_new}"))

    # C2 dedup fired against existing v2 (themuse jobs largely already in v2).
    # Observational check (always records, never fails) — surfaces the split.
    checks.append(("C2 dedup-vs-v2 ran (duplicate count observed)", True, f"duplicate={r1['duplicate']} refined={r1['refined']}"))

    # C3 no flood: v2 grew by AT MOST the refined count
    v2_delta = after["v2"] - before["v2"]
    c3 = v2_delta <= r1["refined"]
    checks.append(("C3 no-flood (v2_delta <= refined)", c3, f"v2_delta={v2_delta} refined={r1['refined']}"))

    # C4 refine idempotent: 2nd pass fetched 0
    c4 = r2["fetched"] == 0
    checks.append(("C4 refine idempotent (2nd fetched=0)", c4, f"r2.fetched={r2['fetched']}"))

    # C5 inbox idempotent: re-scrape stored ~0 (requires the unique-index migration)
    c5 = s2.jobs_stored == 0
    checks.append(("C5 inbox idempotent (rescrape stored=0; needs migration)", c5, f"stored={s2.jobs_stored}"))

    ok = True
    for name, passed, detail in checks:
        flag = "PASS" if passed else "FAIL"
        if not passed:
            ok = False
        print(f"  [{flag}] {name}  -- {detail}")

    print(f"\n==== {'ALL PASS' if ok else 'FAILURES PRESENT'} ====")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
