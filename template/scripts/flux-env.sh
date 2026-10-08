#!/usr/bin/env bash
# Multi-ConfigMap front end to flux-render.sh, which keeps the per-file parsing.
# Usage: VARS=$(scripts/flux-env.sh export-versions "$CMS"); eval "$VARS"
# Contract + env: weisssrv-lib docs/SCRIPTS.md - flux-env.sh.
set -euo pipefail

_SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RENDER="$_SCRIPT_DIR/flux-render.sh"

# `-` not `:-`: an explicitly empty value means "no extras", while an unset one
# means the conventional sibling cluster-config.yaml.
FLUX_EXTRA_CONFIGMAPS="${FLUX_EXTRA_CONFIGMAPS-kubernetes/infrastructure/sources/cluster-config.yaml}"

die() {
    echo "flux-env: ERROR: $*" >&2
    exit 1
}

cmd_export_versions() {
    [ -n "${1:-}" ] || die "usage: $0 export-versions <configmap> [configmap ...]"

    local names="" seen=" " named=" " out line key
    # shellcheck disable=SC2048,SC2086  # both lists are deliberately word-split
    for cm in $* $FLUX_EXTRA_CONFIGMAPS; do
        case "$seen" in *" $cm "*) continue ;; esac
        seen="$seen$cm "
        out=$("$RENDER" export-versions "$cm") || exit 1
        while IFS= read -r line; do
            [ -n "$line" ] || continue
            # Drop each file's own allowlist; one merged allowlist is emitted below.
            case "$line" in "export FLUX_ENVSUBST_VARS="*) continue ;; esac
            printf '%s\n' "$line"
            key=${line#export }
            key=${key%%=*}
            # The allowlist is the union: a key defined in two ConfigMaps has
            # two export lines (last wins) but one entry here.
            case "$named" in *" $key "*) continue ;; esac
            named="$named$key "
            names="${names}\${${key}} "
        done <<EOF
$out
EOF
    done
    # flux-render.sh already rejected any key that is not a shell identifier, so
    # the accumulated list cannot contain a quote and single-quoting is safe.
    printf "export FLUX_ENVSUBST_VARS='%s'\n" "$names"
}

cmd_k8s_version() {
    [ -n "${1:-}" ] || die "usage: $0 k8s-version <configmap>"
    # The k3s pin lives in the first ConfigMap; word-split so a multi-file
    # argument passes only that one through.
    # shellcheck disable=SC2048,SC2086
    set -- $*
    "$RENDER" k8s-version "$1"
}

# Print ONE ConfigMap whose .data is the union of every input file's, for tools
# that accept a single --versions-configmap (validate-helm-values.py). Same file
# list and precedence as export-versions, so the two can never disagree.
cmd_merged_configmap() {
    [ -n "${1:-}" ] || die "usage: $0 merged-configmap <configmap> [configmap ...]"

    local files="" seen=" "
    # shellcheck disable=SC2048,SC2086  # both lists are deliberately word-split
    for cm in $* $FLUX_EXTRA_CONFIGMAPS; do
        case "$seen" in *" $cm "*) continue ;; esac
        seen="$seen$cm "
        [ -f "$cm" ] || die "ConfigMap not found: $cm"
        files="$files $cm"
    done
    # shellcheck disable=SC2086  # the accumulated list is deliberately word-split
    python3 - $files <<'PY'
import sys
import yaml

data = {}
for path in sys.argv[1:]:
    with open(path) as fh:
        doc = yaml.safe_load(fh) or {}
    entries = doc.get("data") or {}
    if not entries:
        sys.exit(f"flux-env: ERROR: no substitution keys found in {path}")
    data.update({k: str(v) for k, v in entries.items()})
yaml.safe_dump(
    {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "merged-substitutions"},
        "data": data,
    },
    sys.stdout,
    sort_keys=True,
)
PY
}

main() {
    local sub="${1:-}"
    shift || true
    case "$sub" in
        export-versions) cmd_export_versions "$@" ;;
        k8s-version) cmd_k8s_version "$@" ;;
        merged-configmap) cmd_merged_configmap "$@" ;;
        *) die "unknown subcommand: ${sub:-<none>} (want: export-versions|k8s-version|merged-configmap)" ;;
    esac
}

main "$@"
