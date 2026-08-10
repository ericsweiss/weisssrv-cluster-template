#!/usr/bin/env bash
# Shared Flux render helpers: extract the postBuild substitution variables from
# the cluster-versions ConfigMap and derive the kubeconform schema version. It
# does NOT own the per-Kustomization build + kubeconform loop.
#
# Callers eval the output so the exports land in the caller's shell (works the
# same under bash and go-task's mvdan/sh interpreter):
#
#   VARS=$(scripts/flux-render.sh export-versions "$CM") || exit 1
#   eval "$VARS"          # exports every .data key + FLUX_ENVSUBST_VARS
#   K8S_VER=$(scripts/flux-render.sh k8s-version "$CM")
set -euo pipefail

die() {
    echo "flux-render: ERROR: $*" >&2
    exit 1
}

cmd_export_versions() {
    local cm="${1:-}"
    [ -n "$cm" ] && [ -f "$cm" ] || die "versions ConfigMap not found: ${cm:-<empty>}"
    python3 - "$cm" <<'PYEOF'
import re
import shlex
import sys

import yaml

# Callers `eval` the emitted `export <key>=...`, so every key must be a valid
# POSIX shell variable name — otherwise eval breaks syntax (or worse).
_VALID_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Names the calling shell owns: exporting one of these would clobber the
# caller's own variable at eval time.
_RESERVED = {
    "PATH", "HOME", "IFS", "PWD", "SHELL", "TMPDIR", "CI",
    "CLUSTER_DIR", "SKIPPED_SCRIPT", "FLUX_RENDER_SCRIPT", "VERSIONS_CONFIGMAP",
    "FAILED", "RENDER_ALL", "K8S_VER", "VARS", "FLUX_ENVSUBST_VARS",
}

cm = sys.argv[1]
with open(cm) as f:
    doc = yaml.safe_load(f)
data = (doc or {}).get("data") or {}
if not data:
    sys.exit(f"flux-render: ERROR: no version keys found in {cm}")
names = []
for key, value in data.items():
    if not _VALID_KEY.match(key):
        sys.exit(f"flux-render: ERROR: invalid shell variable name in {cm}: {key}")
    if key in _RESERVED or key.endswith("_SHA256"):
        sys.exit(f"flux-render: ERROR: reserved variable name in {cm}: {key}")
    print(f"export {key}={shlex.quote(str(value))}")
    names.append(key)
allowlist = "".join(f"${{{key}}} " for key in names)
print(f"export FLUX_ENVSUBST_VARS={shlex.quote(allowlist)}")
PYEOF
}

cmd_k8s_version() {
    local cm="${1:-}"
    [ -n "$cm" ] && [ -f "$cm" ] || die "versions ConfigMap not found: ${cm:-<empty>}"
    local ver
    # MAJOR.MINOR from k3s_version plus .0, so local lint and CI validate
    # against the same kubeconform API schemas. Read through the same YAML
    # loader as export-versions, so both subcommands agree on the ConfigMap.
    ver=$(python3 - "$cm" <<'PYEOF'
import re
import sys

import yaml

cm = sys.argv[1]
with open(cm) as f:
    doc = yaml.safe_load(f)
raw = str(((doc or {}).get("data") or {}).get("k3s_version", ""))
m = re.search(r"(\d+)\.(\d+)", raw)
print(f"{m.group(1)}.{m.group(2)}" if m else "")
PYEOF
)
    # No silent default: a missing or renamed k3s_version key would otherwise
    # drift local lint and CI onto different schemas with nothing red.
    [ -n "$ver" ] || die "could not derive k3s_version from ${cm} (expected a 'k3s_version: X.Y.Z' data key) — run 'task flux:sync-versions'"
    printf '%s.0\n' "$ver"
}

main() {
    local sub="${1:-}"
    shift || true
    case "$sub" in
        export-versions) cmd_export_versions "$@" ;;
        k8s-version) cmd_k8s_version "$@" ;;
        *) die "unknown subcommand: ${sub:-<none>} (want: export-versions|k8s-version)" ;;
    esac
}

main "$@"
