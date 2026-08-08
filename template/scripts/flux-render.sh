#!/usr/bin/env bash
# Shared Flux render helpers: extract the postBuild substitution variables from
# the cluster-versions ConfigMap and derive the kubeconform schema version.
#
# Callers eval the output so the exports land in the caller's shell (works the
# same under bash and go-task's mvdan/sh interpreter):
#
#   VARS=$(scripts/flux-render.sh export-versions "$CM") || exit 1
#   eval "$VARS"          # exports every data key + FLUX_ENVSUBST_VARS
#   K8S_VER=$(scripts/flux-render.sh k8s-version "$CM")
#
# `export-versions <configmap>` emits `export <key>=<shell-quoted value>` for
# every .data entry, plus `export FLUX_ENVSUBST_VARS='${a} ${b} ...'` (the
# allowlist to hand to envsubst so ONLY known vars are substituted).
# `k8s-version <configmap>` prints MAJOR.MINOR.0 from k3s_version (default 1.36.0).
#
# NOTE: this intentionally does NOT own the per-Kustomization `kustomize build`
# + placeholder-check + kubeconform loop; only the shared extraction lives here.
set -euo pipefail

die() {
    echo "flux-render: ERROR: $*" >&2
    exit 1
}

cmd_export_versions() {
    local cm="${1:-}"
    [ -n "$cm" ] && [ -f "$cm" ] || die "versions ConfigMap not found: ${cm:-<empty>}"
    python3 - "$cm" <<'PY'
import re
import shlex
import sys

import yaml

# Callers `eval` the emitted `export <key>=...`, so every key must be a valid
# POSIX shell variable name — otherwise eval breaks syntax (or worse). The keys
# are repo-controlled, but validate before emitting so a stray key fails loudly.
_VALID_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

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
    print(f"export {key}={shlex.quote(str(value))}")
    names.append(key)
allowlist = "".join(f"${{{key}}} " for key in names)
print(f"export FLUX_ENVSUBST_VARS={shlex.quote(allowlist)}")
PY
}

cmd_k8s_version() {
    local cm="${1:-}"
    [ -n "$cm" ] && [ -f "$cm" ] || die "versions ConfigMap not found: ${cm:-<empty>}"
    local ver
    # Derive MAJOR.MINOR from k3s_version and append .0 so local lint and CI
    # validate against the same kubeconform API schemas. `|| true` keeps the
    # empty-match fallback reachable under `set -eo pipefail`.
    ver=$(grep -E '^ *k3s_version:' "$cm" | head -1 | grep -oE '[0-9]+\.[0-9]+' | head -1 || true)
    printf '%s.0\n' "${ver:-1.36}"
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
