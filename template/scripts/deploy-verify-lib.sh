#!/usr/bin/env bash
# Readiness classifiers a consumer's deploy-verify driver sources. Functions
# only, safe to `source` under a caller's `set -e`; each reads a kubectl list on
# stdin and writes a verdict to stdout. Contract: docs/SCRIPTS.md.

# Every helper here needs jq. The driver stays site-shaped; only the
# classification lives in this file, so one implementation is tested once.

# jq fragment: list items whose Ready condition is missing or not True.
JQ_NOT_READY='select((.status.conditions // []) | map(select(.type == "Ready")) | (length == 0 or .[0].status != "True"))'

# CRITICAL: a swallowed jq error reads as "nothing is wrong". The name helpers
# print to stderr and return non-zero so an empty list cannot pass for a clean
# one; count_not_ready prints 999, which only a numeric caller can act on, so it
# is not fail-closed on its own.

# count_not_ready: read a `kubectl get <kind> -o json` list on stdin, print how
# many items are not Ready. 999 only when jq errors; empty input prints nothing,
# so callers keep their own `|| echo 999` guard.
count_not_ready() {
  jq "[.items[] | $JQ_NOT_READY] | length" 2>/dev/null || echo "999"
}

# not_ready_ns_names: read a list on stdin, print "  <namespace>/<name>" for
# each not-Ready item.
not_ready_ns_names() {
  jq -r ".items[] | $JQ_NOT_READY | \"  \(.metadata.namespace)/\(.metadata.name)\"" || {
    echo "not_ready_ns_names: jq failed, the not-Ready list is unknown" >&2
    return 1
  }
}

# steady_state: given the pre-reconcile count of not-Ready Kustomizations, print
# "true" at exactly 0, where non-Ready dependants are failures, else "false". A
# blank or non-numeric count reads as bootstrap.
steady_state() {
  if [ "${1:-}" = "0" ]; then echo "true"; else echo "false"; fi
}

# nodes_not_ready_count: read `kubectl get nodes --no-headers` on stdin, print
# the number of nodes whose STATUS ($2) is not exactly "Ready".
nodes_not_ready_count() {
  awk '$2 != "Ready" {count++} END {print count+0}'
}

# pods_not_running_or_completed: read `kubectl get pods --no-headers` on stdin,
# print the rows whose STATUS ($3) is not Running or Completed.
pods_not_running_or_completed() {
  awk '$3 !~ /^(Running|Completed)$/'
}

# pods_non_transient: read pod rows on stdin, print those whose STATUS ($3) is
# NOT on the transient allowlist, so they fail a verify even during bootstrap.
# Feed it the pods_not_running_or_completed set.
pods_non_transient() {
  awk '$3 !~ /^(Pending|ContainerCreating|PodInitializing|Terminating|Init:[0-9]+\/[0-9]+)$/'
}

# pods_running_unready: read pod rows on stdin, print the Running pods whose
# READY column ($2, "a/b") has a != b (a failing readiness probe).
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

# helmreleases_hard_failed: read HR JSON on stdin, print each HR that fails hard
# even during bootstrap: Ready != True and either a terminal Ready reason
# (Install/Upgrade/Test/RollbackFailed) or .status.failures > 0.
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
