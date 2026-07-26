#!/usr/bin/env bash
# Export the postBuild substitution variables from EVERY ConfigMap the Flux
# Kustomizations substitute from. This cluster has two — cluster-versions
# (pinned versions) and cluster-config (domains, VIPs, CIDRs) — while both the
# local lint and the library's flux-lint CI template call a single
# `<script> export-versions <configmap>` entry point. This wrapper is that entry
# point; the per-file parsing stays in the vendored flux-render.sh.
#
#   VARS=$(scripts/flux-env.sh export-versions "$VERSIONS_CM") || exit 1
#   eval "$VARS"        # exports every key plus the merged FLUX_ENVSUBST_VARS
#
# The argument may itself name several ConfigMaps separated by whitespace (the
# CI template passes its `versions_configmap` input through as ONE quoted
# argument), and $FLUX_EXTRA_CONFIGMAPS adds more — set it to the empty string
# to add none. Later files win on a key collision; FLUX_ENVSUBST_VARS is the
# union, and a file named twice is read once.
set -euo pipefail

_SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RENDER="$_SCRIPT_DIR/flux-render.sh"

# `-` not `:-`: an explicitly empty value means "no extras", while an unset one
# means "the usual sibling".
FLUX_EXTRA_CONFIGMAPS="${FLUX_EXTRA_CONFIGMAPS-kubernetes/infrastructure/sources/cluster-config.yaml}"

die() {
    echo "flux-env: ERROR: $*" >&2
    exit 1
}

cmd_export_versions() {
    [ -n "${1:-}" ] || die "usage: $0 export-versions <configmap> [configmap ...]"

    local names="" seen=" " out line key
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
