#!/usr/bin/env bash
# Assert that every scripts/<name>.{sh,py} referenced by a Taskfile exists on
# disk, plus each `dotenv:` target. go-task compiles command templates lazily
# and never stats referenced files, so a renamed script is invisible to
# `task --list` — this closes that gap for the taskfile-smoke CI job.
#
# Usage: scripts/check-taskfile.sh [Taskfile.yml]
# Env:   CHECK_TASKFILE_DOTENV — space-separated dotenv targets to require when
#        the Taskfile references them (default: scripts/hosts.env)
set -euo pipefail

_SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$_SCRIPT_DIR/.." && pwd)"
TASKFILE="${1:-$REPO_ROOT/Taskfile.yml}"
DOTENV_TARGETS="${CHECK_TASKFILE_DOTENV:-scripts/hosts.env}"

[ -f "$TASKFILE" ] || {
    echo "ERROR: Taskfile not found: $TASKFILE" >&2
    exit 1
}

rc=0

# A while-read over process substitution (NOT `grep | while`, whose subshell
# would drop rc) keeps the loop in the current shell.
while IFS= read -r ref; do
    [ -n "$ref" ] || continue
    if [ ! -f "$REPO_ROOT/$ref" ]; then
        echo "ERROR: $(basename "$TASKFILE") references missing $ref" >&2
        rc=1
    fi
done < <(grep -oE 'scripts/[A-Za-z0-9_./-]+\.(sh|py)' "$TASKFILE" | sort -u)

# go-task fails hard loading a missing dotenv file. Match the bare path (not
# just a same-line `dotenv:`) so the YAML multi-line list form is caught too.
for target in $DOTENV_TARGETS; do
    target_ere="${target//./\\.}"
    if grep -qE '(^|[[:space:]"'"'"'-])'"$target_ere"'([[:space:]"'"'"']|$)' "$TASKFILE" \
        && [ ! -f "$REPO_ROOT/$target" ]; then
        echo "ERROR: Taskfile dotenv target $target is missing" >&2
        rc=1
    fi
done

if [ "$rc" -eq 0 ]; then
    echo "OK: all Taskfile-referenced scripts + dotenv targets exist."
fi
exit "$rc"
