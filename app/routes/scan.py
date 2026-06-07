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

import json
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
FETCH_LEVER = REPO_ROOT / "scripts" / "fetch_lever_theirstack.py"
LEVER_CURATED = REPO_ROOT / "config" / "curated-slugs" / "lever.txt"

# stage -> scanner flag (Stage B refresh vs Stage A discover). 'discover-lever' is special
# (TheirStack apply-link harvest, not CDX) and routed to _run_lever, not _run.
STAGE_FLAG = {"revalidate": "--revalidate", "discover": "--no-validate"}
STAGES = set(STAGE_FLAG) | {"discover-lever"}


def _hydrate_from_db(supabase, ats: str | None) -> dict[str, int]:
    """Write {ats}-enumeration.json from ats_companies so scanner --revalidate has a
    slug universe (scripts/out/ is ephemeral in the container; the registry is the SoT).

    Scope = monitor=true (active+paused; dead boards are revived via monthly discover).
    _load_prior() only needs all_validations[].slug. Returns {ats: slug_count}.
    """
    rows: list[dict] = []
    page = 0
    while True:
        q = supabase.table("ats_companies").select("ats,slug").eq("monitor", True)
        if ats:
            q = q.eq("ats", ats)
        res = q.range(page * 1000, page * 1000 + 999).execute()
        rows.extend(res.data or [])
        if not res.data or len(res.data) < 1000:
            break
        page += 1

    by_ats: dict[str, list[str]] = {}
    for r in rows:
        by_ats.setdefault(r["ats"], []).append(r["slug"])

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for a, slugs in by_ats.items():
        path = OUT_DIR / f"{a}-enumeration.json"
        path.write_text(json.dumps({
            "ats": a, "crawls": [], "candidates": [],
            "all_validations": [{"slug": s} for s in slugs],
        }), encoding="utf-8")
        counts[a] = len(slugs)
    return counts


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
            # Stage B (revalidate) needs a prior slug universe on disk; hydrate it from
            # the registry (out/ is ephemeral in prod). Stage A (discover) CDX-enumerates
            # fresh and needs no prior.
            if stage == "revalidate":
                hydrated = _hydrate_from_db(supabase, ats)
                stats["hydrated"] = hydrated
                log.write(f"--- hydrated from ats_companies: {hydrated} ---\n")
                log.flush()
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
                err_tail = (seed.stderr or "").strip()[-800:] or stats["seed_summary"]
                stats["seed_stderr_tail"] = err_tail
                raise RuntimeError(f"seeder exited {seed.returncode}: {err_tail}")

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


def _all_lever_slugs(supabase) -> list[str]:
    """Every lever slug in ats_companies (incl. inactive) — the no-delete registry is the SoT.
    Unioned into the curated file so a stateless prod container keeps prior discoveries even
    though scripts/out/ + the file write-back are ephemeral (mirrors _hydrate_from_db)."""
    out: list[str] = []
    page = 0
    while True:
        res = (supabase.table("ats_companies").select("slug").eq("ats", "lever")
               .range(page * 1000, page * 1000 + 999).execute())
        out.extend(r["slug"] for r in (res.data or []) if r.get("slug"))
        if not res.data or len(res.data) < 1000:
            return out
        page += 1


def _union_into_file(path: Path, slugs: list[str]) -> int:
    """Union slugs into the curated file (dedup, header preserved). Returns total slug count."""
    existing, header = [], []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            (header if line.startswith("#") else existing).append(line.strip())
    merged = sorted({s for s in [*existing, *slugs] if s and not s.startswith("#")})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([*header, *merged]) + "\n", encoding="utf-8")
    return len(merged)


def _run_lever(run_id: str, max_credits: int | None = None) -> None:
    """Background worker for the discover-lever stage: TheirStack apply-link harvest ->
    union with DB lever slugs -> validate via scanner list-mode -> seed. Mirrors _run's audit.
    max_credits caps the TheirStack spend (testing/cost control; n8n passes none -> fetch default)."""
    from app.dependencies import get_supabase as _gs

    supabase = _gs()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = OUT_DIR / f"scan-discover-lever-{ts}.log"
    stats: dict = {"log_path": str(log_path)}
    try:
        with open(log_path, "w", encoding="utf-8") as log:
            fetch_cmd = [sys.executable, str(FETCH_LEVER), "--out", str(LEVER_CURATED)]
            if max_credits:
                fetch_cmd += ["--max-credits", str(max_credits)]
            fetch = subprocess.run(
                fetch_cmd, cwd=str(REPO_ROOT), stdout=log, stderr=subprocess.STDOUT, text=True,
            )
            stats["fetch_rc"] = fetch.returncode
            log.write(f"\n--- fetch rc={fetch.returncode} ---\n")
            # DB-union so prior discoveries survive the ephemeral container/file.
            db_slugs = _all_lever_slugs(supabase)
            total = _union_into_file(LEVER_CURATED, db_slugs)
            stats["curated_total"] = total
            stats["db_union"] = len(db_slugs)
            log.write(f"--- db-union: +{len(db_slugs)} known -> {total} total slugs ---\n")
            log.flush()
            if fetch.returncode != 0:
                raise RuntimeError(f"fetch exited {fetch.returncode}")

            scan = subprocess.run(
                [sys.executable, str(SCANNER), "--ats", "lever",
                 "--slugs-file", str(LEVER_CURATED), "--source", "scrape"],
                cwd=str(REPO_ROOT), stdout=log, stderr=subprocess.STDOUT, text=True,
            )
            stats["scanner_rc"] = scan.returncode
            log.write(f"\n--- scanner rc={scan.returncode} ---\n")
            log.flush()
            if scan.returncode != 0:
                raise RuntimeError(f"scanner exited {scan.returncode}")

            seed = subprocess.run(
                [sys.executable, str(SEEDER), "--ats", "lever"],
                cwd=str(REPO_ROOT), capture_output=True, text=True,
            )
            stats["seed_rc"] = seed.returncode
            log.write(seed.stdout or "")
            log.write(seed.stderr or "")
            lines = [ln for ln in (seed.stdout or "").splitlines() if ln.strip()]
            stats["seed_summary"] = lines[-1] if lines else ""
            if seed.returncode != 0:
                err_tail = (seed.stderr or "").strip()[-800:] or stats["seed_summary"]
                stats["seed_stderr_tail"] = err_tail
                raise RuntimeError(f"seeder exited {seed.returncode}: {err_tail}")

        supabase.table("ats_scan_runs").update(
            {"status": "done", "finished_at": datetime.now(timezone.utc).isoformat(),
             "stats": stats}
        ).eq("id", run_id).execute()
        logger.info("scan_run_done", extra={"run_id": run_id, "stage": "discover-lever"})
    except Exception as e:  # noqa: BLE001
        supabase.table("ats_scan_runs").update(
            {"status": "failed", "finished_at": datetime.now(timezone.utc).isoformat(),
             "stats": stats, "error": str(e)[:2000]}
        ).eq("id", run_id).execute()
        logger.error("scan_run_failed", extra={"run_id": run_id, "stage": "discover-lever", "error": str(e)})


@scan_router.post("/{stage}", status_code=202, dependencies=[Depends(require_scope("scrape:trigger"))])
async def trigger_scan(
    stage: str,
    background_tasks: BackgroundTasks,
    ats: str | None = Query(None, description="narrow to one provider (default: all)"),
    limit: int | None = Query(None, ge=1, description="cap slugs (testing/targeted reruns)"),
    max_credits: int | None = Query(None, ge=1, description="discover-lever: cap TheirStack credit spend"),
    supabase=Depends(get_supabase),
):
    if stage not in STAGES:
        raise HTTPException(status_code=404,
                            detail=f"unknown stage '{stage}' (revalidate|discover|discover-lever)")

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
    if stage == "discover-lever":
        background_tasks.add_task(_run_lever, run_id, max_credits)
    else:
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
