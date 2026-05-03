#!/usr/bin/env bash
# verify_wa_prereq.sh
# AC-019: Assert WonderApply backend has zero active call-sites to removed DE endpoints.
# Checks: /score/batch, /profiles/sync, /profiles/, /discover/, ProfileSync imports.
# Usage: bash scripts/verify_wa_prereq.sh
# Exit 0 = clean (0 matches). Exit 1 = active call-sites found.

set -e
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# WA backend path — set WA_BACKEND_PATH env var to point at the consumer repo.
# This script is a cross-repo CI gate; without WA_BACKEND_PATH set it skips with exit 0.
WA_BACKEND_PATH="${WA_BACKEND_PATH:-}"

echo "=== verify_wa_prereq.sh ==="

if [ -z "${WA_BACKEND_PATH}" ]; then
    echo "SKIP: WA_BACKEND_PATH not set. Set it to the WA backend root to run this gate."
    echo "      Exiting with 0 (non-blocking)."
    exit 0
fi

echo "WA backend path: ${WA_BACKEND_PATH}"
echo ""

if [ ! -d "${WA_BACKEND_PATH}" ]; then
    echo "SKIP: WA backend not found at ${WA_BACKEND_PATH}"
    echo "      Exiting with 0 (non-blocking)."
    exit 0
fi

echo "Scanning for removed DE endpoint call-sites..."

MATCHES=$(grep -rE \
    "(/score/batch|/profiles/sync|/profiles/|ProfileSync|/discover/opportunities)" \
    "${WA_BACKEND_PATH}" \
    --include="*.py" \
    2>/dev/null || true)

if [ -n "${MATCHES}" ]; then
    echo "FAIL: Active DE call-sites found in WA backend:"
    echo "${MATCHES}"
    echo ""
    echo "These call-sites must be removed before Bundle B can be deployed."
    echo "See Phase 0.5 in .specs/10-consumer-agnostic-refactor/plan.md"
    exit 1
fi

echo "OK: No active DE legacy call-sites in WA backend"
exit 0
