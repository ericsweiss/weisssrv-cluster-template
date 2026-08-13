#!/usr/bin/env bash
# Assert that every scripts/<name>.{sh,py} referenced by a Taskfile exists on
# disk, plus each `dotenv:` target. go-task compiles command templates lazily
# and never stats referenced files, so a renamed script is invisible to
# `task --list` — this closes that gap for the taskfile-smoke CI job.
#
# Included fragments are followed: the library's own taskfiles/ carry their own
# scripts/ references, so checking the named file alone leaves the included half
# unguarded for a consumer that vendors them.
#
# Usage: scripts/check-taskfile.sh [Taskfile.yml ...]
# Env:   CHECK_TASKFILE_DOTENV — space-separated dotenv targets to require when
#        the Taskfile references them (default: scripts/hosts.env)
#        CHECK_TASKFILE_MAX_DEPTH — include recursion cap (default: 10)
set -euo pipefail

_SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$_SCRIPT_DIR/.." && pwd)"
DOTENV_TARGETS="${CHECK_TASKFILE_DOTENV:-scripts/hosts.env}"
MAX_DEPTH="${CHECK_TASKFILE_MAX_DEPTH:-10}"

rc=0
VISITED=""

# Paths under an `includes:` block, both `name: path.yml` and
# `name: {taskfile: path.yml}` forms. Optional entries are followed the same:
# absent is reported by the caller, not skipped here.
includes_of() {
    awk '
        /^[^[:space:]#]/ { in_inc = ($0 ~ /^includes:[[:space:]]*$/); next }
        !in_inc { next }
        {
            line = $0
            sub(/#.*/, "", line)
            if (match(line, /taskfile:[[:space:]]*['"'"'"]?[^'"'"'",} ]+/)) {
                path = substr(line, RSTART, RLENGTH)
            } else if (match(line, /:[[:space:]]*['"'"'"]?[^'"'"'",} ]+\.ya?ml/)) {
                path = substr(line, RSTART, RLENGTH)
            } else next
            sub(/^[^:]*:[[:space:]]*/, "", path)
            gsub(/['"'"'"]/, "", path)
            if (path != "") print path
        }
    ' "$1" | sort -u
}

check_taskfile() {
    local taskfile="$1" depth="$2" dir ref target target_ere include resolved

    if [ ! -f "$taskfile" ]; then
        echo "ERROR: Taskfile not found: $taskfile" >&2
        rc=1
        return
    fi
    case " $VISITED " in *" $taskfile "*) return ;; esac
    VISITED="$VISITED $taskfile"

    dir="$(cd "$(dirname "$taskfile")" && pwd)"

    # A while-read over process substitution (NOT `grep | while`, whose subshell
    # would drop rc) keeps the loop in the current shell.
    while IFS= read -r ref; do
        [ -n "$ref" ] || continue
        if [ ! -f "$REPO_ROOT/$ref" ]; then
            echo "ERROR: $(basename "$taskfile") references missing $ref" >&2
            rc=1
        fi
    done < <(grep -oE 'scripts/[A-Za-z0-9_./-]+\.(sh|py)' "$taskfile" | sort -u)

    # go-task fails hard loading a missing dotenv file. Match the bare path (not
    # just a same-line `dotenv:`) so the YAML multi-line list form is caught too.
    for target in $DOTENV_TARGETS; do
        target_ere="${target//./\\.}"
        if grep -qE '(^|[[:space:]"'"'"'-])'"$target_ere"'([[:space:]"'"'"']|$)' "$taskfile" \
            && [ ! -f "$REPO_ROOT/$target" ]; then
            echo "ERROR: Taskfile dotenv target $target is missing" >&2
            rc=1
        fi
    done

    if [ "$depth" -ge "$MAX_DEPTH" ]; then
        echo "ERROR: include depth cap ($MAX_DEPTH) reached at $taskfile" >&2
        rc=1
        return
    fi
    while IFS= read -r include; do
        [ -n "$include" ] || continue
        case "$include" in
            /*) resolved="$include" ;;
            *) resolved="$dir/$include" ;;
        esac
        check_taskfile "$resolved" "$((depth + 1))"
    done < <(includes_of "$taskfile")
}

if [ "$#" -gt 0 ]; then
    for arg in "$@"; do
        check_taskfile "$arg" 0
    done
else
    check_taskfile "$REPO_ROOT/Taskfile.yml" 0
fi

if [ "$rc" -eq 0 ]; then
    echo "OK: all Taskfile-referenced scripts + dotenv targets exist."
fi
exit "$rc"
