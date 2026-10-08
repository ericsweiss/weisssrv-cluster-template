#!/usr/bin/env bash
# Fail when a Taskfile references a scripts/<name>.{sh,py}, a `dotenv:` target or
# a task that is not on disk, and when a fragment on disk no `includes:` reaches.
# Contract + env: weisssrv-lib docs/SCRIPTS.md - check-taskfile.sh.
set -euo pipefail

_SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$_SCRIPT_DIR/.." && pwd)"
DOTENV_TARGETS="${CHECK_TASKFILE_DOTENV:-scripts/hosts.env}"
MAX_DEPTH="${CHECK_TASKFILE_MAX_DEPTH:-10}"
FRAGMENT_DIR="${CHECK_TASKFILE_FRAGMENT_DIR-taskfiles}"
CHECK_REFS="${CHECK_TASKFILE_REFS:-1}"

rc=0
VISITED=""
DEFINED=""
REFERENCED=""

# Normalised so the cycle guard, the visited set and the orphan scan compare the
# same spelling of a path reached through a `..` include.
abspath() {
    local dir base
    dir="$(dirname "$1")"
    base="$(basename "$1")"
    if [ -d "$dir" ]; then
        printf '%s/%s\n' "$(cd "$dir" && pwd)" "$base"
    else
        printf '%s\n' "$1"
    fi
}

# Entries under an `includes:` block as "namespace<TAB>path", for both the
# `name: path.yml` and `name: {taskfile: path.yml}` forms. Optional entries are
# followed the same: absent is reported by the caller, not skipped here.
includes_of() {
    awk -F'\t' '
        /^[^[:space:]#]/ { hdr = $0; sub(/#.*/, "", hdr); in_inc = (hdr ~ /^includes:[[:space:]]*$/); next }
        !in_inc { next }
        {
            line = $0
            sub(/#.*/, "", line)
            ns = line
            if (!match(ns, /^[[:space:]]*[A-Za-z0-9_.-]+:/)) next
            sub(/^[[:space:]]*/, "", ns)
            sub(/:.*$/, "", ns)
            if (match(line, /taskfile:[[:space:]]*['"'"'"]?[^'"'"'",} ]+/)) {
                path = substr(line, RSTART, RLENGTH)
            } else if (match(line, /:[[:space:]]*['"'"'"]?[^'"'"'",} ]+\.ya?ml/)) {
                path = substr(line, RSTART, RLENGTH)
            } else next
            sub(/^[^:]*:[[:space:]]*/, "", path)
            gsub(/['"'"'"]/, "", path)
            if (path != "") print ns "\t" path
        }
    ' "$1" | sort -u
}

# Task names a Taskfile defines and the task references its `cmds:` and `deps:`
# make, as "T<TAB>name" and "R<TAB>owner<TAB>ref" lines.
tasks_and_refs() {
    awk '
        function strip(s) {
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", s)
            gsub(/^['"'"'"]|['"'"'"]$/, "", s)
            return s
        }
        function emit_list(owner, rest,    n, i, parts, item) {
            gsub(/^\[|\]$/, "", rest)
            n = split(rest, parts, ",")
            for (i = 1; i <= n; i++) {
                item = strip(parts[i])
                if (item != "") print "R\t" owner "\t" item
            }
        }
        {
            line = $0
            sub(/[[:space:]]*#.*$/, "", line)
            if (line ~ /^[[:space:]]*$/) next
            indent = match(line, /[^ ]/) - 1
            if (indent == 0) {
                in_tasks = (line ~ /^tasks:[[:space:]]*$/)
                mode = ""; owner = ""; key_indent = -1
                next
            }
            if (!in_tasks) next
            if (key_indent < 0) key_indent = indent
            if (indent == key_indent && line ~ /^[[:space:]]*[^[:space:]]+:[[:space:]]*$/) {
                owner = line
                sub(/:[[:space:]]*$/, "", owner)
                owner = strip(owner)
                print "T\t" owner
                mode = ""
                next
            }
            if (owner == "") next
            if (mode != "" && indent <= mode_indent && line ~ /^[[:space:]]*[A-Za-z0-9_.-]+:/) mode = ""
            if (line ~ /^[[:space:]]*deps:/) {
                rest = line
                sub(/^[[:space:]]*deps:[[:space:]]*/, "", rest)
                if (rest != "") { emit_list(owner, rest); mode = "" }
                else { mode = "deps"; mode_indent = indent }
                next
            }
            if (line ~ /^[[:space:]]*cmds:/) { mode = "cmds"; mode_indent = indent; next }
            if (mode == "deps") {
                item = line
                if (item ~ /^[[:space:]]*-/) {
                    sub(/^[[:space:]]*-[[:space:]]*/, "", item)
                    if (item ~ /^task:/) {
                        sub(/^task:[[:space:]]*/, "", item)
                        print "R\t" owner "\t" strip(item)
                    } else if (item !~ /:/) {
                        print "R\t" owner "\t" strip(item)
                    }
                } else if (item ~ /^[[:space:]]*task:/) {
                    sub(/^[[:space:]]*task:[[:space:]]*/, "", item)
                    print "R\t" owner "\t" strip(item)
                }
                next
            }
            if (mode == "cmds" && line ~ /^[[:space:]]*-?[[:space:]]*task:/) {
                item = line
                sub(/^[[:space:]]*-?[[:space:]]*task:[[:space:]]*/, "", item)
                print "R\t" owner "\t" strip(item)
            }
        }
    ' "$1"
}

# Resolve one reference the way go-task does: a leading colon is root-relative,
# a bare name inside a namespace resolves within that namespace.
qualify() {
    local ref="$1" ns="$2"
    case "$ref" in
        :*) printf '%s\n' "${ref#:}" ;;
        *) if [ -n "$ns" ]; then printf '%s:%s\n' "$ns" "$ref"; else printf '%s\n' "$ref"; fi ;;
    esac
}

collect_tasks() {
    local taskfile="$1" ns="$2" kind name ref owner
    [ "$CHECK_REFS" = "1" ] || return 0
    while IFS="$(printf '\t')" read -r kind owner ref; do
        case "$kind" in
            T)
                name="$owner"
                [ -n "$ns" ] && name="$ns:$name"
                DEFINED="$DEFINED $name"
                ;;
            R)
                # A templated name is only known at run time.
                case "$ref" in *"{{"*) continue ;; esac
                name="$owner"
                [ -n "$ns" ] && name="$ns:$name"
                REFERENCED="$REFERENCED $(basename "$taskfile")|$name|$(qualify "$ref" "$ns")"
                ;;
        esac
    done < <(tasks_and_refs "$taskfile")
}

check_taskfile() {
    local taskfile="$1" depth="$2" ns="${3:-}" dir ref target target_ere include includes resolved entry
    local child_ns

    if [ ! -f "$taskfile" ]; then
        echo "ERROR: Taskfile not found: $taskfile" >&2
        rc=1
        return
    fi
    taskfile="$(abspath "$taskfile")"
    case " $VISITED " in *" $taskfile "*) return ;; esac
    VISITED="$VISITED $taskfile"
    collect_tasks "$taskfile" "$ns"

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

    # The cap bounds recursion, so it fires only when there is something left
    # to descend into: a leaf reached at the cap terminates the chain itself.
    includes="$(includes_of "$taskfile")"
    if [ -n "$includes" ] && [ "$((depth + 1))" -gt "$MAX_DEPTH" ]; then
        echo "ERROR: include depth cap ($MAX_DEPTH) reached at $taskfile" >&2
        rc=1
        return
    fi
    while IFS= read -r entry; do
        [ -n "$entry" ] || continue
        child_ns="${entry%%	*}"
        include="${entry#*	}"
        [ -n "$ns" ] && child_ns="$ns:$child_ns"
        case "$include" in
            /*) resolved="$include" ;;
            *) resolved="$dir/$include" ;;
        esac
        check_taskfile "$resolved" "$((depth + 1))" "$child_ns"
    done <<EOF
$includes
EOF
}

# A fragment no `includes:` entry reaches is wholly inert: `task --list` omits
# every task in it and no other gate fires.
check_orphan_fragments() {
    local dir="$REPO_ROOT/$FRAGMENT_DIR" fragment
    [ -n "$FRAGMENT_DIR" ] || return 0
    [ -d "$dir" ] || return 0
    for fragment in "$dir"/*.yml "$dir"/*.yaml; do
        [ -f "$fragment" ] || continue
        fragment="$(abspath "$fragment")"
        case " $VISITED " in
            *" $fragment "*) ;;
            *)
                echo "ERROR: ${fragment#"$REPO_ROOT"/} is not reached by any includes: entry" >&2
                rc=1
                ;;
        esac
    done
}

check_task_references() {
    local entry file owner ref
    [ "$CHECK_REFS" = "1" ] || return 0
    for entry in $REFERENCED; do
        file="${entry%%|*}"
        owner="${entry#*|}"
        ref="${owner#*|}"
        owner="${owner%%|*}"
        case " $DEFINED " in
            *" $ref "*) ;;
            *)
                echo "ERROR: $file: task $owner references $ref, which no task defines" >&2
                rc=1
                ;;
        esac
    done
}

if [ "$#" -gt 0 ]; then
    for arg in "$@"; do
        check_taskfile "$arg" 0 ""
    done
else
    check_taskfile "$REPO_ROOT/Taskfile.yml" 0 ""
fi
check_task_references
check_orphan_fragments

if [ "$rc" -eq 0 ]; then
    echo "OK: all Taskfile-referenced scripts, dotenv targets and tasks exist."
fi
exit "$rc"
