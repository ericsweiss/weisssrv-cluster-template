# Triage a firing alert

`kubernetes/infrastructure/observability/README.md` owns the rule groups and the
alert routing. `docs/RUNBOOKS.md` § Alerts that link here routes an alert to
its remediation. This page is the order to work in. Every step is read-only
until the last one.

## 1. Read the alert itself

- The notification names the alert, its `severity` and its labels. The
  `runbook_url` annotation points at the `docs/RUNBOOKS.md` heading that owns
  the fix. Open it before anything else.
- A `runbook_url` with no `#anchor` lands at the top of `docs/RUNBOOKS.md`. The
  § Alerts that link here table there routes it.

## 2. Find the rule

- A platform rule lives in `additionalPrometheusRulesMap` in
  `kubernetes/infrastructure/observability/kube-prometheus-stack/release.yaml`.
  An app's rule is a `PrometheusRule` in that app's directory. The Loki ruler
  rules are under `kubernetes/infrastructure/observability/loki/`.
  `grep -rn "alert: <Name>" kubernetes/` finds any of them.
- No match means a kube-prometheus-stack built-in rule. Its overrides are
  `defaultRules.disabled` in the same `release.yaml`.
- The rule's `for:` is the longest benign run. A rule that fires too often is
  a rule change, not a silence.

## 3. Look at live state

The Prometheus and Alertmanager UIs are not published. Port-forward them, with
`KUBECONFIG` set as `references/cluster-access.md` describes:

```bash
kubectl -n observability port-forward svc/kube-prometheus-stack-alertmanager 9093:9093
kubectl -n observability port-forward svc/kube-prometheus-stack-prometheus 9090:9090
```

- Alertmanager shows what is firing, what is silenced and what is inhibited.
  An alert that fired in Prometheus and never arrived is an inhibition or a
  route.
- In Prometheus, run the rule's `expr` without the comparison to see the raw
  series. The labels it returns are the ones the alert carries.
- Query Loki in Grafana for host logs rather than opening a shell on each host.
  A host-level fault usually shows as a pattern across hosts.

## 4. Act

- A consequence never pages beside its cause. Fix the cause first.
- Fix it through the normal change flow: branch, MR, merge. Flux or the deploy
  job applies it.
- A silence covers a planned maintenance window only:
  `task observability:silence ALERT=<name> DURATION=2H`. There are no standing
  silences.
- Every alert must demand an action. One that fires with nothing to do is
  retuned or removed through `references/add-k8s-app.md` § Alert rules, with its
  promtool test, and proved with `task lint:prometheus-config`.
