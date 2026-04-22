#!/usr/bin/env bash
# verify_file_deletions.sh
# AC-004: Assert all 9 Phase-1 deleted files are gone from the worktree.
# Usage: bash scripts/verify_file_deletions.sh
# Exit 0 = all files gone. Exit 1 = at least one file still exists.

set -e
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

FAILED=0

echo "=== verify_file_deletions.sh ==="
echo "Repo root: ${REPO_ROOT}"
echo ""

DELETED_FILES=(
    "app/scoring/llm.py"
    "app/scoring/embedding.py"
    "app/routes/profiles.py"
    "app/routes/discover.py"
    "app/routes/score.py"
    "app/repositories/profiles.py"
    "app/models/profile.py"
    "scripts/backfill_cv_embeddings.py"
    "app/utils/profile_mapper.py"
)

for f in "${DELETED_FILES[@]}"; do
    TARGET="${REPO_ROOT}/${f}"
    if [ -f "${TARGET}" ]; then
        echo "FAIL: ${f} still exists (should have been deleted in Phase 1)"
        FAILED=1
    else
        echo "OK:   ${f} deleted"
    fi
done

echo ""
if [ "${FAILED}" -eq 0 ]; then
    echo "OK: All Phase-1 deletions verified"
    exit 0
else
    echo "FAIL: ${FAILED} file(s) still present — Phase 1 incomplete"
    exit 1
fi
