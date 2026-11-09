#!/usr/bin/env bash
# A consolidated gate job sources this driver. run_check runs one check and
# records a failure without stopping, so one red check never hides the rest.
# Contract: weisssrv-lib docs/SCRIPTS.md - ci-run-check.sh.

# Sourced, so no `set -e` here; the job sets `set -euo pipefail` itself and
# run_check restores errexit after every check.
overall=0
failed=""

run_check() {
    local name="$1"
    local rc=0
    shift
    # Collapsible log sections on GitLab, a plain heading elsewhere.
    if [ -n "${GITLAB_CI:-}" ]; then
        printf 'section_start:%s:%s[collapsed=true]\r\033[0K== %s\n' "$(date +%s)" "$name" "$name"
    else
        printf '== %s\n' "$name"
    fi
    # The check runs outside any AND-OR list, so its own `set -e` fires and a
    # helper that fails mid-body fails the check. errexit is off only around
    # the subshell, so the job records the status and carries on.
    set +e
    ( set -eo pipefail; "$@" )
    rc=$?
    set -e
    [ -z "${GITLAB_CI:-}" ] || printf 'section_end:%s:%s\r\033[0K\n' "$(date +%s)" "$name"
    if [ "$rc" -ne 0 ]; then
        overall=1
        failed="$failed $name"
        echo "FAILED: $name (rc=$rc)"
    fi
}

# Name every failed check and return the job's exit status. Usage:
# `run_check_summary "sync checks"; exit $?` as the job's last script line.
run_check_summary() {
    [ "$overall" -eq 0 ] || echo "Failed ${1:-checks}:$failed"
    return "$overall"
}
