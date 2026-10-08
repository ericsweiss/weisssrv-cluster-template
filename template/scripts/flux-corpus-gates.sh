#!/usr/bin/env bash
# The cluster-invariant gates over the WHOLE rendered corpus. `task flux:lint` and
# the CI flux-lint job both call this; every gate runs even after one fails.
# Exit 1 is a finding, exit 2 an operator error a gate or this script reported.

# Usage: scripts/flux-corpus-gates.sh <rendered-corpus> [<versions-configmap>]
# Without the second argument the HelmRelease values validation is skipped.
set -euo pipefail

CORPUS=${1:-}
VERSIONS_CM=${2:-}

if [ -z "$CORPUS" ] || [ ! -f "$CORPUS" ]; then
  echo "usage: $0 <rendered-corpus> [<versions-configmap>]" >&2
  exit 2
fi

# Every path below is repository-relative; callers pass an absolute corpus path.
_SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$_SCRIPT_DIR/.." || exit 2

WORST=0

# A gate's rc 2 or more is an operator error, never a policy finding, so it must
# not reach the caller as a finding's exit 1.
record() {
  if [ "$1" -ge 2 ]; then
    echo "ERROR: ${2:-a gate} exited $1 — operator error, not a finding" >&2
    WORST=2
  elif [ "$1" -ne 0 ] && [ "$WORST" -eq 0 ]; then
    WORST=1
  fi
}

finish() {
  if [ "$WORST" -eq 2 ]; then
    echo "corpus gates: a gate reported an OPERATOR ERROR" >&2
    exit 2
  fi
  exit "$WORST"
}

# A glob that matched nothing would leave the literal pattern and the gates would
# exit 0 over a corpus missing the trees below.
shopt -s nullglob
CLUSTERS=(kubernetes/clusters/*/)
shopt -u nullglob
if [ ${#CLUSTERS[@]} -eq 0 ]; then
  echo "ERROR: no kubernetes/clusters/<name>/ — the corpus gates inspected no cluster" >&2
  exit 2
fi

# The tenants tree is a Kustomize aggregator, not a Flux Kustomization with a
# spec.path, so the caller's per-Kustomization loop never renders it and these
# gates would never see a tenant's own SecretStore or NetworkPolicy.
for root in "${CLUSTERS[@]}"; do
  tenants="${root%/}/tenants"
  if [ ! -d "$tenants" ]; then
    echo "ERROR: $tenants is missing — tenant SecretStores and NetworkPolicies would be ungated" >&2
    record 1 "$tenants"
    continue
  fi
  echo "=== Adding $tenants to the corpus ==="
  printf '\n---\n' >> "$CORPUS"
  if ! kustomize build "$tenants" >> "$CORPUS"; then
    echo "ERROR: kustomize build failed for $tenants" >&2
    record 2 "kustomize build $tenants"
  fi
  printf '\n---\n' >> "$CORPUS"
done

# The bootstrap flux-system Kustomization's path is the cluster root, which
# flux-child-kustomizations.py never enumerates, so the Flux controllers are
# ungated without this. tenants/ repeats above; gates key on namespace/kind/name.
for root in "${CLUSTERS[@]}"; do
  root=${root%/}
  if [ ! -e "$root/flux-system/gotk-components.yaml" ]; then
    echo "=== Skipping $root: flux bootstrap has not written flux-system yet ==="
    continue
  fi
  echo "=== Adding $root to the corpus ==="
  printf '\n---\n' >> "$CORPUS"
  if ! kustomize build "$root" >> "$CORPUS"; then
    echo "ERROR: kustomize build failed for $root" >&2
    record 2 "kustomize build $root"
  fi
  printf '\n---\n' >> "$CORPUS"
done

# Every gate below reads the corpus on stdin and reports a clean run over zero
# resources, so a render that produced nothing would pass the whole stage. The
# separators the appends above write are not objects.
if ! grep -qE '^kind:' "$CORPUS"; then
  echo "ERROR: rendered corpus holds no Kubernetes object — the gates below would inspect nothing" >&2
  exit 2
fi

echo "=== Checking HPA/VPA invariant ==="
# --allow-unjudged-vpa-caps: a chart renders most of these targets, so the
# corpus carries no limit to compare the cap against. validate-helm-values.py
# judges those caps against the chart-rendered limits.
python3 scripts/check-hpa-vpa-invariant.py --require-chart-native-vpas \
  --allow-unjudged-vpa-caps \
  --policy-config scripts/autoscaling-policy.yaml < "$CORPUS" || record $? check-hpa-vpa-invariant.py
echo "=== Checking scrape/NetworkPolicy invariant ==="
python3 scripts/check-scrape-netpol.py < "$CORPUS" || record $? check-scrape-netpol.py
echo "=== Checking ingress default-deny coverage ==="
python3 scripts/check-default-deny-coverage.py < "$CORPUS" || record $? check-default-deny-coverage.py
echo "=== Checking ClusterSecretStore scoping ==="
python3 scripts/check-secretstore-scope.py < "$CORPUS" || record $? check-secretstore-scope.py
echo "=== Checking PVC storageClassName ==="
python3 scripts/check-pvc-storageclass.py < "$CORPUS" || record $? check-pvc-storageclass.py
# --allow-empty: a cluster with no NFS storage ships no such PV. The cert domain
# only names the certificate in the IP-server message.
echo "=== Checking NFS PersistentVolume TLS ==="
NFS_CERT_DOMAIN=$(scripts/cluster-config-value.sh cluster_internal_domain || true)
python3 scripts/check-nfs-tls.py --allow-empty --cert-domain "$NFS_CERT_DOMAIN" \
  < "$CORPUS" || record $? check-nfs-tls.py
# Source files, not the corpus: the app list is Ansible site data.
echo "=== Checking backup-artifact apps against their alert arms ==="
python3 scripts/check-backup-artifact-apps.py \
  --host-vars ansible/inventories/prod/group_vars/nas.yml \
  --rules kubernetes/infrastructure/observability/kube-prometheus-stack/release.yaml || record $? check-backup-artifact-apps.py

if [ -z "$VERSIONS_CM" ]; then
  echo "=== Skipping HelmRelease values validation (no versions ConfigMap argument) ==="
  finish
fi

# kustomize build emits a HelmRelease verbatim, so .spec.values — a free-form
# object in the CRD — reaches the cluster unvalidated. This renders the
# value-heavy charts with `helm template` instead. Needs network.
echo "=== Schema-validating HelmRelease values blocks (helm template) ==="
MERGED_CM=$(mktemp)
trap 'rm -f "$MERGED_CM"' EXIT
if scripts/flux-env.sh merged-configmap "$VERSIONS_CM" > "$MERGED_CM"; then
  # An empty merge leaves every `${...}` unsubstituted and the validation below
  # would still report clean: a gate run over no substitutions is not a gate.
  if [ ! -s "$MERGED_CM" ]; then
    echo "ERROR: flux-env.sh produced an empty merged configmap from $VERSIONS_CM — the HelmRelease values validation would inspect no substitutions" >&2
    record 2 "flux-env.sh merged-configmap"
    finish
  fi
  python3 scripts/validate-helm-values.py --kubeconform \
    --releases scripts/helm-values-releases.yaml \
    --versions-configmap "$MERGED_CM" \
    --policy-config scripts/autoscaling-policy.yaml || record $? validate-helm-values.py
else
  echo "ERROR: could not merge the substitution ConfigMaps" >&2
  record 2 "flux-env.sh merged-configmap"
fi

finish
