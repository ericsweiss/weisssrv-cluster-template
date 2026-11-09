#!/usr/bin/env bash
# Shell helpers other scripts source. Function-only: no top-level side effects,
# so sourcing is safe under a caller's `set -e`.
# Contract: weisssrv-lib docs/SCRIPTS.md - Shell helpers.

# Warn once per shell when neither timeout(1) nor gtimeout(1) exists. A helper
# that captures a probe calls it before the capture, so the warning reaches
# stderr and the guard persists in the caller's shell.
shell_lib_warn_unbounded() {
    if command -v timeout >/dev/null 2>&1 || command -v gtimeout >/dev/null 2>&1; then
        return 0
    fi
    if [ -z "${_SHELL_LIB_TIMEOUT_WARNED:-}" ]; then
        echo "warning: neither timeout(1) nor gtimeout(1) found — probes run UNBOUNDED (macOS: brew install coreutils)" >&2
        _SHELL_LIB_TIMEOUT_WARNED=1
    fi
}

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
        shell_lib_warn_unbounded
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
