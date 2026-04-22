#!/usr/bin/env bash
# verify_no_llm_deps.sh
# AC-002: Assert zero LLM (openai/anthropic) dependencies in code, pyproject, and .env.example
# Usage: bash scripts/verify_no_llm_deps.sh
# Exit 0 = clean. Exit 1 = LLM dependency found (CI gate failure).

set -e
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

FAILED=0

echo "=== verify_no_llm_deps.sh ==="
echo "Repo root: ${REPO_ROOT}"

# ---------------------------------------------------------------------------
# Check 1: Python imports in app/ scripts/ tests/
# ---------------------------------------------------------------------------
echo ""
echo "[1/3] Checking Python source files for openai/anthropic imports..."

if grep -rqE '(from|import) (openai|anthropic)' \
    "${REPO_ROOT}/app/" \
    "${REPO_ROOT}/scripts/" \
    "${REPO_ROOT}/tests/" 2>/dev/null; then
    echo "FAIL: LLM import found in app/ scripts/ tests/"
    grep -rE '(from|import) (openai|anthropic)' \
        "${REPO_ROOT}/app/" \
        "${REPO_ROOT}/scripts/" \
        "${REPO_ROOT}/tests/" 2>/dev/null || true
    FAILED=1
else
    echo "OK: No openai/anthropic imports in app/ scripts/ tests/"
fi

# ---------------------------------------------------------------------------
# Check 2: pyproject.toml dependencies section
# ---------------------------------------------------------------------------
echo ""
echo "[2/3] Checking pyproject.toml for openai/anthropic dependencies..."

PYPROJECT="${REPO_ROOT}/pyproject.toml"
if [ -f "${PYPROJECT}" ]; then
    # Match lines in [project.dependencies] or [tool.poetry.dependencies] that aren't comments
    if grep -E '^[^#]*(openai|anthropic)' "${PYPROJECT}" | grep -qvE '^\s*#'; then
        echo "FAIL: LLM dep found in pyproject.toml"
        grep -E '^[^#]*(openai|anthropic)' "${PYPROJECT}" | grep -vE '^\s*#' || true
        FAILED=1
    else
        echo "OK: No openai/anthropic in pyproject.toml dependencies"
    fi
else
    echo "SKIP: pyproject.toml not found at ${PYPROJECT}"
fi

# ---------------------------------------------------------------------------
# Check 3: .env.example for LLM API key placeholders
# ---------------------------------------------------------------------------
echo ""
echo "[3/3] Checking .env.example for OPENAI_API_KEY / ANTHROPIC_API_KEY..."

ENV_EXAMPLE="${REPO_ROOT}/.env.example"
if [ -f "${ENV_EXAMPLE}" ]; then
    if grep -qE 'OPENAI_API_KEY|ANTHROPIC_API_KEY' "${ENV_EXAMPLE}"; then
        echo "FAIL: LLM API key placeholder found in .env.example"
        grep -E 'OPENAI_API_KEY|ANTHROPIC_API_KEY' "${ENV_EXAMPLE}" || true
        FAILED=1
    else
        echo "OK: No LLM keys in .env.example"
    fi
else
    echo "SKIP: .env.example not found — skipping env check"
fi

# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------
echo ""
if [ "${FAILED}" -eq 0 ]; then
    echo "OK: All LLM-dependency checks passed"
    exit 0
else
    echo "FAIL: One or more LLM-dependency checks failed (see above)"
    exit 1
fi
