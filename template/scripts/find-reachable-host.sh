#!/usr/bin/env bash
# Print the first reachable SSH target from the arguments (each optionally
# user@-prefixed), or exit 1 if none respond. For task wrappers that need any
# one cluster entry point — pass the candidates in preference order.
#
# Usage: find-reachable-host.sh <ssh-target> [<ssh-target> ...]
#   find-reachable-host.sh user@10.0.0.10 user@10.0.0.11
#   find-reachable-host.sh host-a host-b     # ssh-config resolves the user
set -euo pipefail

_SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=scripts/shell-lib.sh
. "$_SCRIPT_DIR/shell-lib.sh"

if [ "$#" -lt 1 ]; then
    echo "Usage: $0 <ssh-target> [ssh-target ...]" >&2
    exit 2
fi

for target in "$@"; do
    if ssh_probe "$target" "true" 2>/dev/null; then
        echo "$target"
        exit 0
    fi
done

echo "ERROR: no reachable SSH target in: $*" >&2
exit 1
