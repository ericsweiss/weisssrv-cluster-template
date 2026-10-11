#!/usr/bin/env bash
# The render/scan/kubeconform program behind `ci/validate/flux-lint.yml` and a
# consumer's own `flux:lint` task, so both run one implementation.
# Flags + contract: weisssrv-lib docs/SCRIPTS.md - flux-lint.sh.
set -eo pipefail

# Flag state carries the `FL_` prefix: `eval`ing the render script's exports
# below would otherwise let a ConfigMap key overwrite one of these.
FL_MODE="substitute"
FL_CLUSTER_DIR=""
FL_KUSTOMIZE_PATH=""
FL_VERSIONS_CM=""
FL_RENDER_SCRIPT="scripts/flux-render.sh"
FL_SKIPPED_SCRIPT="scripts/kubeconform-skipped.py"
FL_EXPECTED_SKIPPED=""
FL_CHILD_KUSTOMIZATIONS=""
FL_RENDER_ALL=""
FL_K8S_VERSION=""
FL_CATALOG=""
FL_CATALOG_REF=""
FL_ALLOWED_SKIPS="0"
FL_REQUIRE_CLUSTER_ROOT="true"
FL_CHECK_FLUX_VERSION="true"

usage() {
    cat <<'USAGE'
Usage: flux-lint.sh [--mode substitute|simple] [options]

Substitute mode (a cluster repo; needs flux, kustomize, kubeconform, envsubst):
  --cluster-dir DIR            dir of Flux Kustomizations to iterate (required)
  --versions-configmap FILE    ConfigMap of substitution keys (required)
  --render-script FILE         flux-render.sh / flux-env.sh (default scripts/flux-render.sh)
  --skipped-script FILE        kubeconform-skipped.py (default scripts/kubeconform-skipped.py)
  --expected-skipped FILE      baseline of apiVersion/Kind pairs with no schema
  --child-kustomizations FILE  flux-child-kustomizations.py, for dependsOn order
  --render-all FILE            accumulate the rendered corpus here (caller-owned)
  --require-cluster-root BOOL  build the cluster root (default true)
  --check-flux-version BOOL    hold the flux CLI equal to the ConfigMap (default true)

Simple mode (a tenant repo; needs kustomize, kubeconform):
  --kustomize-path PATH        the ONE directory to build (required)
  --allowed-skips N            resources kubeconform may validate against no schema

Both modes:
  --k8s-version VER            kubeconform target; substitute mode derives it otherwise
  --crd-catalog URL            kubeconform -schema-location template
  --crd-catalog-ref REF        datreeio/CRDs-catalog ref the default template uses
USAGE
}

die() {
    echo "flux-lint: ERROR: $*" >&2
    exit 2
}

need_value() {
    [ -n "$2" ] || die "$1 needs a value"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --mode) need_value "$1" "${2:-}"; FL_MODE="$2"; shift 2 ;;
        --cluster-dir) need_value "$1" "${2:-}"; FL_CLUSTER_DIR="$2"; shift 2 ;;
        --kustomize-path) need_value "$1" "${2:-}"; FL_KUSTOMIZE_PATH="$2"; shift 2 ;;
        --versions-configmap) need_value "$1" "${2:-}"; FL_VERSIONS_CM="$2"; shift 2 ;;
        --render-script) need_value "$1" "${2:-}"; FL_RENDER_SCRIPT="$2"; shift 2 ;;
        --skipped-script) need_value "$1" "${2:-}"; FL_SKIPPED_SCRIPT="$2"; shift 2 ;;
        # An empty value is how a caller passes an unset template input through.
        --expected-skipped) FL_EXPECTED_SKIPPED="${2:-}"; shift 2 ;;
        --child-kustomizations) FL_CHILD_KUSTOMIZATIONS="${2:-}"; shift 2 ;;
        --render-all) FL_RENDER_ALL="${2:-}"; shift 2 ;;
        --k8s-version) FL_K8S_VERSION="${2:-}"; shift 2 ;;
        --crd-catalog) FL_CATALOG="${2:-}"; shift 2 ;;
        --crd-catalog-ref) FL_CATALOG_REF="${2:-}"; shift 2 ;;
        --allowed-skips) FL_ALLOWED_SKIPS="${2:-}"; shift 2 ;;
        --require-cluster-root) need_value "$1" "${2:-}"; FL_REQUIRE_CLUSTER_ROOT="$2"; shift 2 ;;
        --check-flux-version) need_value "$1" "${2:-}"; FL_CHECK_FLUX_VERSION="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown argument: $1" ;;
    esac
done

# The catalog is pinned to a ref so a catalog rewrite cannot change what the
# gate accepts. A caller may pass the whole template instead, to self-host.
if [ -z "$FL_CATALOG" ]; then
    [ -n "$FL_CATALOG_REF" ] || die "--crd-catalog or --crd-catalog-ref is required"
    FL_CATALOG="https://raw.githubusercontent.com/datreeio/CRDs-catalog/${FL_CATALOG_REF}/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
fi
FL_CATALOG_LABEL="${FL_CATALOG_REF:-$FL_CATALOG}"

run_simple_mode() {
    [ -n "$FL_KUSTOMIZE_PATH" ] || die "--kustomize-path is required in simple mode"
    local k8s_ver="${FL_K8S_VERSION:-1.36.0}"
    local rendered summary skipped
    rendered=$(mktemp)
    # shellcheck disable=SC2064  # expand the path now, not at trap time
    trap "rm -f '$rendered'" EXIT

    echo "=== kustomize build $FL_KUSTOMIZE_PATH ==="
    kustomize build "$FL_KUSTOMIZE_PATH" > "$rendered"
    # CRITICAL: an emptied or mistyped `resources:` list renders nothing and
    # both tools exit 0 on it, while the cluster-side Kustomization's prune
    # then deletes every object this repo applied.
    if ! grep -q '^kind:' "$rendered"; then
        echo "ERROR: kustomize build $FL_KUSTOMIZE_PATH rendered no document — check for an emptied or mistyped resources: list"
        return 1
    fi

    echo "=== kubeconform (k8s $k8s_ver) ==="
    # -ignore-missing-schemas lets platform CRDs validate against the datreeio
    # catalog; 2>&1 because per-resource failures go to stderr; stdin because
    # kubeconform parses a file by its extension and reads a bare name as none.
    if ! summary=$(kubeconform -strict -ignore-missing-schemas \
            -kubernetes-version "$k8s_ver" \
            -schema-location default \
            -schema-location "$FL_CATALOG" \
            -summary < "$rendered" 2>&1); then
        printf '%s\n' "$summary"
        return 1
    fi
    printf '%s\n' "$summary"

    # CRITICAL: -ignore-missing-schemas exits 0 on a resource it could not
    # schema-validate, so an unreachable catalog degrades the run to core
    # kinds in silence. A summary with no Skipped: field is that signature.
    case "$FL_ALLOWED_SKIPS" in
        ''|*[!0-9]*) echo "ERROR: allowed_skips must be a non-negative integer, got '$FL_ALLOWED_SKIPS'"; return 1 ;;
    esac
    skipped=$(printf '%s\n' "$summary" | sed -n 's/.*Skipped: \([0-9][0-9]*\).*/\1/p')
    if [ -z "$skipped" ] || [ "$skipped" -gt "$FL_ALLOWED_SKIPS" ]; then
        echo "ERROR: kubeconform validated ${skipped:-?} resource(s) against no schema (allowed_skips=$FL_ALLOWED_SKIPS)"
        echo "The catalog at $FL_CATALOG_LABEL moved or was unreachable, or the kind has no published schema."
        echo "Add a -schema-location for it, or raise allowed_skips."
        return 1
    fi
    return 0
}

# Writes `name<TAB>spec.path` per stage into $1, keeping its triage on stdout.
# The child-kustomizations enumerator adds dependsOn order; the directory scan
# needs no second vendored file, so it is the default.
enumerate_stages() {
    local out="$1"
    if [ -n "$FL_CHILD_KUSTOMIZATIONS" ]; then
        python3 "$FL_CHILD_KUSTOMIZATIONS" --paths --dir "$FL_CLUSTER_DIR" > "$out"
        return
    fi
    local ks name meta label srcpath
    for ks in "$FL_CLUSTER_DIR"/*.yaml "$FL_CLUSTER_DIR"/*.yml; do
        [ -f "$ks" ] || continue
        name=$(basename "$ks")
        name="${name%.yaml}"
        name="${name%.yml}"
        [ "$name" = "kustomization" ] && continue
        if ! meta=$(classify_document "$ks"); then
            echo "ERROR: failed to read $ks"
            return 1
        fi
        label=$(printf '%s\n' "$meta" | sed -n 1p)
        srcpath=$(printf '%s\n' "$meta" | sed -n 2p)
        if [ "$label" != "Kustomization" ]; then
            echo "SKIP: $ks is a $label, not a Flux Kustomization"
            continue
        fi
        if [ -z "$srcpath" ]; then
            # Flux reconciles the repository root when spec.path is absent,
            # which this gate cannot build; the path is required, not skipped.
            echo "ERROR: $ks is a Flux Kustomization with no spec.path: spell the path it reconciles"
            return 1
        fi
        printf '%s\t%s\n' "$ks" "$srcpath" >> "$out"
    done
}

# A cluster dir also holds non-Kustomization documents (GitRepository,
# Secret); only a Flux Kustomization has a spec.path to build, and the
# apiVersion tells one apart from a kustomize.config kustomization.yaml.
classify_document() {
    python3 -c '
import sys

import yaml

with open(sys.argv[1]) as f:
    docs = [d for d in yaml.safe_load_all(f) if isinstance(d, dict)]
flux = [d for d in docs if d.get("kind") == "Kustomization"
        and str(d.get("apiVersion", "")).startswith("kustomize.toolkit")]
if len(flux) > 1:
    sys.exit("%s holds %d Flux Kustomizations; give each one its own file"
             % (sys.argv[1], len(flux)))
if flux:
    doc, label = flux[0], "Kustomization"
elif docs:
    doc = docs[0]
    label = "%s %s" % (doc.get("apiVersion") or "?", doc.get("kind") or "?")
else:
    doc, label = {}, "empty document"
print(label)
print((doc.get("spec") or {}).get("path") or "")
' "$1"
}

# CRITICAL: `envsubst --strict` is authoritative only while the CLI is the
# release kustomize-controller runs, and the ConfigMap is where the cluster
# states that release.
check_flux_cli() {
    local cli
    cli=$(flux --version | awk '{print $NF}')
    if [ -n "${flux_version:-}" ]; then
        if [ "$cli" != "${flux_version#v}" ]; then
            echo "ERROR: flux CLI $cli != $FL_VERSIONS_CM flux_version ${flux_version#v}"
            echo "The strict parser must be the cluster's own: move the flux_version/flux_sha256 pins onto ${flux_version#v}."
            return 1
        fi
        echo "flux CLI $cli matches the versions-configmap flux_version"
    else
        echo "NOTE: $FL_VERSIONS_CM declares no flux_version, so the flux CLI pin ($cli) is held equal to nothing"
    fi
    return 0
}

# Build the cluster root to catch a malformed top-level kustomization.
# Both gotk files absent is the pre-bootstrap state and skips. `-e || -L`,
# not `-f`, so a dangling symlink cannot read as pre-bootstrap.
check_cluster_root() {
    local gotk_components="$FL_CLUSTER_DIR/flux-system/gotk-components.yaml"
    local gotk_sync="$FL_CLUSTER_DIR/flux-system/gotk-sync.yaml"
    # CRITICAL: check the shape of the tree first, independent of bootstrap
    # state. The -L tests below inspect only each path's final component, so
    # containment is asserted on the resolved path too. Otherwise an
    # intermediate symlink hands kustomize a tree outside the repo.
    local repo_root cluster_real contained=0
    repo_root=$(pwd -P)
    cluster_real=$(readlink -f "$FL_CLUSTER_DIR" 2>/dev/null || echo "")
    case "${cluster_real:-/nonexistent}/" in
        "$repo_root"/*) contained=1 ;;
    esac
    if [ "$contained" != "1" ]; then
        echo "ERROR: $FL_CLUSTER_DIR resolves to ${cluster_real:-nothing}, which is outside the repository at $repo_root (symlinked path component?)"
        return 1
    fi
    if [ ! -d "$FL_CLUSTER_DIR" ] || [ -L "$FL_CLUSTER_DIR" ] \
        || [ ! -d "$FL_CLUSTER_DIR/flux-system" ] \
        || [ -L "$FL_CLUSTER_DIR/flux-system" ] \
        || [ ! -f "$FL_CLUSTER_DIR/kustomization.yaml" ] \
        || [ -L "$FL_CLUSTER_DIR/kustomization.yaml" ] \
        || [ ! -f "$FL_CLUSTER_DIR/flux-system/kustomization.yaml" ] \
        || [ -L "$FL_CLUSTER_DIR/flux-system/kustomization.yaml" ]; then
        echo "ERROR: $FL_CLUSTER_DIR and its flux-system/ must be real directories, and both hand-authored kustomization.yaml files must be regular files (not symlinks)"
        return 1
    fi
    if [ ! -e "$gotk_components" ] && [ ! -L "$gotk_components" ] \
        && [ ! -e "$gotk_sync" ] && [ ! -L "$gotk_sync" ] \
        && [ "$FL_REQUIRE_CLUSTER_ROOT" != "true" ]; then
        echo "=== Skipping cluster root $FL_CLUSTER_DIR: neither flux-system/gotk-components.yaml nor gotk-sync.yaml exists (flux bootstrap writes both); set require_cluster_root: true to build anyway ==="
        return 0
    fi
    echo "=== Building cluster root $FL_CLUSTER_DIR ==="
    if ! kustomize build "$FL_CLUSTER_DIR" >/dev/null; then
        echo "ERROR: cluster root kustomization failed to build"
        # Name the one cause the consumer cannot deduce: a repo that has never
        # been bootstrapped has no gotk files for its kustomization to resolve,
        # and the fix is an input, not a code change.
        if [ ! -e "$gotk_components" ] && [ ! -L "$gotk_components" ] \
            && [ ! -e "$gotk_sync" ] && [ ! -L "$gotk_sync" ]; then
            echo "HINT: neither flux-system/gotk-components.yaml nor gotk-sync.yaml exists, so this repo has not been bootstrapped yet."
            echo "HINT: pass require_cluster_root: false to this template until \`flux bootstrap\` has run."
        fi
        return 1
    fi
    return 0
}

# One stage: build, scan the pre-substitution text, substitute, validate.
# Appends the rendered documents to the corpus every aggregate check reads.
lint_stage() {
    local label="$1" srcpath="$2" raw malformed missing empty ref varname
    local strict_err rendered repo_root src_real
    echo "=== Building + validating $label (path $srcpath) ==="
    # Flux reconciles paths relative to the repository root, so a stage that
    # resolves outside it (absolute, `..`, a symlink out) is not this cluster.
    repo_root=$(pwd -P)
    case "$srcpath" in
        /*) echo "ERROR: $label has an absolute spec.path ($srcpath); Flux paths are relative to the repository"; return 1 ;;
    esac
    if [ -d "$srcpath" ]; then
        src_real=$(cd "$srcpath" && pwd -P)
        case "$src_real/" in
            "$repo_root"/*) ;;
            *) echo "ERROR: $label spec.path $srcpath resolves to $src_real outside the repository at $repo_root"; return 1 ;;
        esac
    fi
    if ! raw=$(kustomize build "$srcpath" 2>&1); then
        echo "ERROR: kustomize build failed for $srcpath:"
        printf '%s\n' "$raw"
        return 1
    fi
    # printf, never echo: a leading `-n` reads as an option and dashboard JSON
    # carries literal \n that echo expands. `$${` is Flux's escape, masked out
    # of every scan below.
    local scan
    scan=$(printf '%s\n' "$raw" | sed 's/\$\$[{]/__FLUX_ESCAPED__/g')
    malformed=$(printf '%s\n' "$scan" | grep -nE '\$\{[^A-Za-z_]' || true)
    if [ -n "$malformed" ]; then
        echo "ERROR: $srcpath renders a \${ sequence Flux's envsubst mishandles (escape as \$\${ or assemble at runtime):"
        printf '%s\n' "$malformed" | head -5 | sed 's/^/  /'
        return 1
    fi
    missing=""
    empty=""
    for ref in $(printf '%s\n' "$scan" | grep -oE '\$\{[A-Za-z_][A-Za-z0-9_]*\}' | sort -u); do
        varname=${ref#\$\{}
        varname=${varname%\}}
        if [ -z "${!varname+x}" ]; then
            missing="$missing $varname"
        elif [ -z "${!varname}" ]; then
            empty="$empty $varname"
        fi
    done
    missing=$(printf '%s\n' "$missing" | tr ' ' '\n' | grep -v '^$' | sort -u || true)
    if [ -n "$missing" ]; then
        echo "ERROR: placeholders in $srcpath reference keys not in versions-configmap:"
        printf '%s\n' "$missing" | sed 's/^/  - /'
        return 1
    fi
    # A key present with an EMPTY value is its own failure: envsubst renders
    # `image: foo:${x}` as `foo:`, which yamllint and kubeconform both pass,
    # so the broken reference only shows up in-cluster.
    empty=$(printf '%s\n' "$empty" | tr ' ' '\n' | grep -v '^$' | sort -u || true)
    if [ -n "$empty" ]; then
        echo "ERROR: $srcpath references keys that are present but EMPTY in the substitution ConfigMap(s):"
        printf '%s\n' "$empty" | sed 's/^/  - /'
        echo "  Give each one a value, or drop the key and the reference together."
        return 1
    fi
    # CRITICAL: GNU envsubst below reads only `${NAME}`, while Flux's Go
    # envsubst also reads bash modifiers (`${conf%/*}`) as variables. Its
    # own --strict is the authority, or the post-build fails in-cluster and
    # that Kustomization stops reconciling.
    if ! strict_err=$(printf '%s\n' "$raw" | flux envsubst --strict 2>&1 >/dev/null); then
        echo "ERROR: $srcpath fails Flux's strict substitution: $strict_err"
        return 1
    fi
    rendered=$(printf '%s\n' "$raw" | envsubst "$FLUX_ENVSUBST_VARS")
    # kubeconform exits 0 on a stream holding no resource (empty, comments or
    # separators only), so a stage must render at least one kinded document.
    # The count drains stdin: an early exit would SIGPIPE a large render.
    if ! printf '%s\n' "$rendered" | python3 -c '
import sys
import yaml
kinded = sum(1 for d in yaml.safe_load_all(sys.stdin) if isinstance(d, dict) and d.get("kind"))
sys.exit(0 if kinded else 1)
'; then
        echo "ERROR: $srcpath rendered no resources"
        return 1
    fi
    printf '%s\n---\n' "$rendered" >> "$FL_RENDER_ALL"
    printf '%s\n' "$rendered" | kubeconform -strict -ignore-missing-schemas \
        -kubernetes-version "$K8S_VER" \
        -schema-location default \
        -schema-location "$FL_CATALOG" \
        -summary
}

# Which rendered kinds kubeconform could NOT schema-validate
# (-ignore-missing-schemas skipped them). With a baseline a kind outside it
# fails the run; without one, informational.
track_unvalidated_kinds() {
    local json rc=0
    echo "=== kubeconform unvalidated-kind tracking ==="
    json=$(mktemp)
    kubeconform -verbose -ignore-missing-schemas -kubernetes-version "$K8S_VER" \
        -schema-location default \
        -schema-location "$FL_CATALOG" \
        -output json < "$FL_RENDER_ALL" > "$json" || true
    if [ -n "$FL_EXPECTED_SKIPPED" ]; then
        python3 "$FL_SKIPPED_SCRIPT" "$FL_EXPECTED_SKIPPED" < "$json" || rc=1
    else
        python3 "$FL_SKIPPED_SCRIPT" < "$json" || true
    fi
    rm -f "$json"
    return $rc
}

run_substitute_mode() {
    [ -n "$FL_CLUSTER_DIR" ] || die "--cluster-dir is required in substitute mode"
    [ -n "$FL_VERSIONS_CM" ] || die "--versions-configmap is required in substitute mode"

    local vars
    vars=$(bash "$FL_RENDER_SCRIPT" export-versions "$FL_VERSIONS_CM") \
        || { echo "ERROR: failed to extract version keys from $FL_VERSIONS_CM"; return 1; }
    eval "$vars"

    local failed=0
    [ "$FL_CHECK_FLUX_VERSION" != "true" ] || check_flux_cli || failed=1

    if [ -n "$FL_K8S_VERSION" ]; then
        K8S_VER="$FL_K8S_VERSION"
        echo "kubeconform -kubernetes-version: $K8S_VER"
    else
        K8S_VER=$(bash "$FL_RENDER_SCRIPT" k8s-version "$FL_VERSIONS_CM")
        echo "kubeconform -kubernetes-version: $K8S_VER (from versions-configmap k3s_version)"
    fi
    export K8S_VER

    if [ -z "$FL_RENDER_ALL" ]; then
        FL_RENDER_ALL=$(mktemp)
        # shellcheck disable=SC2064  # expand the path now, not at trap time
        trap "rm -f '$FL_RENDER_ALL'" EXIT
    fi

    local stages label srcpath
    stages=$(mktemp)
    if ! enumerate_stages "$stages"; then
        echo "ERROR: could not derive the stage source paths from $FL_CLUSTER_DIR/"
        rm -f "$stages"
        return 1
    fi
    while IFS=$'\t' read -r label srcpath; do
        [ -n "$srcpath" ] || continue
        srcpath="${srcpath#./}"
        lint_stage "$label" "$srcpath" || failed=1
    done < "$stages"
    rm -f "$stages"

    # A gate that inspects nothing is not a gate: every check below reads the
    # corpus.
    if [ ! -s "$FL_RENDER_ALL" ]; then
        echo "ERROR: no Kustomization under $FL_CLUSTER_DIR rendered any manifest — check cluster_dir and each child's spec.path"
        failed=1
    else
        track_unvalidated_kinds || failed=1
    fi

    check_cluster_root || failed=1
    return $failed
}

case "$FL_MODE" in
    simple) run_simple_mode ;;
    substitute) run_substitute_mode ;;
    *) die "unknown --mode: $FL_MODE (want: substitute|simple)" ;;
esac
