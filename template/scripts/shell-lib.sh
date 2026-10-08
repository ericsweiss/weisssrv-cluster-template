#!/usr/bin/env bash
# Shell helpers other scripts source. Function-only: no top-level side effects,
# so sourcing is safe under a caller's `set -e`.
# Contract: weisssrv-lib docs/SCRIPTS.md - CI invariants.

# Run "$@" under a hard wall-clock bound (first arg = seconds). Prefers
# `timeout`, then `gtimeout`; with neither present the command runs unbounded,
# guarded only by the caller's own ssh timeouts.
timeout_cmd() {
    local seconds="$1"
    shift
    if command -v timeout >/dev/null 2>&1; then
        timeout "$seconds" "$@"
    elif command -v gtimeout >/dev/null 2>&1; then
        gtimeout "$seconds" "$@"
    else
        # Warn once per shell: losing the wall-clock backstop silently turns a
        # bounded probe into an indefinite hang, with nothing to tell the
        # operator it happened.
        if [ -z "${_SHELL_LIB_TIMEOUT_WARNED:-}" ]; then
            echo "warning: neither timeout(1) nor gtimeout(1) found — probes run UNBOUNDED (macOS: brew install coreutils)" >&2
            _SHELL_LIB_TIMEOUT_WARNED=1
        fi
        "$@"
    fi
}

# SSH reachability probe under a wall-clock backstop. ConnectTimeout bounds the
# TCP connect, ServerAlive* trips a dead channel, timeout_cmd catches a host
# that connects then stalls. Usage: ssh_probe "$host" "true".
ssh_probe() {
    timeout_cmd 6 ssh -o ConnectTimeout=2 -o BatchMode=yes \
        -o ServerAliveInterval=2 -o ServerAliveCountMax=2 "$@"
}

# CRITICAL: `kubectl get … 2>/dev/null || true` collapses an absent object, no
# cluster, no RBAC and an API timeout into the same empty string, so the caller
# prints a remedy for the wrong problem. This reads one object as JSON and
# separates them: 0 with the JSON on stdout, 3 on NotFound, 1 with kubectl's
# stderr otherwise. Usage: json="$(kubectl_read secret x -n ns)" || rc=$?
kubectl_read() {
    local out
    if out="$(kubectl get "$@" -o json 2>&1)"; then
        printf '%s' "$out"
        return 0
    fi
    case "$out" in
        *NotFound*|*"not found"*) return 3 ;;
    esac
    printf '%s\n' "$out" >&2
    return 1
}

# CRITICAL: `cmd | grep -q` under pipefail inverts a match into a failure —
# grep exits on the first hit, the producer takes SIGPIPE, and pipefail returns
# its status. Capture first, then test the value. Usage:
#   out=$(ssh "$h" "systemctl is-active x") || return 1
#   captured_match "$out" "^active"
captured_match() {
    printf '%s' "$1" | grep -q -- "$2"
}

# url_contains <url> <pattern>: fetch a URL and test its body against a BRE.
# Non-zero when the fetch failed, so an unreachable endpoint is a FAIL rather
# than an empty body. SHELL_LIB_CURL_MAX_TIME bounds the fetch.
url_contains() {
    local out
    out=$(curl -s --max-time "${SHELL_LIB_CURL_MAX_TIME:-10}" "$1" 2>/dev/null) || return 1
    captured_match "$out" "$2"
}

# ssh_contains <user@host> <command> <pattern>: run a command over ssh and test
# its output against a BRE. Non-zero when the command or the connection failed,
# so an unreachable host does not read as a clean non-match.
ssh_contains() {
    local out
    out=$(ssh_probe "$1" "$2" 2>/dev/null) || return 1
    captured_match "$out" "$3"
}
