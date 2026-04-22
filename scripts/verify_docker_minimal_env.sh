#!/usr/bin/env bash
# verify_docker_minimal_env.sh
# AC-012: Build and start container without LLM env vars, assert /health returns 200.
# NOTE: This script is SKIPPED in Phase 4 validation — executed by Worker-F (Phase 6).
# Usage: bash scripts/verify_docker_minimal_env.sh
# Exit 0 = OK or docker not installed (graceful skip). Exit 1 = failure.

set -e
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

CONTAINER_NAME="de-test-minimal-$$"
IMAGE_NAME="de-test-minimal"
PORT=18091
HEALTH_URL="http://localhost:${PORT}/health"

echo "=== verify_docker_minimal_env.sh ==="

# ---------------------------------------------------------------------------
# Graceful skip if docker not installed
# ---------------------------------------------------------------------------
if ! command -v docker &>/dev/null; then
    echo "SKIP: docker not installed — Phase 6 (Worker-F) will run this check"
    exit 0
fi

# ---------------------------------------------------------------------------
# Check for .env.minimal (minimal env without LLM keys)
# ---------------------------------------------------------------------------
ENV_MINIMAL="${REPO_ROOT}/.env.minimal"
if [ ! -f "${ENV_MINIMAL}" ]; then
    echo "SKIP: .env.minimal not found at ${ENV_MINIMAL}"
    echo "      Create .env.minimal with SUPABASE_URL, SUPABASE_KEY, DE_API_KEY (no OPENAI/ANTHROPIC keys)"
    exit 0
fi

# Verify .env.minimal contains no LLM keys
if grep -qE 'OPENAI_API_KEY|ANTHROPIC_API_KEY' "${ENV_MINIMAL}"; then
    echo "FAIL: .env.minimal contains OPENAI_API_KEY or ANTHROPIC_API_KEY — must NOT have LLM keys"
    exit 1
fi

echo "Building Docker image: ${IMAGE_NAME}..."
docker build -t "${IMAGE_NAME}" "${REPO_ROOT}" 2>&1

echo "Starting container: ${CONTAINER_NAME} on port ${PORT}..."
docker run -d \
    --name "${CONTAINER_NAME}" \
    -p "${PORT}:8091" \
    --env-file "${ENV_MINIMAL}" \
    "${IMAGE_NAME}" 2>&1

# Wait for startup
echo "Waiting for container startup..."
RETRIES=10
for i in $(seq 1 ${RETRIES}); do
    if curl -sf "${HEALTH_URL}" >/dev/null 2>&1; then
        break
    fi
    if [ "${i}" -eq "${RETRIES}" ]; then
        echo "FAIL: /health did not respond after ${RETRIES} retries"
        docker logs "${CONTAINER_NAME}" 2>&1 || true
        docker rm -f "${CONTAINER_NAME}" 2>/dev/null || true
        exit 1
    fi
    sleep 2
done

# Assert /health returns 200
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "${HEALTH_URL}")
docker rm -f "${CONTAINER_NAME}" 2>/dev/null || true

if [ "${HTTP_CODE}" = "200" ]; then
    echo "OK: Container started without LLM env vars, /health returned 200"
    exit 0
else
    echo "FAIL: /health returned HTTP ${HTTP_CODE} (expected 200)"
    exit 1
fi
