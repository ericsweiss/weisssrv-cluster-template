#!/usr/bin/env bash
# Print the first reachable SSH target from the arguments, or exit 1 if none
# respond. Targets may be user@-prefixed; pass them in preference order.
# Usage: find-reachable-host.sh <ssh-target> [<ssh-target> ...]
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
