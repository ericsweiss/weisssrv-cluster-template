#!/usr/bin/env bash
# Run a maintenance command, then run the verify script whatever the outcome.
# Exits with the command's rc if it failed, else the verify's.
# Usage and VERIFY_SCRIPT: docs/SCRIPTS.md.

# No `-e`: a failing command must still reach the verify below.
set -uo pipefail

if [ "$#" -lt 1 ]; then
  echo "ERROR: $0 requires at least one argument (the command to run)" >&2
  exit 64  # EX_USAGE
fi

REPO_DIR="${CI_PROJECT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
VERIFY="${VERIFY_SCRIPT:-scripts/post-maintenance-verify.sh}"
case "$VERIFY" in
  /*) ;;
  *) VERIFY="${REPO_DIR}/${VERIFY}" ;;
esac

# Invoked as `bash "$VERIFY"`, so readability is enough: a CI checkout need not
# preserve the +x bit.
if [ ! -r "$VERIFY" ]; then
  echo "ERROR: verify script not found or not readable: $VERIFY" >&2
  exit 64
fi

echo "=== Maintenance command: $* ==="
"$@"
op_rc=$?
echo ""
echo "=== Maintenance command exited with rc=$op_rc; running verify ==="
echo ""

bash "$VERIFY"
verify_rc=$?

echo ""
if [ "$op_rc" -ne 0 ]; then
  echo "=== SUMMARY: maintenance command FAILED (rc=$op_rc); verify rc=$verify_rc ==="
  exit "$op_rc"
fi
if [ "$verify_rc" -ne 0 ]; then
  echo "=== SUMMARY: maintenance command OK; verify FAILED (rc=$verify_rc) ==="
  exit "$verify_rc"
fi
echo "=== SUMMARY: maintenance command OK; verify OK ==="
