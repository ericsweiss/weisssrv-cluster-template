#!/usr/bin/env bash
# CRITICAL: cluster health check for the maintenance tasks and jobs; exits 1 on
# any critical failure. Two rules hold throughout, and breaking either makes
# this gate lie. Test a CAPTURED value, never `! ... | grep -q`: under pipefail
# grep -q's early pipe close SIGPIPEs the upstream and inverts the verdict.
# Look pods up with jsonpath, never `kubectl get -o wide`: the RESTARTS
# column's "5 (3m ago)" suffix shifts every field after it.
# Needs kubectl plus maintenance-lib.sh.

set -euo pipefail

# Absolute dir so sourcing works regardless of CWD / PATH invocation.
_SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=scripts/maintenance-lib.sh
. "$_SCRIPT_DIR/maintenance-lib.sh"

echo "=== Post-Maintenance Verification ==="
ERRORS=0

# Nodes kured is actively rebooting: NotReady and evicted pods are expected, so
# checks WARN for them and ERROR for everything else. Re-read per check (kured
# reboots serially). Needs configuration.annotateNodes:true in kured/release.yaml.
kured_rebooting_nodes() {
  # The annotated-AND-cordoned filter lives in maintenance-lib.sh, unit-tested.
  kubectl get nodes \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.annotations.weave\.works/kured-reboot-in-progress}{"\t"}{.spec.unschedulable}{"\n"}{end}' \
    2>/dev/null | kured_rebooting_filter || true
}

echo "Checking k3s node status..."
# Capture failure as a verify error rather than aborting under set -e.
node_query_failed=false
NODE_OUTPUT=$(kubectl get nodes --no-headers 2>/dev/null) || node_query_failed=true
echo "$NODE_OUTPUT"
if [ "$node_query_failed" = true ]; then
  echo "ERROR: kubectl get nodes failed"
  ERRORS=$((ERRORS + 1))
elif [ -z "$NODE_OUTPUT" ]; then
  echo "ERROR: kubectl returned no nodes"
  ERRORS=$((ERRORS + 1))
else
  # Not-ready = STATUS not starting with "Ready", so a cordoned-but-healthy node
  # is fine. A node kured is rebooting WARNs; anything else ERRORs.
  KURED_NOW=$(kured_rebooting_nodes)
  NOT_READY_NAMES=$(echo "$NODE_OUTPUT" | not_ready_node_names)
  NODE_VERDICTS=$(printf '%s\n' "$NOT_READY_NAMES" | classify_not_ready_nodes "$KURED_NOW")
  # Grace + re-read once: a node that just finished a kured reboot can be briefly
  # NotReady with its annotation already cleared.
  case "$NODE_VERDICTS" in
    *error\ *)
      sleep 20
      # Only re-classify on a successful fresh query; a stale snapshot plus an
      # empty KURED_NOW would mis-ERROR a rebooting node.
      if FRESH_NODES=$(kubectl get nodes --no-headers 2>/dev/null); then
        NODE_OUTPUT="$FRESH_NODES"
        KURED_NOW=$(kured_rebooting_nodes)
        NOT_READY_NAMES=$(echo "$NODE_OUTPUT" | not_ready_node_names)
        NODE_VERDICTS=$(printf '%s\n' "$NOT_READY_NAMES" | classify_not_ready_nodes "$KURED_NOW")
      fi
      ;;
  esac
  node_errors=0
  while IFS= read -r verdict_line; do
    [ -n "$verdict_line" ] || continue
    n="${verdict_line#* }"
    if [ "${verdict_line%% *}" = "excused" ]; then
      echo "WARNING: node $n NotReady (kured rebooting; verified next run)"
    else
      echo "ERROR: node $n not Ready"
      node_errors=$((node_errors + 1))
    fi
  done <<< "$NODE_VERDICTS"
  # Ready,SchedulingDisabled with no active kured reboot may be a stuck uncordon
  # or an intentional cordon, so WARN rather than ERROR.
  while IFS= read -r n; do
    [ -n "$n" ] || continue
    printf '%s\n' "$KURED_NOW" | grep -qxF "$n" || \
      echo "WARNING: node $n cordoned (Ready,SchedulingDisabled) with no active kured reboot — check for a stuck uncordon"
  done <<< "$(echo "$NODE_OUTPUT" | awk '$2 == "Ready,SchedulingDisabled" {print $1}')"
  if [ "$node_errors" -gt 0 ]; then
    ERRORS=$((ERRORS + node_errors))
  else
    echo "All nodes Ready${KURED_NOW:+ (kured-rebooting node(s) excused)}"
  fi
fi

echo ""
echo "Checking for unhealthy pods..."
# Report genuinely-unhealthy pods only: exclude Completed/Succeeded and re-check
# after a grace window.
list_unhealthy() {
  kubectl get pods -A --no-headers 2>/dev/null | list_unhealthy_pods
}
BAD=$(list_unhealthy) || {
  echo "ERROR: failed to query pods"
  ERRORS=$((ERRORS + 1))
  BAD=""
}
if [ -n "$BAD" ]; then
  sleep 25
  # Keep the pre-grace snapshot if the re-query fails, so an API blip cannot
  # clear BAD and read as "All pods healthy".
  if RECHECK=$(list_unhealthy); then
    BAD="$RECHECK"
  else
    echo "ERROR: failed to re-query pods after grace (keeping pre-grace result)"
    ERRORS=$((ERRORS + 1))
  fi
fi
if [ -n "$BAD" ]; then
  KURED_NOW=$(kured_rebooting_nodes)
  # Excuse an unhealthy pod only while kured is mid-reboot AND the pod is on a
  # rebooting node or unscheduled; a pod that went bad elsewhere in the same
  # window is reported next run.
  pn_ok=true
  if ! POD_NODES=$(kubectl get pods -A \
      -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}{"\t"}{.spec.nodeName}{"\n"}{end}' \
      2>/dev/null); then
    pn_ok=false
    POD_NODES=""
  fi
  pod_errors=0
  pod_warn=""
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    pkey=$(echo "$line" | awk '{print $1"/"$2}')
    pstatus=$(echo "$line" | awk '{print $4}')
    if [ -z "$KURED_NOW" ]; then
      echo "ERROR: pod $pkey unhealthy (status $pstatus)"
      pod_errors=$((pod_errors + 1))
      continue
    fi
    if [ "$pn_ok" = false ]; then
      # kured active but the pod->node lookup failed: WARN as undetermined.
      pod_warn="${pod_warn}  $pkey ($pstatus, node lookup inconclusive)"$'\n'
      continue
    fi
    pnode=$(printf '%s\n' "$POD_NODES" | awk -F'\t' -v p="$pkey" '$1 == p {print $2}')
    if [ -z "$pnode" ] || printf '%s\n' "$KURED_NOW" | grep -qxF "$pnode"; then
      pod_warn="${pod_warn}  $pkey ($pstatus, node ${pnode:-<unscheduled>})"$'\n'
    else
      echo "ERROR: pod $pkey unhealthy on a healthy node (status $pstatus, node $pnode)"
      pod_errors=$((pod_errors + 1))
    fi
  done <<< "$BAD"
  if [ -n "$pod_warn" ]; then
    echo "WARNING: pod(s) excused while kured is rebooting node(s) - not failing (verified next run):"
    printf '%s' "$pod_warn"
  fi
  ERRORS=$((ERRORS + pod_errors))
else
  echo "All pods healthy"
fi

echo ""
echo "Checking critical deployments..."
# The platform deployments every generated cluster runs. One kured snapshot for
# this loop: it completes well inside a reboot cycle.
KURED_NOW=$(kured_rebooting_nodes)
for dep in traefik:traefik coredns:kube-system cert-manager:cert-manager metallb-controller:metallb-system authentik-server:authentik; do
  name="${dep%%:*}"
  ns="${dep##*:}"
  # Wrapped in `if` so one lookup failure does not abort the loop under set -e.
  if DEP_REPLICAS=$(kubectl get deployment "$name" -n "$ns" -o jsonpath='{.status.availableReplicas} {.spec.replicas}' 2>/dev/null); then
    AVAIL="${DEP_REPLICAS%% *}"
    DESIRED="${DEP_REPLICAS##* }"
    if deployment_replicas_ok "$AVAIL" "$DESIRED"; then
      echo "  $name ($ns): ${AVAIL:-0}/${DESIRED:-1} available"
      continue
    fi
    # Excuse only when one of THIS deployment's pods is on a rebooting node; the
    # pod-name anchor keeps sibling deployments out.
    dep_excuse=no
    if [ -n "$KURED_NOW" ]; then
      if DEP_PODNODES=$(kubectl get pods -n "$ns" -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.nodeName}{"\n"}{end}' 2>/dev/null); then
        DEP_NODES=$(echo "$DEP_PODNODES" | deployment_pod_nodes "$name")
        # A replica on a rebooting node or unscheduled is the same transient the
        # pod check excuses.
        if printf '%s\n' "$DEP_NODES" | grep -qxF '<unscheduled>' \
           || printf '%s\n' "$DEP_NODES" | grep -qxFf <(printf '%s\n' "$KURED_NOW"); then
          dep_excuse=yes
        fi
      else
        dep_excuse=undetermined
      fi
    fi
    if [ "$dep_excuse" = no ]; then
      # Grace: a deployment can be briefly under-replicated during a roll.
      sleep 10
      if DEP2=$(kubectl get deployment "$name" -n "$ns" -o jsonpath='{.status.availableReplicas} {.spec.replicas}' 2>/dev/null) \
         && deployment_replicas_ok "${DEP2%% *}" "${DEP2##* }"; then
        echo "  $name ($ns): ${DEP2%% *}/${DEP2##* } available (recovered after grace)"
      else
        echo "  ERROR: $name ($ns): ${AVAIL:-0}/${DESIRED:-?} available"
        ERRORS=$((ERRORS + 1))
      fi
    else
      sfx=""
      [ "$dep_excuse" = undetermined ] && sfx=" (pod-node lookup inconclusive)"
      echo "  WARNING: $name ($ns): ${AVAIL:-0}/${DESIRED:-?} available (kured rebooting; verified next run)$sfx"
    fi
  else
    echo "  ERROR: $name ($ns): deployment lookup failed"
    ERRORS=$((ERRORS + 1))
  fi
done

echo ""
echo "Checking for failed Jobs..."
# Terminal Failed Jobs are checked here because list_unhealthy_pods skips them:
# Failed=True without Complete=True on a non-CronJob Job. The kured excuse is
# node-scoped; a TTL-cleaned Job is ambiguous and WARNs.
KURED_NOW=$(kured_rebooting_nodes)
# Query and filter are split so a query failure ERRORs instead of reading as
# "no failed Jobs".
jobs_query_failed=false
if JOBS_RAW=$(kubectl get jobs -A --request-timeout=15s \
  -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}{"\t"}{.metadata.ownerReferences[0].kind}{"\t"}{range .status.conditions[*]}{.type}={.status},{end}{"\n"}{end}' \
  2>/dev/null); then
  FAILED_JOBS=$(printf '%s\n' "$JOBS_RAW" | awk -F'\t' '$2 != "CronJob" && $3 ~ /Failed=True/ && $3 !~ /Complete=True/ {print $1}')
else
  echo "ERROR: failed to query Jobs (kubectl get jobs -A failed) - cannot verify failed Jobs this run"
  ERRORS=$((ERRORS + 1))
  FAILED_JOBS=""
  jobs_query_failed=true
fi
job_errors=0
job_warn=""
while IFS= read -r jk; do
  [ -n "$jk" ] || continue
  jns="${jk%%/*}"
  jname="${jk##*/}"
  if [ -z "$KURED_NOW" ]; then
    echo "ERROR: failed Job $jk (terminal Failed condition)"
    job_errors=$((job_errors + 1))
    continue
  fi
  # A failed pod lookup cannot node-scope the excuse, so it is a real ERROR.
  if ! JOB_NODES=$(kubectl get pods -n "$jns" -l batch.kubernetes.io/job-name="$jname" --request-timeout=15s \
    -o jsonpath='{range .items[*]}{.spec.nodeName}{"\n"}{end}' 2>/dev/null); then
    echo "ERROR: failed Job $jk (could not query its pods to confirm a kured excuse)"
    job_errors=$((job_errors + 1))
    continue
  fi
  if [ -z "$JOB_NODES" ]; then
    # No pod left to attribute a node to, so this WARNs while kured is active.
    job_warn="${job_warn} $jk(pods gone; kured active)"
  # Capture-and-test, NOT `! ... | grep -q`: under pipefail grep -q's early pipe
  # close SIGPIPEs the upstream, so `!` would wrongly excuse a real failure.
  # The list is empty iff every job pod is unscheduled or on a rebooting node.
  elif [ -z "$(printf '%s\n' "$JOB_NODES" | grep -v '^$' | grep -vxFf <(printf '%s\n' "$KURED_NOW"))" ]; then
    # Every pod unscheduled or on a rebooting node; one failure on a healthy
    # node would have fallen through to the ERROR below.
    job_warn="${job_warn} $jk(all pods unscheduled or on a kured-rebooting node)"
  else
    echo "ERROR: failed Job $jk (a pod failed on a healthy node, not a kured transient)"
    job_errors=$((job_errors + 1))
  fi
done <<< "$FAILED_JOBS"
if [ -n "$job_warn" ]; then
  echo "WARNING: failed Job(s) excused while kured is rebooting - not failing (verified next run):$job_warn"
fi
if [ "$job_errors" -eq 0 ] && [ -z "$job_warn" ] && [ "$jobs_query_failed" = false ]; then
  echo "No failed Jobs"
fi
ERRORS=$((ERRORS + job_errors))

echo ""
echo "Checking cluster DNS (internal service resolution)..."
# One-off busybox pod in kube-system (`default` is PSA restricted plus an egress
# deny, so a probe there fails for reasons that are not DNS). Do NOT use
# `kubectl run --attach --rm` - attach races a fast pod and loses its stdout.
kctl_timeout="--request-timeout=15s"
dns_ns="kube-system"
dns_ok=false
dns_saw_fail=false
dns_last_output=""
for dns_attempt in 1 2 3 4 5; do
  dns_pod="dns-verify-${CI_JOB_ID:-$$}-${dns_attempt}"
  # Clear a leftover pod of the same name so its logs cannot be read as a verdict.
  kubectl delete pod "$dns_pod" -n "$dns_ns" "$kctl_timeout" --ignore-not-found --wait=true >/dev/null 2>&1 || true
  # The busybox pin is gated against busybox_version in group_vars/all.yml by
  # `task lint:busybox-version-pin`; bump both together.
  if ! kubectl run "$dns_pod" -n "$dns_ns" "$kctl_timeout" --restart=Never --image=busybox:1.38 --command -- \
      sh -c "nslookup kubernetes.default.svc.cluster.local >/dev/null 2>&1 && echo DNS_PASS || echo DNS_FAIL" \
      >/dev/null 2>&1; then
    dns_last_output="failed to create DNS probe pod $dns_pod"
    # A client-side timeout can still have created the pod; delete best-effort.
    kubectl delete pod "$dns_pod" -n "$dns_ns" "$kctl_timeout" --ignore-not-found --wait=false >/dev/null 2>&1 || true
    sleep 5
    continue
  fi
  # Poll for a terminal phase; ~40s budget covers image pull.
  dns_phase=""
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    dns_phase=$(kubectl get pod "$dns_pod" -n "$dns_ns" "$kctl_timeout" -o jsonpath='{.status.phase}' 2>/dev/null || true)
    case "$dns_phase" in Succeeded | Failed) break ;; esac
    sleep 2
  done
  if [ "$dns_phase" != "Succeeded" ] && [ "$dns_phase" != "Failed" ]; then
    # Never finished: a scheduling delay, not a DNS verdict. Clean up and retry.
    dns_last_output="DNS probe pod did not finish (last phase: ${dns_phase:-unknown})"
    kubectl delete pod "$dns_pod" -n "$dns_ns" "$kctl_timeout" --ignore-not-found --wait=false >/dev/null 2>&1 || true
    sleep 5
    continue
  fi
  # Read the verdict; retry briefly since log publication can lag pod completion.
  DNS_OUTPUT=""
  for _ in 1 2 3 4 5; do
    DNS_OUTPUT=$(kubectl logs "$dns_pod" -n "$dns_ns" "$kctl_timeout" 2>&1 || true)
    if echo "$DNS_OUTPUT" | grep -qE "DNS_PASS|DNS_FAIL"; then break; fi
    sleep 1
  done
  kubectl delete pod "$dns_pod" -n "$dns_ns" "$kctl_timeout" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  # Three outcomes: DNS_PASS, DNS_FAIL, or no marker at all (the pod finished but
  # the logs never yielded a verdict), so the error below attributes it honestly.
  if echo "$DNS_OUTPUT" | grep -q "DNS_PASS"; then
    dns_ok=true
    dns_last_output="$DNS_OUTPUT"
    break
  elif echo "$DNS_OUTPUT" | grep -q "DNS_FAIL"; then
    dns_saw_fail=true
    dns_last_output="$DNS_OUTPUT"
  else
    dns_last_output="DNS probe produced no verdict (phase: ${dns_phase:-unknown}; logs: ${DNS_OUTPUT:-<empty>})"
  fi
  sleep 5
done
if [ "$dns_ok" = true ]; then
  echo "Cluster DNS: OK (kubernetes.default.svc.cluster.local resolves)"
elif [ "$dns_saw_fail" = true ]; then
  echo "ERROR: Cluster DNS cannot resolve kubernetes.default.svc.cluster.local"
  echo "$dns_last_output"
  ERRORS=$((ERRORS + 1))
else
  echo "ERROR: cluster DNS probe could not run after retries (pod scheduling/API/image issue — not necessarily DNS itself):"
  echo "$dns_last_output"
  ERRORS=$((ERRORS + 1))
fi

echo ""
if [ "$ERRORS" -gt 0 ]; then
  echo "=== VERIFICATION FAILED: $ERRORS critical issue(s) ==="
  exit 1
fi
echo "=== Post-Maintenance Verification Passed ==="
