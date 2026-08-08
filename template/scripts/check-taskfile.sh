#!/usr/bin/env bash
# Assert that every scripts/<name>.{sh,py} referenced by Taskfile.yml exists on
# disk, plus the `dotenv:` target scripts/hosts.env. go-task compiles command
# templates lazily and `task --list`/`--dry` never stat referenced files, so a
# since-deleted/renamed script reference isn't caught by go-task itself — this
# closes that gap for the taskfile-smoke CI job (which runs `task --list` for the
# YAML/schema check, then this for the dangling-reference check).
#
# Usage: scripts/check-taskfile.sh [Taskfile.yml]
set -euo pipefail

_SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$_SCRIPT_DIR/.." && pwd)"
TASKFILE="${1:-$REPO_ROOT/Taskfile.yml}"

[ -f "$TASKFILE" ] || {
    echo "ERROR: Taskfile not found: $TASKFILE" >&2
    exit 1
}

rc=0

# Every scripts/<name>.(sh|py) mentioned in the Taskfile must exist. A while-read
# over process substitution (NOT `grep | while`, whose subshell would drop rc)
# keeps the loop in the current shell so rc survives.
while IFS= read -r ref; do
    [ -n "$ref" ] || continue
    if [ ! -f "$REPO_ROOT/$ref" ]; then
        echo "ERROR: Taskfile.yml references missing $ref" >&2
        rc=1
    fi
done < <(grep -oE 'scripts/[A-Za-z0-9_./-]+\.(sh|py)' "$TASKFILE" | sort -u)

# The dotenv target must exist — go-task fails hard loading a missing dotenv
# file. Match the bare path (not just same-line `dotenv:`) so the YAML
# multi-line list form is caught too.
if grep -qE '(^|[[:space:]"'"'"'-])scripts/hosts\.env([[:space:]"'"'"']|$)' "$TASKFILE" \
    && [ ! -f "$REPO_ROOT/scripts/hosts.env" ]; then
    echo "ERROR: Taskfile dotenv target scripts/hosts.env is missing" >&2
    rc=1
fi

if [ "$rc" -eq 0 ]; then
    echo "OK: all Taskfile-referenced scripts + scripts/hosts.env exist."
fi
exit "$rc"
