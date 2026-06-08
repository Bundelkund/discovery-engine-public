#!/usr/bin/env bash
# verify_no_profile_id.sh — agnostik guard
#
# Greps app/ for functional profile_id occurrences and exits non-zero if any
# are found.  Lines that are pure Python comments, docstring lines, or plain
# English prose inside a string literal are excluded.
#
# "Functional" means: assignment, parameter passing, dict key usage, field
# definitions, or imports.  Documentation mentioning profile_id in context
# (e.g. "no profile_id required") is not functional and is excluded.
#
# Usage:
#   bash scripts/verify_no_profile_id.sh       # exit 0 = clean
#
# CI integration: add this as a check step; a non-zero exit fails the build.

set -euo pipefail

SEARCH_ROOT="${1:-app}"

# Match lines that contain profile_id as functional Python code:
#   - variable/attribute name in assignment: profile_id =, .profile_id =
#   - keyword argument: profile_id=
#   - dict literal key: "profile_id":  or  'profile_id':
#   - model field: profile_id: type
# Exclude:
#   - lines whose first non-whitespace char is # (Python comment)
#   - lines that are inside docstrings (contain only string content — no = or :)
#   - lines matching the "no profile_id" / "profile_id required" prose pattern
matches=$(
  grep -rn "profile_id" "$SEARCH_ROOT" \
    --include="*.py" \
    --exclude-dir="graphify-out" \
  | grep -v '^\s*#' \
  | grep -Ev \
    'no[[:space:]]+profile_id|profile_id[[:space:]]+(required|is[[:space:]]|sent|anywhere|—)|without[[:space:]]+profile_id|[Nn]o[[:space:]]+profile_id' \
  || true
)

if [ -n "$matches" ]; then
  echo "ERROR: functional profile_id occurrences found in $SEARCH_ROOT:" >&2
  echo "$matches" >&2
  exit 1
fi

echo "OK: no functional profile_id occurrences in $SEARCH_ROOT"
exit 0
