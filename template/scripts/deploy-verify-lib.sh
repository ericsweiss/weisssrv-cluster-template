#!/usr/bin/env bash
# Classification helpers for the verify scripts, unit-tested without a live
# cluster by scripts/test_deploy_verify_lib.py. Functions only, safe to `source`
# under `set -e`; each reads stdin or args and writes a verdict to stdout.

# Every helper needs jq, which the cluster-verify job env provides.

# jq fragment: select list items whose Ready condition is missing or not True.
# Shared by the count/name/dump helpers below (and referenced by deploy-verify.sh
# for the projections that print richer per-item detail).
JQ_NOT_READY='select((.status.conditions // []) | map(select(.type == "Ready")) | (length == 0 or .[0].status != "True"))'

# count_not_ready: read a `kubectl get <kind> -o json` list on stdin, print how
# many items are not Ready. 999 only when jq errors; empty input prints nothing,
# so callers keep their own `|| echo 999` guard. This is not fail-closed alone.
count_not_ready() {
  jq "[.items[] | $JQ_NOT_READY] | length" 2>/dev/null || echo "999"
}

# not_ready_ns_names: read a list on stdin, print "  <namespace>/<name>" for each
# not-Ready item (used to enumerate non-Ready ExternalSecrets).
not_ready_ns_names() {
  jq -r ".items[] | $JQ_NOT_READY | \"  \(.metadata.namespace)/\(.metadata.name)\"" || {
    echo "not_ready_ns_names: jq failed, the not-Ready list is unknown" >&2
    return 1
  }
}

# steady_state: given the pre-reconcile count of not-Ready Kustomizations, print
# "true" at exactly 0, where non-Ready ExternalSecrets and pods are failures,
# else "false". A blank or non-numeric count reads as bootstrap.
steady_state() {
  if [ "${1:-}" = "0" ]; then echo "true"; else echo "false"; fi
}

# nodes_not_ready_count: read `kubectl get nodes --no-headers` on stdin, print
# the number of nodes whose STATUS ($2) is not exactly "Ready".
nodes_not_ready_count() {
  awk '$2 != "Ready" {count++} END {print count+0}'
}

# pods_not_running_or_completed: read `kubectl get pods --no-headers` on stdin,
# print the rows whose STATUS ($3) is not Running or Completed (the "bad" set).
pods_not_running_or_completed() {
  awk '$3 !~ /^(Running|Completed)$/'
}

# pods_non_transient: read pod rows on stdin, print those whose STATUS ($3) is NOT
# on the transient allowlist — i.e. the genuinely-failing pods that fail a verify
# even during bootstrap/recovery. Feed it the pods_not_running_or_completed set.
pods_non_transient() {
  awk '$3 !~ /^(Pending|ContainerCreating|PodInitializing|Terminating|Init:[0-9]+\/[0-9]+)$/'
}

# pods_running_unready: read pod rows on stdin, print the Running pods whose READY
# column ($2, "a/b") has a != b (a failing readiness probe, container not ready).
pods_running_unready() {
  awk '$3=="Running"{split($2,a,"/"); if(a[1]!=a[2]) print}'
}

# helmreleases_not_ready_names: read `kubectl get helmreleases -o json` on stdin,
# print the .metadata.name of each HR whose Ready condition is missing or not True.
helmreleases_not_ready_names() {
  jq -r ".items[] | $JQ_NOT_READY | .metadata.name" || {
    echo "helmreleases_not_ready_names: jq failed, HelmRelease readiness is unknown" >&2
    return 1
  }
}

# helmreleases_hard_failed: read HR JSON on stdin, print each HR failing hard even
# during bootstrap — Ready != True plus a terminal reason (Install/Upgrade/Test/
# RollbackFailed) or .status.failures > 0. Unreadable input escalates too.
helmreleases_hard_failed() {
  jq -r '
    .items[]
    | (.status.conditions // [] | map(select(.type=="Ready")) | .[0]) as $ready
    | select(
        $ready.status != "True"
        and (
          (($ready.reason // "") | test("InstallFailed|UpgradeFailed|TestFailed|RollbackFailed"))
          or ((.status.failures // 0) > 0)
        )
      )
    | .metadata.name' || {
    echo "helmreleases_hard_failed: jq failed, hard-failure state is unknown" >&2
    return 1
  }
}
