#!/usr/bin/env bash
# Read one or more keys out of the cluster-config ConfigMap, the cluster's
# identity source of truth for domains, CIDRs and VIPs.

# Usage:
#   cluster-config-value.sh cluster_k3s_api_vip
#   CLUSTER_CONFIG=path/to/cluster-config.yaml cluster-config-value.sh a b

# Prints one value per key, space-separated, and fails if any key is absent: an
# empty value silently becomes a no-op sed or an empty probe list. Only the
# `data:` scalars are read.

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

python3 - "$CLUSTER_CONFIG" "$@" <<'PYEOF'
import sys

import yaml

path = sys.argv[1]
with open(path) as f:
    doc = yaml.safe_load(f)
data = (doc or {}).get("data") or {}
values = []
for key in sys.argv[2:]:
    value = data.get(key)
    if value is None or str(value) == "":
        sys.exit(f"ERROR: {key} is not set in {path}")
    values.append(str(value))
print(" ".join(values))
PYEOF
