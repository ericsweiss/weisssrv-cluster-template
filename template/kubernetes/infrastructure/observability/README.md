# infrastructure/observability

The platform observability stage: Prometheus + Alertmanager + Grafana
(kube-prometheus-stack), Loki, the Alloy log-collector DaemonSet, the blackbox
prober, the platform dashboards, and the internal ingress for Grafana and the
Loki push API.

Reconciled by the `infrastructure-observability` Flux Kustomization
(`dependsOn: infrastructure-configs`). A failure here must never block the
`apps` stage — the two are siblings, not a chain.

## No site literals

Nothing in this directory contains a domain, an IP, or a hostname. Every
site-specific value arrives at reconcile time from the `cluster-config`
ConfigMap via `postBuild.substituteFrom` (chart/image versions come from the
sibling `cluster-versions` ConfigMap the same way). Adding a hostname literal
here is a regression — put it in `cluster-config` instead.

### `cluster-config` keys this directory consumes

| Key | Example | Used by |
|---|---|---|
| `cluster_internal_domain` | `int.example.com` | Grafana root URL + OIDC, ingress hosts, certs, SMTP/relay host, blackbox module |
| `cluster_external_domain` | `example.com` | blackbox external probe |
| `cluster_issuer` | `letsencrypt-prod` | Certificates |
| `cluster_secret_store` | `onepassword-cluster` | ExternalSecrets |
| `cluster_node_label_domain` | `int.example.com` | storage-node affinity + tolerations |
| `cluster_nas_host` | `nas-01.int.example.com` | Grafana NFS PV server |
| `cluster_smtp_host` | `smtp-relay.int.example.com` | Alertmanager `smtp_smarthost` |
| `cluster_alert_email` | `ops@example.com` | Alertmanager critical receiver |
| `cluster_api_vip` | `10.0.0.161` | blackbox API-VIP probe |
| `cluster_runbook_base_url` | `https://git.example.com/ops/cluster/-/blob/main/docs` | every alert's `runbook_url` (see below) |
| `cluster_node_exporter_job_regex` | `node-exporter` | node/storage alert job scoping — see below |

### `cluster-versions` keys this directory consumes

`helm_chart_versions_kube_prometheus_stack`, `helm_chart_versions_loki`,
`helm_chart_versions_alloy`, `helm_chart_versions_prometheus_blackbox_exporter`.

## Alert rules

The custom rules live in `additionalPrometheusRulesMap` inside
`kube-prometheus-stack/release.yaml`, in five platform groups:

| Group | Covers |
|---|---|
| `platform.storage` | filesystem bytes/inodes, PVC usage, stray dynamically-provisioned PVs |
| `platform.nodes` | node memory pressure, stuck cordon, stuck kured reboot, deferred maintenance reboot |
| `platform.gitops` | Flux reconcile errors, durable `Ready=False` on any Flux resource |
| `platform.certificates` | cert-manager expiry (warning/critical), edge-cert expiry via blackbox |
| `platform.backups` | etcd off-node snapshot freshness, offsite-repo freshness/verify |

Plus `platform.probes` (blackbox endpoint down, prober down) and
`platform.secrets` (ExternalSecret sync failures) in the same map.

Every rule carries a `runbook_url` pointing at
`${cluster_runbook_base_url}/RUNBOOKS.md`, with three anchors —
`#backups-and-restore`, `#certificates`, `#where-to-look-first`. Keep that file
and those headings, or repoint the annotations: an alert whose runbook 404s at
03:00 is worse than one with no link at all.

Validate rule changes before pushing — `scripts/lint-prometheus-config.sh`
extracts this map and runs `promtool check rules` over it, which catches a bad
PromQL expression that schema validation cannot see.

**App alerts do not go here.** An app module ships its own `PrometheusRule` CR
next to its manifests — Prometheus discovers rules in every namespace
(`ruleSelectorNilUsesHelmValues: false`). Only rules about the *platform*
belong in the release values.

### The node-exporter job regex

Every rule the chart ships from the upstream node-exporter mixin is hard-scoped
to `job="node-exporter"` — the in-cluster DaemonSet. A cluster that also scrapes
bare-metal or VM hosts with a second job (the `node_exporter_host` role's
`:9101` targets, conventionally `job="node-exporter-host"`) gets **no** upstream
coverage on those 15-odd targets: no filesystem, no NIC-error, no textfile-scrape
alerts. That gap is a documented production finding, not a hypothetical.

The rules in this repo therefore never hardcode the job. They select
`job=~"${cluster_node_exporter_job_regex}"`, defaulting to `node-exporter` alone.
When you add host-level scraping, widen the key to
`node-exporter|node-exporter-host` and every storage/node rule covers both
without editing a manifest.

Adding the host job itself is site data (an IP roster), so this repo ships no
`Endpoints` list. The shape is one selectorless headless `Service` + a manual
`Endpoints` list + a `ServiceMonitor` with
`jobLabel: app.kubernetes.io/name` — put it in a site overlay and set the
`app.kubernetes.io/name` label to the job name you want.

## Dashboards

`dashboards/` carries nine platform dashboards (cluster overview, Flux, alerts
overview, node-exporter-full, Prometheus/Alertmanager self-monitoring,
cert-manager, blackbox, Traefik). Each is a `configMapGenerator` entry with
`disableNameSuffixHash: true` — the name must stay stable or the Grafana sidecar
loses track of it and orphans accumulate.

Add an app dashboard by dropping the JSON next to the app's manifests with the
`grafana_dashboard: "1"` label and a `grafana_folder` annotation; the sidecar
searches all namespaces.

## Loki ruler

`loki/kustomization.yaml` generates rule ConfigMaps labelled `loki_rule: "1"`
and annotated `k8s-sidecar-target-directory: /rules/fake`. That chain is subtle
and load-bearing: `auth_enabled: false` makes the single tenant `fake`, and the
ruler's local storage scans `<directory>/<tenant>/`, so the files must land in
`/rules/fake` or the ruler silently loads nothing.

## Secrets backend seam

Three ExternalSecrets live here (Grafana OIDC credentials, the Alertmanager
config template, the Loki push basic-auth htpasswd). They reference the
`ClusterSecretStore` named by `${cluster_secret_store}` and address items by
provider-native `key`/`property`. Swapping the secrets backend means replacing
that one store — the manifests here only need their `remoteRef` addressing
adjusted to the new provider's convention.

## Storage assumptions

Prometheus and Loki bind statically-provisioned local PVs
(`storageClassName: ""`, node-affinity on the `${cluster_node_label_domain}/nas`
label); Grafana binds an NFS PV. A cluster with a real CSI driver can replace
`storage.yaml` in each directory with a `storageClassName` and delete the
`volumeName`/`existingClaim` pins — nothing else changes.

`loki/nodeport.yaml` is deliberately **not** reconciled. An always-on NodePort is
an unauthenticated Loki push/read path on every node IP that bypasses both the
basic-auth ingress and the NetworkPolicy. Apply it by hand only as an emergency
fallback, and delete it when the ingress path is restored.
