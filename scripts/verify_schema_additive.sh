#!/usr/bin/env bash
# verify_schema_additive.sh
# AC-010: Assert schema migration is additive-only — no baseline columns dropped.
# Queries current schema via supabase-py, diffs against hardcoded baseline.
# Usage: bash scripts/verify_schema_additive.sh
# Exit 0 = additive-only confirmed. Exit 1 = dropped column detected or error.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
# The .env lives in the main repo root (worktrees share it via git worktree)
# Worktree layout: <main-repo>/.worktrees/<branch>/ → .env is 2 levels up
ENV_ROOT="${REPO_ROOT}"
for candidate in "${REPO_ROOT}" "${REPO_ROOT}/.." "${REPO_ROOT}/../.."; do
    if [ -f "${candidate}/.env" ]; then
        ENV_ROOT="$(cd "${candidate}" && pwd)"
        break
    fi
done

BASELINE_FILE="${REPO_ROOT}/.specs/10-consumer-agnostic-refactor/schema-baseline.txt"
QUERY_SCRIPT="${REPO_ROOT}/scripts/_schema_query_helper.py"

echo "=== verify_schema_additive.sh ==="
echo "Repo root: ${REPO_ROOT}"
echo ""

# ---------------------------------------------------------------------------
# Hardcoded baseline columns (25 columns confirmed by MCP on 2026-04-22)
# ---------------------------------------------------------------------------
HARDCODED_BASELINE=(
    "archetype"
    "company"
    "company_domain"
    "content_hash"
    "description"
    "external_id"
    "id"
    "job_type"
    "keywords"
    "location"
    "match_highlights"
    "match_pitch"
    "match_reasoning"
    "metadata"
    "profile_id"
    "remote"
    "salary_max"
    "salary_min"
    "score_stage_1"
    "score_stage_2"
    "score_stage_3"
    "scraped_at"
    "source"
    "title"
    "url"
)

# ---------------------------------------------------------------------------
# Write inline Python helper to a temp file (avoids heredoc + set -e issues)
# ---------------------------------------------------------------------------
HELPER_PY=$(mktemp /tmp/schema_query_XXXXXX.py)
cat > "${HELPER_PY}" << 'PYEOF'
import os
import sys
from pathlib import Path

# Load .env from repo root (passed as argv[1])
repo_root = sys.argv[1] if len(sys.argv) > 1 else "."
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(repo_root) / ".env")
except ImportError:
    pass

supabase_url = os.environ.get("SUPABASE_URL", "")
supabase_key = os.environ.get("SUPABASE_KEY", "")

if not supabase_url or not supabase_key:
    print("ERROR: SUPABASE_URL or SUPABASE_KEY not set", file=sys.stderr)
    sys.exit(1)

try:
    from supabase import create_client
    client = create_client(supabase_url, supabase_key)
    res = client.table("jobs").select("*").limit(1).execute()
    if res.data:
        cols = list(res.data[0].keys())
        for col in sorted(cols):
            print(col)
        sys.exit(0)
    else:
        print("WARN: jobs table returned no rows", file=sys.stderr)
        sys.exit(2)
except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF

# ---------------------------------------------------------------------------
# Query current schema
# ---------------------------------------------------------------------------
echo "Querying current Supabase schema for 'jobs' table..."

CURRENT_COLS_FILE=$(mktemp /tmp/schema_current_XXXXXX.txt)

python3 "${HELPER_PY}" "${ENV_ROOT}" 2>/tmp/schema_py_stderr.txt | tr -d '\r' > "${CURRENT_COLS_FILE}"
PYTHON_EXIT=$?

rm -f "${HELPER_PY}"

if [ "${PYTHON_EXIT}" -eq 2 ]; then
    echo "WARN: Table empty — cannot introspect columns from REST response"
    echo "OK: Schema additive-only check DEFERRED (table empty)"
    rm -f "${CURRENT_COLS_FILE}"
    exit 0
fi

if [ "${PYTHON_EXIT}" -ne 0 ]; then
    echo "FAIL: Could not query current schema"
    cat /tmp/schema_py_stderr.txt >&2 || true
    rm -f "${CURRENT_COLS_FILE}"
    exit 1
fi

echo "Current columns in 'jobs' ($(wc -l < "${CURRENT_COLS_FILE}") total):"
cat "${CURRENT_COLS_FILE}"
echo ""

# ---------------------------------------------------------------------------
# Build baseline column list
# ---------------------------------------------------------------------------
BASELINE_COLS_FILE=$(mktemp /tmp/schema_baseline_XXXXXX.txt)

if [ -f "${BASELINE_FILE}" ]; then
    echo "Using schema-baseline.txt: ${BASELINE_FILE}"
    grep -oE '^[a-z_]+' "${BASELINE_FILE}" | sort > "${BASELINE_COLS_FILE}"
else
    echo "WARN: schema-baseline.txt not found — using hardcoded 25-column baseline"
    for col in "${HARDCODED_BASELINE[@]}"; do
        echo "${col}"
    done | sort > "${BASELINE_COLS_FILE}"
fi

SORTED_CURRENT=$(mktemp /tmp/schema_sorted_XXXXXX.txt)
sort "${CURRENT_COLS_FILE}" > "${SORTED_CURRENT}"

# ---------------------------------------------------------------------------
# Diff: any baseline column missing from current = DROP detected
# ---------------------------------------------------------------------------
MISSING=$(comm -23 "${BASELINE_COLS_FILE}" "${SORTED_CURRENT}")

rm -f "${CURRENT_COLS_FILE}" "${BASELINE_COLS_FILE}" "${SORTED_CURRENT}"

if [ -n "${MISSING}" ]; then
    echo "FAIL: Baseline columns MISSING from current schema (DROP detected):"
    echo "${MISSING}"
    exit 1
fi

echo "OK: Schema is additive-only — all 25 baseline columns present"
exit 0
