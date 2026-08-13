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
| `cluster_etcd_endpoints` | `'["10.0.0.31", "10.0.0.32", "10.0.0.33"]'` | `kubeEtcd` scrape targets — a JSON flow sequence, see below |
| `cluster_node_exporter_host_addresses` | `'[{"addresses": ["10.0.0.11"], "conditions": {"ready": true}}, …]'` | `exporters/node-exporter-host.yaml` EndpointSlice roster — also a flow sequence |
| `cluster_unbound_exporter_addresses` | `'[{"addresses": ["10.0.0.21"], "conditions": {"ready": true}}, …]'` | `exporters/unbound-exporter.yaml` roster, one entry per resolver |
| `cluster_zfs_exporter_addresses` | `'[{"addresses": ["10.0.0.11"], "conditions": {"ready": true}}]'` | `exporters/zfs-exporter.yaml` roster (ZFS storage backend only) |
| `cluster_offsite_backup_probe_metric` | `up` | gates `OffsiteBackupStale`'s `absent()` arm — see `platform.backups` |
| `cluster_runbook_base_url` | `https://git.example.com/ops/cluster/-/blob/main/docs` | every alert's `runbook_url` (see below) |
| `cluster_node_exporter_job_regex` | `node-exporter\|node-exporter-host` | node/storage alert job scoping — see below |

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

`platform.backups` reads textfile metrics that only exist because the host
node-exporter job below is scraped. `OffsiteBackupStale`'s `absent()` arm is
gated by `${cluster_offsite_backup_probe_metric}`, which ships as `up` (inert) —
flip it to `restic_offsite_last_success_timestamp_seconds` in the same change
that sets `restic_offsite_enabled: true`, or that arm stays switched off. The
gate is a metric name rather than a boolean because `${...}` is only valid PromQL
inside a quoted string, and these rules are linted before substitution.

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
`job=~"${cluster_node_exporter_job_regex}"`.

**This cluster ships that second job.** `exporters/node-exporter-host.yaml` is a
selectorless headless `Service` + a hand-maintained `Endpoints` list +
a `ServiceMonitor` with `jobLabel: app.kubernetes.io/name`, giving
`job="node-exporter-host"` over the `:9101` exporters the `node_exporter_host`
Ansible role installs on the Proxmox hosts, the resolver and relay LXCs, and the
k3s servers. That layer carries the hardware metrics (hwmon, SMART, NIC, disk
I/O) no VM can see, and the textfile metrics — off-node etcd snapshot freshness,
offsite-backup timestamps — that `platform.backups` alerts on.

> **Set `cluster_node_exporter_job_regex` to `node-exporter|node-exporter-host`.**
> The address roster is shipped, but the *rules* only cover what the regex
> matches. Left at `node-exporter` alone, the hosts are scraped and graphed while
> none of the filesystem, inode or node-condition alerts apply to them — the
> quietest possible failure.

The address roster itself lives with `cluster_node_exporter_host_addresses` in
`infrastructure/sources/cluster-config.yaml` (a JSON flow sequence, same reason
as the etcd endpoints), annotated host by host. The k3s **agents** are
deliberately excluded: the in-cluster DaemonSet already covers them on `:9100`,
and listing them would double every agent's series under two jobs.

### The other two host exporters

`exporters/unbound-exporter.yaml` (`:9167`, every resolver) and
`exporters/zfs-exporter.yaml` (`:9134`, the storage node, ZFS backend only) have
the same shape and take their rosters from `cluster_unbound_exporter_addresses`
and `cluster_zfs_exporter_addresses`. Both ship the **scrape only** — their
series are there for dashboards and for rules you add, and no shipped rule
selects them.

Pool integrity is deliberately not built on `zfs_exporter`: the
`zfs_pool_status_*` series that a `ZFSPoolNotOnline` / `ZFSPoolDeviceErrors` /
`ZFSPoolSpace*` set wants come from the `node_exporter_host` zpool textfile
collector, which covers **every** host with a pool rather than only the storage
node. That collector is already running — it follows `node_exporter_host_proxmox`,
which `group_vars/proxmox.yml` sets true — so the series are being scraped and
only the rules are missing: add them under `platform-storage:` in
`kube-prometheus-stack/release.yaml` (group `platform.storage`).

## Blackbox probe targets

`exporters/blackbox-exporter.yaml` ships a starter target list: only endpoints
this template guarantees exist, each composed from `cluster-config` so there are
no literals to update when the domain changes. Add your own to that list, using
one of the modules defined in the same file (`http_2xx`, `http_sso`,
`dns_resolution`, `icmp_ping`, `tcp_connect`):

```yaml
        - name: dns-01
          url: 10.0.0.150:53        # a site address; prefer a cluster-config key
          module: dns_resolution
        - name: public-ingress
          url: https://www.${cluster_external_domain}
          module: http_2xx
```

A target that is legitimately offline much of the time needs its own gated
alert, not a bare `EndpointDown`.

## Dashboards

`dashboards/` carries nine platform dashboards (cluster overview, Flux, alerts
overview, node-exporter-full, Prometheus/Alertmanager self-monitoring,
cert-manager, blackbox, Traefik). Each is a `configMapGenerator` entry with
`disableNameSuffixHash: true` — the name must stay stable or the Grafana sidecar
loses track of it and orphans accumulate.

Add an app dashboard by dropping the JSON next to the app's manifests with the
`grafana_dashboard: "1"` label and a `grafana_folder` annotation; the sidecar
searches all namespaces.

## Grafana is SSO-only, and that is enforced in three places

`kube-prometheus-stack` ships a built-in `admin` account whose password is a
**well-known chart default** (`prom-operator`), and that account is a Grafana
Admin — it can query every datasource, edit dashboards and mint API tokens.
Disabling the login form alone does not close it: the form is a browser affordance,
while HTTP basic auth against `/api/...` is a separate path that keeps working.

The release values therefore set both of:

| Setting | Closes |
|---|---|
| `auth.disable_login_form: true` | the browser login form |
| `auth.basic.enabled: false` | `curl -u admin:<password> .../api/...` |

and replace the chart's well-known default password
(`prom-operator`) with an ESO-managed one:
`grafana.admin.existingSecret: observability-secrets`, reading
`grafana-admin-user` / `grafana-admin-password`. The account therefore *exists*
but authenticates through no open path.

`security.disable_initial_admin_creation` is deliberately **not** set, even
though it looks like the strictest option. Grafana creates the built-in admin
only while the user table is empty, so suppressing it means the account can
never be created at all: after the first OIDC login the table is non-empty, and
flipping the setting back later re-opens the login form against a database that
contains no admin. `grafana cli admin reset-admin-password` fails the same way
("Could not find user named admin"), leaving hand-editing `grafana.db` on the PV
as the only way back in. A password-protected account behind two closed doors is
the safer trade.

Authorization then comes entirely from the OIDC role mapping
(`GF_AUTH_GENERIC_OAUTH_ROLE_ATTRIBUTE_PATH`): the first member of the admin
group to sign in becomes a Grafana Admin. Two consequences:

- **Running without an identity provider** means deleting both settings *and*
  the OIDC block. The vault-backed admin account is then the only way in, which
  is why it is wired up from the start.
- **Break-glass** (the IdP is down, or a group-mapping mistake in the IdP locks
  everyone out): set `auth.disable_login_form: false` and
  `auth.basic.enabled: true`, reconcile, and sign in as `admin` with the vault's
  `Grafana SSO` → `admin-password`. Fix the IdP, then revert both. Do not leave
  that state committed.

### The admin password is applied once, at the first start

The same mechanic that keeps the account creatable makes its password a
one-shot. Grafana reads `GF_SECURITY_ADMIN_PASSWORD` only while it is creating
the account — that is, only while the user table is empty. `persistence.enabled:
true` against the NFS-backed `grafana-data` claim means the database outlives
the pod, the HelmRelease and the node, so there is never a second first start.

Consequences, in the order they bite:

- The vault's `admin-password` must be its **final** value before this stage
  first reconciles. A placeholder typed in phase 0 becomes the live break-glass
  password, and the runbook that sends a locked-out operator to that field sends
  them to a value that no longer matches.
- Changing the vault field later, refreshing the ExternalSecret and restarting
  the Deployment all do exactly nothing to the account: the restart re-reads the
  Secret into an environment variable Grafana no longer consults.
- Rotation is therefore two steps in one change — edit the vault field, then
  make the database agree:

  ```bash
  task flux:refresh-secret -- observability/observability-secrets
  kubectl -n observability exec deploy/kube-prometheus-stack-grafana -c grafana -- \
    grafana cli --homepath /usr/share/grafana admin reset-admin-password '<new>'
  ```

  `--homepath` is not optional: without it the CLI cannot find its config
  defaults and exits before touching the database.

Closing the basic-auth path is also why both Grafana sidecars run with
`skipReload: true` — their default behaviour is to POST Grafana's
`/api/admin/provisioning/*/reload` endpoint using that same admin account, which
would now only ever log a 401. Dashboards are unaffected (Grafana's file
provisioner rescans on its own interval). **Datasources are not**: Grafana reads
those files only at startup, and the Prometheus datasource itself arrives as a
sidecar-discovered ConfigMap — so `sidecar.datasources.initDatasources: true`
runs the sidecar as an init container as well, guaranteeing the files exist
before Grafana boots. Keep `skipReload` and `initDatasources` together; the
chart documents them as a pair.

## etcd

`kubeEtcd` is **enabled**. k3s serves etcd metrics on `:2381` over plain HTTP on
every server node, and enabling this component is what turns on the upstream
etcd rule group — quorum loss, leader flapping, DB-size growth, fsync latency —
on the one component whose failure loses the cluster.

Its `endpoints` list is the single site value here that cannot be an ordinary
substitution, because Helm needs a real YAML list rather than a string. It comes
from `${cluster_etcd_endpoints}` in `cluster-config`, stored as a **JSON flow
sequence** (`'["10.0.0.31", "10.0.0.32", "10.0.0.33"]'`) so that it parses as a
list once Flux substitutes it inline. Re-address the server nodes and update
that key in the same change; a stale entry shows up as a permanently-down etcd
target and an `etcdMembersDown` alert, never as silence.

## GPU telemetry

If this cluster was generated with `gpu: nvidia`, the device plugin in
`infrastructure/controllers/nvidia-device-plugin/` advertises the card to the
scheduler but **exports no metrics at all** — no utilisation, VRAM, temperature,
power or per-pod attribution. That is the one gap in this stage's coverage, and
it matters most under the time-slicing the plugin enables by default, where
several pods share a single card's memory and the first symptom of exhaustion is
a CUDA OOM with nothing on a dashboard to explain it.

NVIDIA's DCGM exporter fills it. It is not shipped because it needs a version
pin, and every pin in this repository is single-sourced from
`ansible/inventories/prod/group_vars/all.yml`. Adding it is four steps:

1. **Pin the chart** — add to `all.yml` under `helm_chart_versions:`:
   ```yaml
   dcgm_exporter: "4.6.1"     # check https://github.com/NVIDIA/dcgm-exporter/releases
   ```
   then `task flux:sync-versions` (this regenerates
   `sources/versions-configmap.yaml`; CI fails if you skip it).
2. **Add the chart repository** — `infrastructure/sources/nvidia-dcgm.yaml`:
   ```yaml
   ---
   apiVersion: source.toolkit.fluxcd.io/v1
   kind: HelmRepository
   metadata:
     name: nvidia-dcgm
     namespace: flux-system
   spec:
     interval: 1h
     url: https://nvidia.github.io/dcgm-exporter/helm-charts
   ```
   and list it in `sources/kustomization.yaml`.
3. **Add the release** — a `dcgm-exporter/` directory in *this* stage with a
   namespace (PSA `privileged`; the DaemonSet mounts the host GPU devices), the
   `netpol-baseline` component, and a HelmRelease whose values carry the same
   `nodeSelector`/`tolerations` as the device plugin
   (`${cluster_node_label_domain}/gpu: nvidia`, tolerating `nvidia.com/gpu`),
   `runtimeClassName: nvidia`, and `serviceMonitor.enabled: true`. Prometheus
   discovers ServiceMonitors in every namespace, so nothing else needs editing.
4. **Add a dashboard** — NVIDIA publishes one (`dashboards/` in the exporter
   repo); drop the JSON into `dashboards/` with a `configMapGenerator` entry and
   `disableNameSuffixHash: true`, like the others here.

Useful first alerts once the metrics exist: `DCGM_FI_DEV_GPU_TEMP` above the
card's slowdown threshold, and `DCGM_FI_DEV_FB_FREE` near zero for longer than a
few minutes (the time-slicing VRAM ceiling).

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
