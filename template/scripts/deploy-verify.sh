#!/usr/bin/env bash
# Post-deployment cluster verification, invoked by the cluster-verify CI job.
# Needs kubectl, jq and flux on PATH, and reads KUSTOMIZE_VERSION,
# KUSTOMIZE_SHA256 and PYYAML_VERSION from the job environment.

# The FLUX_VERSION pin stays inline in .gitlab-ci.yml: task lint:flux-version-pin
# greps it from that file.

# Bind + assert the CI-provided version pins up front: fail loudly if this
# script is ever run outside the CI job that sets them, and give shellcheck a
# visible assignment (no SC2154 for env-sourced vars).
KUSTOMIZE_VERSION="${KUSTOMIZE_VERSION:?deploy-verify.sh requires KUSTOMIZE_VERSION (set in .gitlab-ci.yml variables)}"
KUSTOMIZE_SHA256="${KUSTOMIZE_SHA256:?deploy-verify.sh requires KUSTOMIZE_SHA256 (set in .gitlab-ci.yml variables)}"
PYYAML_VERSION="${PYYAML_VERSION:?deploy-verify.sh requires PYYAML_VERSION (set in .gitlab-ci.yml variables)}"

set -euo pipefail

# Pure pod/HelmRelease/Ready-condition classifiers live in a sourced lib so they
# can be unit-tested without a live cluster (scripts/test_deploy_verify_lib.py).
_SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=scripts/deploy-verify-lib.sh
. "$_SCRIPT_DIR/deploy-verify-lib.sh"

# Tools for server-side dry-run validation: the cluster-verify job ships
# kubectl, jq and flux only. Each arm no-ops when its tool is already present;
# install output stays on the job log so a failure is diagnosable.
if ! command -v envsubst > /dev/null 2>&1; then
  apt-get install -y -qq gettext-base > /dev/null
fi
if ! python3 -c "import yaml" > /dev/null 2>&1; then
  pip install --quiet "pyyaml==${PYYAML_VERSION}"
fi
if ! command -v kustomize > /dev/null 2>&1; then
  # Per-run download dir: a fixed /tmp path collides when two jobs share /tmp.
  _dl=$(mktemp -d)
  curl -fsSL "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2Fv${KUSTOMIZE_VERSION}/kustomize_v${KUSTOMIZE_VERSION}_linux_amd64.tar.gz" -o "${_dl}/kustomize.tar.gz"
  echo "${KUSTOMIZE_SHA256}  ${_dl}/kustomize.tar.gz" | sha256sum -c -
  tar xzf "${_dl}/kustomize.tar.gz" -C /usr/local/bin kustomize
  rm -rf "${_dl}"
fi

# CRITICAL: every cluster read below is captured with `|| true`, so a missing or
# unauthorized kubectl reads as a clean cluster. Fail before the first capture.
for _tool in kubectl jq flux; do
  if ! command -v "$_tool" > /dev/null 2>&1; then
    echo "ERROR: deploy-verify.sh needs $_tool on PATH; without it every check below reads as clean" >&2
    exit 2
  fi
done
if ! kubectl version --request-timeout=10s > /dev/null 2>&1; then
  echo "ERROR: kubectl cannot reach the cluster; without an API every check below reads as clean" >&2
  exit 2
fi

# Bounded retry helper for transient startup states.
# Suppresses output during polling; on timeout, prints the last
# failed attempt's output for diagnostics.
wait_for() {
  local desc="$1" timeout="$2" interval="$3"; shift 3
  local start_ts last_output
  start_ts=$(date +%s)
  while true; do
    last_output=$("$@" 2>&1) && return 0
    local elapsed=$(( $(date +%s) - start_ts ))
    if [ "$elapsed" -ge "$timeout" ]; then
      echo "wait_for: $desc timed out after ${elapsed}s"
      echo "Last check output:"
      echo "$last_output"
      return 1
    fi
    echo "wait_for: $desc not ready (${elapsed}s/${timeout}s)..."
    sleep "$interval"
  done
}

# Ready condition of one flux-system Kustomization, "Unknown" when it cannot be
# read, so a missing object reports rather than aborting under set -e.
ks_ready() {
  kubectl -n flux-system get kustomization "$1" \
    -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || echo "Unknown"
}

echo "=== Post-Deployment Verification ==="

echo "Checking node status..."
check_nodes_ready() {
  NODE_OUTPUT=$(kubectl get nodes --no-headers)
  [ -z "$NODE_OUTPUT" ] && return 1
  NOT_READY=$(echo "$NODE_OUTPUT" | nodes_not_ready_count)
  [ "$NOT_READY" -eq 0 ]
}
if ! wait_for "all nodes Ready" 60 5 check_nodes_ready; then
  echo "ERROR: Node(s) not Ready after 60s"
  kubectl get nodes --no-headers
  exit 1
fi
kubectl get nodes --no-headers

EXIT=0

echo ""
echo "=== Server-side dry-run validation ==="
# Validate rendered manifests against the cluster API without applying: catches
# CRD field mismatches kubeconform's offline schema cannot. Runs before the Flux
# reconcile, so issues surface even if Flux would fail to apply them.
VERSIONS_CONFIGMAP=kubernetes/infrastructure/sources/versions-configmap.yaml
if [ ! -f "$VERSIONS_CONFIGMAP" ]; then
  echo "ERROR: $VERSIONS_CONFIGMAP not found"
  exit 1
fi
# flux-env.sh, not flux-render.sh: it merges BOTH substitution ConfigMaps
# (cluster-versions and cluster-config), so ${cluster_*} resolves.
VARS=$(bash scripts/flux-env.sh export-versions "$VERSIONS_CONFIGMAP") \
  || { echo "ERROR: failed to extract substitution keys for dry-run"; exit 1; }
eval "$VARS"
# No substitution keys means the dry-run below would validate nothing, which is
# a gate that cannot fail rather than a clean run.
require_envsubst_vars() {
  [ -n "${FLUX_ENVSUBST_VARS:-}" ] && return 0
  echo "ERROR: no substitution keys exported from ${VERSIONS_CONFIGMAP:-the versions ConfigMap} and cluster-config;"
  echo "       the server-side dry-run would validate nothing."
  return 1
}
if ! require_envsubst_vars; then
  EXIT=1
else
  if ! KS_PATHS=$(python3 "$_SCRIPT_DIR/flux-child-kustomizations.py" --paths); then
    echo "ERROR: could not derive the stage source paths from kubernetes/clusters/"
    exit 1
  fi
  DRYRUN_PATHS=$(printf '%s\n' "$KS_PATHS" | grep -c '[^[:space:]]' || true)
  if [ "$DRYRUN_PATHS" -eq 0 ]; then
    echo "ERROR: flux-child-kustomizations.py --paths yielded no stage paths; nothing was dry-run."
    EXIT=1
  fi
  while IFS= read -r SRCPATH; do
    [ -z "$SRCPATH" ] && continue
    echo "  dry-run: $SRCPATH"
    # `printf '%s\n'`, NEVER `echo`, when piping captured YAML: bash's echo
    # expands the `\n` literals inside the dashboard block scalars and breaks the
    # parser. kustomize stderr goes to a tmpfile so notices cannot contaminate it.
    KS_ERR=$(mktemp)
    if ! RAW=$(kustomize build "$SRCPATH" 2>"$KS_ERR"); then
      echo "    FAIL: kustomize build failed for $SRCPATH"
      head -120 <"$KS_ERR"
      rm -f "$KS_ERR"
      EXIT=1
      continue
    fi
    rm -f "$KS_ERR"
    RENDERED=$(printf '%s\n' "$RAW" | envsubst "$FLUX_ENVSUBST_VARS")
    # SSA, not plain `apply --dry-run=server`: the latter needs the legacy
    # last-applied-configuration annotation Flux-owned resources lack.
    # --force-conflicts wins field-ownership disputes; nothing is persisted.

    # The `if` wrapper keeps a non-zero kubectl exit from aborting under set -e.
    if DRYRUN_ERR=$(printf '%s\n' "$RENDERED" | kubectl apply \
      --server-side --dry-run=server --force-conflicts \
      --field-manager=ci-deploy-verify -f - 2>&1); then
      DRYRUN_RC=0
    else
      DRYRUN_RC=$?
    fi
    if [ "$DRYRUN_RC" -ne 0 ]; then
      echo "    FAIL: server-side dry-run rejected manifests from $SRCPATH"
      # `|| true` absorbs grep's no-match exit under pipefail; the unfiltered
      # output is the fallback. Here-strings, not `printf | head`, so head's
      # early close cannot SIGPIPE the writer.
      FILTERED_ERR=$(grep -iE "error|invalid|did not find|cannot|forbidden|timeout" \
        <<<"$DRYRUN_ERR" || true)
      if [ -n "$FILTERED_ERR" ]; then
        head -30 <<<"$FILTERED_ERR"
      else
        head -30 <<<"$DRYRUN_ERR"
      fi
      EXIT=1
    fi
  done <<<"$KS_PATHS"
fi

# Snapshot pre-reconcile state to tell steady-state from bootstrap: with every
# Kustomization already Ready a non-Ready ExternalSecret is a failure, while
# during bootstrap ESO may still be starting.

# A classifier that could not run must not relax the rest of the verify, so a
# failed kubectl is reported and the STRICT arm is taken.
KS_ERR=$(mktemp)
if PRE_KS_JSON=$(kubectl get kustomizations.kustomize.toolkit.fluxcd.io -A -o json 2>"$KS_ERR"); then
  PRE_KS_NOT_READY=$(printf '%s' "$PRE_KS_JSON" | count_not_ready)
  # count_not_ready's jq-error sentinel is numeric, so the sanity check below
  # would accept it and read as "bootstrap", relaxing every later check.
  if [ "$PRE_KS_NOT_READY" = "999" ]; then
    echo "ERROR: could not classify the pre-reconcile Kustomization list (jq failed); steady state is unknown"
    EXIT=1
    PRE_KS_NOT_READY=0
  fi
else
  echo "ERROR: could not list Kustomizations pre-reconcile; steady state is unknown:"
  head -20 <"$KS_ERR"
  EXIT=1
  PRE_KS_NOT_READY=""
fi
rm -f "$KS_ERR"
if ! [ "$PRE_KS_NOT_READY" -eq "$PRE_KS_NOT_READY" ] 2>/dev/null; then
  PRE_KS_NOT_READY=0
  EXIT=1
fi
STEADY_STATE=$(steady_state "$PRE_KS_NOT_READY")
if [ "$STEADY_STATE" = "true" ]; then
  echo "Cluster is steady-state (all Kustomizations were Ready pre-reconcile)"
else
  echo "Cluster is in bootstrap/recovery ($PRE_KS_NOT_READY Kustomization(s) not Ready pre-reconcile)"
fi

echo ""
echo "=== Triggering Flux reconciliation ==="
# Force Flux to reconcile the current commit before checking status.
# Without this, we would check stale state from the previous reconcile cycle.
echo "Triggering Flux source reconciliation..."
if ! flux reconcile source git flux-system --timeout=2m; then
  echo "ERROR: Flux source reconciliation failed or timed out"
  EXIT=1
fi
echo "Triggering root kustomization reconciliation..."
if ! flux reconcile kustomization flux-system --timeout=3m --with-source; then
  echo "ERROR: Flux root kustomization reconciliation failed or timed out"
  EXIT=1
fi
# Allow downstream kustomizations time to reconcile after the root
sleep 15

echo ""
echo "Flux controllers:"
# A failed controller check is a finding; the three listings below are
# informational, and must not abort the gates further down.
flux check || EXIT=1

echo ""
echo "Flux Kustomizations:"
flux get kustomizations -A || true

echo ""
echo "Flux HelmReleases:"
flux get helmreleases -A || true

echo ""
echo "ExternalSecrets:"
kubectl get externalsecrets -A || echo "(no ExternalSecret CRD yet)"

echo ""
echo "ExternalSecret readiness:"
check_externalsecrets_ready() {
  # The query is split from the count: count_not_ready exits 0 with empty output
  # on empty input, so a trailing `|| echo 999` can never fire and a failed
  # kubectl would leave the count blank.
  if ES_JSON=$(kubectl get externalsecrets -A -o json 2>/dev/null); then
    ES_COUNT=$(printf '%s' "$ES_JSON" | count_not_ready)
  else
    ES_COUNT="999"
  fi
  [ "$ES_COUNT" -eq 0 ]
}
if ! wait_for "ExternalSecrets ready" 90 10 check_externalsecrets_ready; then
  ES_ERR=$(mktemp)
  if ES_JSON=$(kubectl get externalsecrets -A -o json 2>"$ES_ERR"); then
    ES_NOT_READY=$(printf '%s' "$ES_JSON" | count_not_ready)
    # The name list is diagnostics: its classifier exits non-zero when jq fails,
    # and under `set -e` that would end the run before the verdict below.
    printf '%s' "$ES_JSON" | not_ready_ns_names || true
  else
    ES_NOT_READY="999"
    echo "ERROR: could not list ExternalSecrets:"
    head -20 <"$ES_ERR"
  fi
  rm -f "$ES_ERR"
  # count_not_ready's sentinel is numeric and its empty output is not, so a
  # failed classifier would otherwise print as a count and, during bootstrap, be
  # tolerated as a warning.
  if [ "$ES_NOT_READY" = "999" ] || ! [ "$ES_NOT_READY" -eq "$ES_NOT_READY" ] 2>/dev/null; then
    echo "ERROR: could not classify the ExternalSecret list (jq failed or the list was unreadable); readiness is unknown"
    EXIT=1
  elif [ "$STEADY_STATE" = "true" ]; then
    echo "ERROR: $ES_NOT_READY ExternalSecret(s) not Ready on a steady-state cluster"
    EXIT=1
  else
    echo "WARNING: $ES_NOT_READY ExternalSecret(s) not Ready (bootstrap/recovery)"
  fi
else
  echo "All ExternalSecrets ready"
fi

# Fail if any Flux resource is not Ready. Queried as CRDs via kubectl -o json
# because `flux get all` emits only tabular output, and parsing that text counts
# stderr warnings as outages.
echo ""
echo "Checking for non-Ready Flux resources..."
FLUX_KINDS="kustomizations.kustomize.toolkit.fluxcd.io,helmreleases.helm.toolkit.fluxcd.io,gitrepositories.source.toolkit.fluxcd.io,helmrepositories.source.toolkit.fluxcd.io,ocirepositories.source.toolkit.fluxcd.io"
check_flux_resources_ready() {
  FLUX_ALL=$(kubectl get "$FLUX_KINDS" -A -o json) || return 1
  NOT_READY=$(echo "$FLUX_ALL" | count_not_ready)
  [ "$NOT_READY" -eq 0 ]
}
# 5 min headroom — major chart bumps (kube-prometheus-stack, Loki) can
# take 2-4 min to reconcile through HelmRelease + child CRD updates +
# rollout, and verify runs immediately after `git push` of those bumps.
if ! wait_for "Flux resources ready" 300 10 check_flux_resources_ready; then
  FLUX_ERR=$(mktemp)
  if FLUX_ALL=$(kubectl get "$FLUX_KINDS" -A -o json 2>"$FLUX_ERR"); then
    NOT_READY_COUNT=$(echo "$FLUX_ALL" | count_not_ready)
    echo "ERROR: $NOT_READY_COUNT Flux resource(s) not Ready:"
    echo "$FLUX_ALL" | jq -r ".items[] | $JQ_NOT_READY | \"  \(.kind)/\(.metadata.namespace)/\(.metadata.name): \((.status.conditions // []) | map(select(.type == \"Ready\")) | .[0].message // \"no Ready condition\")\""
  else
    echo "ERROR: could not list Flux resources ($FLUX_KINDS):"
    head -20 <"$FLUX_ERR"
  fi
  rm -f "$FLUX_ERR"
  EXIT=1
else
  echo "All Flux resources ready"
fi

# Name the top-level Kustomizations explicitly: the broad check above catches a
# stuck stage too, but not with a per-stage failure line. Derived in dependsOn
# order, never hand-listed, so a new stage cannot be missing from the gate.
TOP_KUSTOMIZATIONS=$(python3 "$_SCRIPT_DIR/flux-child-kustomizations.py")
if [ -z "$TOP_KUSTOMIZATIONS" ]; then
  echo "ERROR: could not derive the child Kustomization list from kubernetes/clusters/"
  exit 1
fi
echo "Top-level Kustomizations under gate: $(echo "$TOP_KUSTOMIZATIONS" | tr '\n' ' ')"
check_top_kustomizations_ready() {
  for ks_name in $TOP_KUSTOMIZATIONS; do
    KS_READY=$(ks_ready "$ks_name")
    if [ "$KS_READY" != "True" ]; then return 1; fi
  done
}
if ! wait_for "top-level Kustomizations ready" 180 5 check_top_kustomizations_ready; then
  for ks_name in $TOP_KUSTOMIZATIONS; do
    KS_READY=$(ks_ready "$ks_name")
    if [ "$KS_READY" != "True" ]; then
      echo "ERROR: flux-system/$ks_name Kustomization not Ready (status=$KS_READY)"
      EXIT=1
    fi
  done
else
  echo "All top-level Kustomizations ready"
fi
echo ""
echo "=== Observability Stack ==="
check_obs_pods_healthy() {
  OBS_PODS=$(kubectl get pods -n observability --no-headers 2>/dev/null) || return 1
  [ -z "$OBS_PODS" ] && return 1
  [ -z "$(echo "$OBS_PODS" | pods_not_running_or_completed)" ] || return 1
  [ -z "$(echo "$OBS_PODS" | pods_running_unready)" ] || return 1
}
# Long timeout in steady state covers chart-upgrade rollouts; the short
# bootstrap timeout drops into the WARNING branch instead of burning the budget.
OBS_PODS_WAIT=240
[ "$STEADY_STATE" != "true" ] && OBS_PODS_WAIT=30
if ! wait_for "observability pods healthy" "$OBS_PODS_WAIT" 10 check_obs_pods_healthy; then
  # A failed lookup is not an empty namespace: keep them apart so an unreachable
  # API cannot read as "bootstrap, nothing here yet".
  OBS_RC=0
  OBS_PODS=$(kubectl get pods -n observability --no-headers 2>/dev/null) || OBS_RC=$?
  if [ "$OBS_RC" -ne 0 ]; then
    echo "ERROR: could not list observability pods (kubectl exit $OBS_RC) - the stack was not verified"
    EXIT=1
  elif [ -z "$OBS_PODS" ]; then
    if [ "$STEADY_STATE" = "true" ]; then
      echo "ERROR: No pods in observability namespace on a steady-state cluster"
      EXIT=1
    else
      echo "WARNING: No pods in observability namespace (bootstrap/recovery)"
    fi
  else
    OBS_BAD=$(echo "$OBS_PODS" | pods_not_running_or_completed)
    if [ -n "$OBS_BAD" ]; then
      # In bootstrap/recovery a Pending or initializing pod only warns. Anything
      # off the transient-state allowlist (CrashLoopBackOff, ImagePullBackOff,
      # OOMKilled, ...) is a real bug. Steady state fails on any non-Running pod.
      OBS_NON_TRANSIENT=$(echo "$OBS_BAD" | pods_non_transient)
      if [ "$STEADY_STATE" = "true" ] || [ -n "$OBS_NON_TRANSIENT" ]; then
        echo "ERROR: Observability pods in failing state:"
        echo "$OBS_BAD"
        EXIT=1
      else
        echo "WARNING: Some observability pods not yet Running (bootstrap/recovery):"
        echo "$OBS_BAD"
      fi
    else
      OBS_UNREADY=$(echo "$OBS_PODS" | pods_running_unready)
      if [ -n "$OBS_UNREADY" ]; then
        echo "WARNING: Some observability pods have unready containers:"
        echo "$OBS_UNREADY"
        if [ "$STEADY_STATE" = "true" ]; then EXIT=1; fi
      fi
    fi
  fi
else
  echo "All observability pods healthy"
fi
check_obs_helmreleases_ready() {
  OBS_HR_JSON=$(kubectl get helmreleases -n observability -o json 2>/dev/null) || return 1
  OBS_HR_COUNT=$(echo "$OBS_HR_JSON" | jq '.items | length' 2>/dev/null) || return 1
  [ "${OBS_HR_COUNT:-0}" -eq 0 ] && return 1
  OBS_HR_BAD=$(echo "$OBS_HR_JSON" | helmreleases_not_ready_names)
  [ -z "$OBS_HR_BAD" ]
}
# Same pattern as the pods wait above: long budget in steady state for
# chart-upgrade reconciliation, short budget in bootstrap/recovery so
# the WARNING branch handles the legitimately-empty case without delay.
OBS_HR_WAIT=300
[ "$STEADY_STATE" != "true" ] && OBS_HR_WAIT=30
if ! wait_for "observability HelmReleases ready" "$OBS_HR_WAIT" 10 check_obs_helmreleases_ready; then
  # As with the pods above, a failed lookup must not read as an empty namespace.
  OBS_HR_RC=0
  OBS_HR_JSON=$(kubectl get helmreleases -n observability -o json 2>/dev/null) || OBS_HR_RC=$?
  OBS_HR_COUNT=$(echo "$OBS_HR_JSON" | jq '.items | length' 2>/dev/null) || OBS_HR_RC=1
  if [ "$OBS_HR_RC" -ne 0 ]; then
    echo "ERROR: could not list observability HelmReleases - the stack was not verified"
    EXIT=1
  elif [ "${OBS_HR_COUNT:-0}" -eq 0 ]; then
    if [ "$STEADY_STATE" = "true" ]; then
      echo "ERROR: No HelmReleases in observability namespace on a steady-state cluster"
      EXIT=1
    else
      echo "WARNING: No HelmReleases in observability namespace (bootstrap/recovery)"
    fi
  else
    OBS_HR_BAD=$(echo "$OBS_HR_JSON" | helmreleases_not_ready_names)
    # In bootstrap/recovery a still-reconciling HelmRelease only warns. Hard
    # failure reasons and any HR with a non-zero .status.failures are real bugs
    # even then (helmreleases_hard_failed in deploy-verify-lib.sh).
    OBS_HR_FAILED=$(echo "$OBS_HR_JSON" | helmreleases_hard_failed)
    if [ "$STEADY_STATE" = "true" ] || [ -n "$OBS_HR_FAILED" ]; then
      echo "ERROR: Not all observability HelmReleases are Ready:"
      echo "$OBS_HR_BAD"
      EXIT=1
    else
      echo "WARNING: Some observability HelmReleases still reconciling (bootstrap/recovery):"
      echo "$OBS_HR_BAD"
    fi
  fi
else
  echo "All observability HelmReleases ready"
fi

echo ""
echo "LoadBalancer services:"
kubectl get svc -A | grep LoadBalancer || true

# Both ingress VIPs must be assigned AND announceable: externalTrafficPolicy
# Local plus the speaker's ingress nodeSelector mean a VIP is announced only from
# an ingress-labelled node that also holds a ready Traefik endpoint.
if VIP_CONF=$("$_SCRIPT_DIR/cluster-config-value.sh" \
    cluster_metallb_public_vip cluster_metallb_internal_vip cluster_node_label_domain); then
  read -r PUBLIC_VIP INTERNAL_VIP NODE_LABEL_DOMAIN <<< "$VIP_CONF"
  INGRESS_NODES=$(kubectl get nodes -l "${NODE_LABEL_DOMAIN}/ingress=true" \
    -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || true)
  for SVC_VIP in "traefik=$PUBLIC_VIP" "traefik-internal=$INTERNAL_VIP"; do
    SVC="${SVC_VIP%%=*}"
    WANT_VIP="${SVC_VIP#*=}"
    GOT_VIP=$(kubectl get svc -n traefik "$SVC" \
      -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
    READY_NODES=$(kubectl get endpointslices -n traefik \
      -l "kubernetes.io/service-name=$SVC" -o json 2>/dev/null \
      | jq -r '.items[].endpoints[] | select(.conditions.ready != false) | .nodeName // empty' \
      2>/dev/null || true)
    ANNOUNCER=""
    while read -r NODE; do
      case " $INGRESS_NODES " in
        *" $NODE "*) [ -n "$NODE" ] && ANNOUNCER="$NODE" ;;
      esac
    done <<< "$READY_NODES"
    VIP_PROBLEM=""
    if [ "$GOT_VIP" != "$WANT_VIP" ]; then
      VIP_PROBLEM="holds '${GOT_VIP:-<pending>}', not the configured VIP $WANT_VIP"
    elif [ -z "$ANNOUNCER" ]; then
      VIP_PROBLEM="has no ready endpoint on a ${NODE_LABEL_DOMAIN}/ingress node, so $WANT_VIP is announced by nobody"
    fi
    if [ -z "$VIP_PROBLEM" ]; then
      echo "traefik/$SVC: $WANT_VIP announced from $ANNOUNCER"
    elif [ "$STEADY_STATE" = "true" ]; then
      echo "ERROR: Service traefik/$SVC $VIP_PROBLEM"
      EXIT=1
    else
      echo "WARNING: Service traefik/$SVC $VIP_PROBLEM (bootstrap/recovery)"
    fi
  done
else
  echo "ERROR: could not read the ingress VIPs and node-label domain from cluster-config; the VIP-announce gate did not run"
  EXIT=1
fi

echo ""
echo "=== Verification Complete (exit=$EXIT) ==="
exit $EXIT
