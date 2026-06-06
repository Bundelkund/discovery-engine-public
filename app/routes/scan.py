"""ATS-scanner trigger endpoint (T8 cc-recheck-cron).

n8n fires POST /scan/{stage} on a schedule (fire-and-forget). The scanner is a
long-running Python subprocess (~5915 feeds), so it runs as a FastAPI BackgroundTask;
the request returns 202 immediately with a run_id. Run status + audit live in
public.ats_scan_runs (see docs/ats-pipeline.md).

  Stage B  revalidate  (daily)   -> ats_scanner --all --revalidate   (refresh state)
  Stage A  discover    (monthly) -> ats_scanner --all --no-validate  (new slugs)

Both stages run scripts/seed_ats_companies.py afterwards to upsert into ats_companies
(no-delete). Only one run per stage at a time (409 otherwise) to avoid CC overlap.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from app.dependencies import get_supabase, require_scope

logger = logging.getLogger(__name__)

scan_router = APIRouter(prefix="/scan", tags=["scan"])

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = REPO_ROOT / "scripts" / "out"
SCANNER = REPO_ROOT / "scripts" / "ats_scanner.py"
SEEDER = REPO_ROOT / "scripts" / "seed_ats_companies.py"

# stage -> scanner flag (Stage B refresh vs Stage A discover)
STAGE_FLAG = {"revalidate": "--revalidate", "discover": "--no-validate"}


def _run(stage: str, run_id: str, ats: str | None = None, limit: int | None = None) -> None:
    """Background worker: run scanner then seeder, update the ats_scan_runs row.

    Uses a fresh Supabase client (the request-scoped one is gone by now).
    `ats`/`limit` are optional scope-narrowers (testing / targeted reruns); n8n
    passes neither -> full --all run.
    """
    from app.dependencies import get_supabase as _gs

    supabase = _gs()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = OUT_DIR / f"scan-{stage}-{ts}.log"
    stats: dict = {"log_path": str(log_path)}
    scope = ["--ats", ats] if ats else ["--all"]
    if limit:
        scope += ["--limit", str(limit)]
    try:
        with open(log_path, "w", encoding="utf-8") as log:
            scan = subprocess.run(
                [sys.executable, str(SCANNER), *scope, STAGE_FLAG[stage]],
                cwd=str(REPO_ROOT), stdout=log, stderr=subprocess.STDOUT, text=True,
            )
            stats["scanner_rc"] = scan.returncode
            log.write(f"\n--- scanner rc={scan.returncode} ---\n")
            log.flush()
            if scan.returncode != 0:
                raise RuntimeError(f"scanner exited {scan.returncode}")

            seed = subprocess.run(
                [sys.executable, str(SEEDER), *(["--ats", ats] if ats else [])],
                cwd=str(REPO_ROOT), capture_output=True, text=True,
            )
            stats["seed_rc"] = seed.returncode
            log.write(seed.stdout or "")
            log.write(seed.stderr or "")
            # last non-empty stdout line, e.g. "done: 12 inserted, 8302 updated across 7 ATS"
            lines = [ln for ln in (seed.stdout or "").splitlines() if ln.strip()]
            stats["seed_summary"] = lines[-1] if lines else ""
            if seed.returncode != 0:
                raise RuntimeError(f"seeder exited {seed.returncode}: {stats['seed_summary']}")

        supabase.table("ats_scan_runs").update(
            {"status": "done", "finished_at": datetime.now(timezone.utc).isoformat(),
             "stats": stats}
        ).eq("id", run_id).execute()
        logger.info("scan_run_done", extra={"run_id": run_id, "stage": stage})
    except Exception as e:  # noqa: BLE001
        supabase.table("ats_scan_runs").update(
            {"status": "failed", "finished_at": datetime.now(timezone.utc).isoformat(),
             "stats": stats, "error": str(e)[:2000]}
        ).eq("id", run_id).execute()
        logger.error("scan_run_failed", extra={"run_id": run_id, "stage": stage, "error": str(e)})


@scan_router.post("/{stage}", status_code=202, dependencies=[Depends(require_scope("scrape:trigger"))])
async def trigger_scan(
    stage: str,
    background_tasks: BackgroundTasks,
    ats: str | None = Query(None, description="narrow to one provider (default: all)"),
    limit: int | None = Query(None, ge=1, description="cap slugs (testing/targeted reruns)"),
    supabase=Depends(get_supabase),
):
    if stage not in STAGE_FLAG:
        raise HTTPException(status_code=404, detail=f"unknown stage '{stage}' (revalidate|discover)")

    # one run per stage at a time -> no overlapping CC fetches
    running = (
        supabase.table("ats_scan_runs")
        .select("id")
        .eq("stage", stage)
        .eq("status", "running")
        .limit(1)
        .execute()
    )
    if running.data:
        raise HTTPException(status_code=409, detail=f"a '{stage}' run is already in progress")

    ins = supabase.table("ats_scan_runs").insert(
        {"stage": stage, "status": "running",
         "stats": {"ats": ats, "limit": limit} if (ats or limit) else None}
    ).execute()
    run_id = ins.data[0]["id"]
    background_tasks.add_task(_run, stage, run_id, ats, limit)
    logger.info("scan_run_started", extra={"run_id": run_id, "stage": stage, "ats": ats})
    return {"run_id": run_id, "stage": stage, "status": "running"}


@scan_router.get("/runs", dependencies=[Depends(require_scope("jobs:read"))])
async def list_runs(limit: int = Query(20, ge=1, le=100), supabase=Depends(get_supabase)):
    res = (
        supabase.table("ats_scan_runs")
        .select("*")
        .order("started_at", desc=True)
        .limit(limit)
        .execute()
    )
    return {"runs": res.data}
