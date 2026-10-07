#!/usr/bin/env bash
# Read one or more keys out of the cluster-config ConfigMap, the cluster's
# identity source of truth for domains, CIDRs and VIPs.

#   cluster-config-value.sh cluster_k3s_api_vip
#   CLUSTER_CONFIG=path/to/cluster-config.yaml cluster-config-value.sh a b

# Prints one value per key, space-separated, and fails if any key is absent: an
# empty value silently becomes a no-op sed or an empty probe list.

set -euo pipefail

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLUSTER_CONFIG="${CLUSTER_CONFIG:-$_SCRIPT_DIR/../kubernetes/infrastructure/sources/cluster-config.yaml}"

if [ $# -eq 0 ]; then
    echo "usage: $(basename "$0") <key> [key...]" >&2
    exit 2
fi
if [ ! -f "$CLUSTER_CONFIG" ]; then
    echo "ERROR: $CLUSTER_CONFIG not found" >&2
    exit 2
fi

out=""
for key in "$@"; do
    # Only the `data:` scalars are of this shape; quotes are optional in YAML, so
    # both spellings are accepted and neither is emitted.
    value=$(sed -n "s/^[[:space:]]*${key}:[[:space:]]*[\"']\{0,1\}\([^\"']*\)[\"']\{0,1\}[[:space:]]*$/\1/p" \
        "$CLUSTER_CONFIG" | head -1)
    if [ -z "$value" ]; then
        echo "ERROR: $key is not set in $CLUSTER_CONFIG" >&2
        exit 1
    fi
    out="${out:+$out }$value"
done
printf '%s\n' "$out"
