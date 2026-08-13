#!/usr/bin/env bash
# Shared shell helpers sourced by repo scripts. Function-only: NO top-level
# side effects, so sourcing is safe even under a caller's `set -e`.
#
# Source via the _SCRIPT_DIR pattern:
#   _SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
#   # shellcheck source=scripts/shell-lib.sh
#   . "$_SCRIPT_DIR/shell-lib.sh"

# Run "$@" under a hard wall-clock bound (first arg = seconds). Prefers GNU
# coreutils `timeout`, then `gtimeout` (macOS: brew install coreutils), and
# finally — if neither exists — runs the command unbounded so callers still
# work (their own ssh ConnectTimeout/ServerAlive* options are the only guard
# on that fallback path).
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

# SSH reachability probe: short-timeout, keepalive-bounded ssh under a
# wall-clock backstop. ConnectTimeout=2 bounds the TCP connect; ServerAlive*
# trips a dead post-connect channel; timeout_cmd is the backstop for a host that
# connects then stalls (PAM/sssd, disk-stuck remote shell). Pass the target and
# remote command as args, e.g. `ssh_probe "$host" "true"`.
ssh_probe() {
    timeout_cmd 6 ssh -o ConnectTimeout=2 -o BatchMode=yes \
        -o ServerAliveInterval=2 -o ServerAliveCountMax=2 "$@"
}
