"""Dual-write copy + cutover-gate compare-report for jobs v1 → jobs_v2.

Usage:
    python scripts/migrate_jobs_v2.py [--copy] [--report] [--apply-drop]

    --copy        Copy all rows from jobs v1 to jobs_v2 (idempotent upsert).
    --report      Run compare-report only (no writes). Exit 0 = gate PASS.
    --apply-drop  Apply migrations/jobs-v1-drop.sql via Supabase MCP.
                  REQUIRES --report to have passed in the same invocation.

Gate conditions (both must hold for exit 0):
    1. Every (source, external_id) key in jobs v1 exists in jobs_v2 (0 missing).
    2. Sampled 100-row field diff on title/url/company shows 0 mismatches.

Environment:
    SUPABASE_URL   — Supabase REST endpoint
    SUPABASE_KEY   — service-role key

Dependencies: supabase-py (already in project requirements).
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from typing import Any

from supabase import create_client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client():
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_KEY"]
    return create_client(url, key)


def _page(client, table: str, select: str, offset: int, page_size: int) -> list[dict]:
    return (
        client.table(table)
        .select(select)
        .range(offset, offset + page_size - 1)
        .execute()
        .data
        or []
    )


def _fetch_all(client, table: str, select: str, page_size: int = 1000) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        page = _page(client, table, select, offset, page_size)
        rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return rows


# ---------------------------------------------------------------------------
# Copy pass (idempotent upsert)
# ---------------------------------------------------------------------------

V1_COPY_FIELDS = [
    "external_id", "title", "company", "location", "remote", "description",
    "salary_min", "salary_max", "url", "source", "keywords", "scraped_at",
    "metadata", "job_type", "content_hash", "score_stage_1", "score_stage_2",
    "archetype", "company_domain", "score_stage_3", "match_reasoning",
    "match_highlights", "match_pitch", "location_normalized", "location_lat",
    "location_lon", "is_remote", "is_hybrid", "dq_flags",
]

# Fields that must not be None for the v2 NOT NULL constraints.
# external_id and source are guaranteed NOT NULL by the upsert-pk decision.
_NOT_NULL_DEFAULTS: dict[str, Any] = {
    "score_stage_1": 0,
    "is_remote": False,
    "is_hybrid": False,
    "dq_flags": {},
    "remote": False,
}


def _row_v1_to_v2(row: dict) -> dict:
    """Project a v1 row onto v2 shape. Drops profile_id; applies NOT NULL defaults."""
    out: dict[str, Any] = {}
    for field in V1_COPY_FIELDS:
        val = row.get(field)
        if val is None and field in _NOT_NULL_DEFAULTS:
            val = _NOT_NULL_DEFAULTS[field]
        out[field] = val
    # Preserve original scraped_at as first_seen_at if available.
    out["first_seen_at"] = row.get("scraped_at") or "now()"
    out["last_seen_at"] = row.get("scraped_at") or "now()"
    # Default active status for all copied rows.
    out["status"] = "active"
    return out


def cmd_copy(client) -> None:
    print("Fetching jobs v1 rows...")
    v1_rows = _fetch_all(client, "jobs", ",".join(V1_COPY_FIELDS + ["scraped_at"]))
    print(f"  v1 row count: {len(v1_rows)}")

    batch_size = 500
    upserted = 0
    errors = 0
    for i in range(0, len(v1_rows), batch_size):
        batch = [_row_v1_to_v2(r) for r in v1_rows[i : i + batch_size]]
        try:
            client.table("jobs_v2").upsert(
                batch,
                on_conflict="source,external_id",
            ).execute()
            upserted += len(batch)
        except Exception as exc:
            errors += len(batch)
            print(f"  ERROR batch {i}–{i+len(batch)}: {exc}", file=sys.stderr)

    print(f"Copy done: upserted={upserted}, errors={errors}")
    if errors:
        sys.exit(1)


# ---------------------------------------------------------------------------
# Compare-report (gate)
# ---------------------------------------------------------------------------

def cmd_report(client) -> bool:
    """Run compare-report. Returns True = gate PASS, False = gate FAIL."""
    print("\n=== Cutover-Gate Compare-Report ===\n")

    # -- Gate 1: key coverage --
    print("Gate 1: checking (source, external_id) key coverage...")
    v1_keys_raw = _fetch_all(client, "jobs", "source,external_id")
    v1_keys = {(r["source"], r["external_id"]) for r in v1_keys_raw}

    v2_keys_raw = _fetch_all(client, "jobs_v2", "source,external_id")
    v2_keys = {(r["source"], r["external_id"]) for r in v2_keys_raw}

    missing = v1_keys - v2_keys
    print(f"  v1 keys: {len(v1_keys)}")
    print(f"  v2 keys: {len(v2_keys)}")
    print(f"  missing in v2: {len(missing)}")

    gate1_pass = len(missing) == 0
    if not gate1_pass:
        print(f"  FAIL — {len(missing)} v1 keys absent from v2. Sample: {list(missing)[:5]}")
    else:
        print("  PASS")

    # -- Gate 2: sampled 100-row field diff --
    print("\nGate 2: sampling 100 rows for title/url/company field diff...")
    sample_keys = random.sample(list(v1_keys & v2_keys), min(100, len(v1_keys & v2_keys)))
    mismatches = 0

    for source, ext_id in sample_keys:
        v1_row = (
            client.table("jobs")
            .select("source,external_id,title,url,company")
            .eq("source", source)
            .eq("external_id", ext_id)
            .single()
            .execute()
            .data
        )
        v2_row = (
            client.table("jobs_v2")
            .select("source,external_id,title,url,company")
            .eq("source", source)
            .eq("external_id", ext_id)
            .single()
            .execute()
            .data
        )
        if not v1_row or not v2_row:
            mismatches += 1
            print(f"  MISMATCH (row fetch failed): ({source}, {ext_id})")
            continue
        for field in ("title", "url", "company"):
            if v1_row.get(field) != v2_row.get(field):
                mismatches += 1
                print(
                    f"  MISMATCH {field}: ({source}, {ext_id})\n"
                    f"    v1={v1_row.get(field)!r}\n"
                    f"    v2={v2_row.get(field)!r}"
                )

    gate2_pass = mismatches == 0
    if not gate2_pass:
        print(f"  FAIL — {mismatches} field mismatches in 100-row sample.")
    else:
        print(f"  PASS — 0 mismatches across {len(sample_keys)} sampled rows.")

    # -- Summary --
    gate_pass = gate1_pass and gate2_pass
    print(f"\nGate result: {'PASS ✓' if gate_pass else 'FAIL ✗'}")
    print(f"  Gate 1 (key coverage): {'PASS' if gate1_pass else 'FAIL'}")
    print(f"  Gate 2 (field diff):   {'PASS' if gate2_pass else 'FAIL'}")
    if not gate_pass:
        print("\nCutover BLOCKED. Fix issues above before retrying.")
    else:
        print("\nCutover gate CLEARED. Set JOBS_TABLE=jobs_v2 (or run with --apply-drop).")

    return gate_pass


# ---------------------------------------------------------------------------
# Drop (post-gate only)
# ---------------------------------------------------------------------------

def cmd_apply_drop(client) -> None:
    """Apply jobs-v1-drop.sql via Supabase REST (reads the file, runs via execute_sql).

    NOTE: This function is intentionally NOT called unless --report passed in the
    same invocation. The drop SQL itself has embedded guards (see migrations/jobs-v1-drop.sql).
    """
    import pathlib

    drop_sql_path = pathlib.Path(__file__).parent.parent / "migrations" / "jobs-v1-drop.sql"
    sql = drop_sql_path.read_text(encoding="utf-8")
    print(f"\nApplying {drop_sql_path} ...")
    # supabase-py REST does not expose raw DDL execute; caller must use MCP or psql.
    # Print the SQL for the operator to review and apply manually if needed.
    print("--- SQL to apply via Supabase MCP apply_migration (name: jobs_v1_drop) ---")
    print(sql)
    print("--- End SQL ---")
    print(
        "\nTo apply: use mcp__supabase__apply_migration with name='jobs_v1_drop' "
        "and project_id='guocdgjpbvsvcvchgolm'."
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--copy", action="store_true", help="Copy v1 rows to jobs_v2")
    parser.add_argument("--report", action="store_true", help="Run compare-report (gate check)")
    parser.add_argument(
        "--apply-drop",
        action="store_true",
        help="Print drop SQL (requires --report to pass in same run)",
    )
    args = parser.parse_args()

    if not any([args.copy, args.report, args.apply_drop]):
        parser.print_help()
        sys.exit(0)

    client = _client()

    if args.copy:
        cmd_copy(client)

    gate_pass = False
    if args.report:
        gate_pass = cmd_report(client)
        if not gate_pass:
            sys.exit(1)

    if args.apply_drop:
        if not args.report or not gate_pass:
            print(
                "ERROR: --apply-drop requires --report to pass in the same invocation.",
                file=sys.stderr,
            )
            sys.exit(1)
        cmd_apply_drop(client)


if __name__ == "__main__":
    main()
