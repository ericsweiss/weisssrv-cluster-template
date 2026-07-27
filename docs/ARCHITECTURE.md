# Architecture

The shape a generated cluster takes, and why. This is the reference for
understanding an existing cluster or deciding whether the template fits your
plans; [PRE-SETUP.md](PRE-SETUP.md) and [SETUP.md](SETUP.md) are the procedures.

---

## Two lifecycles, one repository

Everything below the Kubernetes API is **pushed** by Ansible. Everything above it
is **pulled** by Flux. The boundary is deliberate and absolute:

```
  ansible/ ───push──▶  Proxmox hosts, LXC guests, VMs, k3s nodes
                       (idempotent, run on demand or from CI)

  kubernetes/ ◀──pull── Flux controllers in the cluster
                       (reconciled continuously; git is the only input)

  terraform/ ───push──▶ things outside both: public DNS, tailnet policy, SSO objects
```

Consequences worth internalising:

- A change under `kubernetes/` is deployed by committing it. `kubectl apply` and
  `helm upgrade` are diagnostics and break-glass, not deployment — Flux reverts
  drift on its next pass.
- A change under `ansible/` is deployed by running the playbook. Nothing watches
  it.
- Versions live in **one** place, `ansible/inventories/prod/group_vars/all.yml`,
  and flow into the cluster through a generated ConfigMap. No manifest names a
  version directly.

---

## Layers

| Layer | Owned by | Contents |
|---|---|---|
| Hardware, pools | you, by hand | ZFS pools; the template never creates or destroys one |
| Hosts | Ansible (`weisssrv.infra` roles) | users, SSH, packages, resolver, exporters, firewall, tuning |
| Storage services | Ansible | datasets, zvols, NFS exports (TLS), SMB, SMART, ARC limits |
| Network services | Ansible | filtering + validating resolvers, SMTP relay, ACME client |
| Guests | Ansible | LXC containers and VMs from cloud-init, HA rules, backups, replication |
| Kubernetes nodes | Ansible | k3s servers/agents, kube-vip, labels and taints |
| Platform | Flux | ingress, certificates, secrets, load balancing, autoscaling, observability |
| Applications | Flux | one directory per application under `kubernetes/apps/` |
| External state | Terraform | public DNS zone, tailnet ACL, SSO objects |

No Ansible role is vendored into a generated cluster. Playbooks address
`weisssrv.infra.<role>` by fully-qualified name, and `ansible/requirements.yml`
pins the collection at `lib_ref` — so a platform upgrade is a one-line, reviewable
bump, and a role fix benefits every cluster generated from the template.

---

## The Flux stage graph

Six top-level `Kustomization`s in `flux-system`, reconciled in `dependsOn` order:

```
             infrastructure-sources          HelmRepository CRs,
                      │                      cluster-config + cluster-versions
                      ▼
             infrastructure-crds             CRDs that later stages reference
                      │                      (wait: true — Established before use)
                      ▼
             infrastructure-controllers      cert-manager, external-secrets,
                      │                      MetalLB, Traefik, external-dns,
                      │                      VPA, reloader, coordinated reboots
                      ▼
             infrastructure-configs          ClusterIssuer, ClusterSecretStore,
                      │                      address pools, middlewares, policies
            ┌─────────┴─────────┐
            ▼                   ▼
   infrastructure-        apps            (parallel on purpose)
   observability
```

Why it is split this way:

- **CRDs get their own stage** because a controller that emits a `ServiceMonitor`
  cannot render before the monitoring CRDs are Established. Bundling them with
  the controllers makes a *fresh* bootstrap order-dependent and flaky; a
  dedicated stage with `wait: true` makes it deterministic.
- **Configs depend on controllers** because they are custom resources whose CRDs
  the controllers install.
- **Applications branch off configs, not observability.** A failing metrics stack
  upgrade must not freeze application reconciliation.

Each stage's `kustomization.yaml` is the authoritative membership list. Tenant
repositories — applications that live in their own git repository, generated from
`weisssrv-app-template` — are onboarded as additional `Kustomization`s under
`kubernetes/clusters/<cluster_name>/tenants/`.

---

## Substitution: how site values reach manifests

**Kubernetes manifests contain no domains and no addresses.** This is the single
most important rule in the template, and the one it exists to enforce: the
reference cluster it was generalized from accumulated 411 hard-coded domain
literals and 200 hard-coded LAN addresses across 122 files, which is what makes
such a repository impossible to fork.

Instead, two ConfigMaps live in the first stage and every Kustomization
substitutes from both:

```yaml
postBuild:
  substituteFrom:
    - kind: ConfigMap
      name: cluster-config      # site identity: domains, addresses, CIDRs
      optional: false
    - kind: ConfigMap
      name: cluster-versions    # every chart and image version
      optional: false
```

Manifests reference the keys as `${cluster_internal_domain}`,
`${cluster_api_vip}`, `${traefik_version}` and so on; kustomize-controller
resolves them at reconcile time.

`cluster-config` carries the site's identity. The file itself is the
authoritative list; the groups are:

| Group | Keys |
|---|---|
| Identity | `cluster_name` |
| Zones | `cluster_internal_domain`, `cluster_external_domain`, `cluster_node_label_domain` |
| Networks | `cluster_lan_cidr`, `cluster_pod_cidr`, `cluster_service_cidr`, `cluster_tailnet_cidr` |
| Addresses | `cluster_k3s_api_vip` (and its `cluster_api_vip` alias), `cluster_apiserver_egress_cidr`, `cluster_metallb_public_vip`, `cluster_metallb_internal_vip` |
| Certificates | `cluster_issuer`, `cluster_acme_email` |
| Secrets | `cluster_secret_store`, `cluster_secrets_vault` |
| Git | `cluster_git_host`, `cluster_runbook_base_url` |
| Host services | `cluster_nas_host`, `cluster_smtp_host`, `cluster_alert_email` |

Two of these have teeth. `cluster_node_label_domain` must match the prefix the
Ansible k3s layer actually applies to nodes, or every affinity rule silently
matches nothing. `cluster_apiserver_egress_cidr` must list the **server node
addresses**, not the API VIP — kube-proxy rewrites the destination before
NetworkPolicy is evaluated, so allowing the VIP allows nothing.

The second one ships **deliberately wide and needs narrowing by hand.** The
server addresses live in the inventory, not in a copier answer, so the generator
cannot know them: it writes the whole LAN CIDR and says so in a comment. Until
you replace it with the server `/32`s — a post-inventory step called out in
[SETUP.md](SETUP.md) § 2 — the API-server egress allowance is LAN-wide. It is
the one shipped value in `cluster-config` that is knowingly looser than the rule
above.

A renamed or missing key fails **quietly in the cluster**: Flux substitutes an
unknown `${placeholder}` with an empty string rather than erroring, so the
object applies and the misconfiguration surfaces later as a service listening on
nothing. Catching it is the gate's job, not the cluster's — see below.

`cluster-versions` is generated from `group_vars/all.yml` by
`task flux:sync-versions` and drift-gated in CI, so a version can only change in
one place.

The division of labour: **copier fills the ConfigMap and genuinely structural
spots** — a directory named after the cluster, a Terraform variable file, an
Ansible group_var. It does not interpolate answers into manifests. When an
address changes afterwards you edit one ConfigMap key, not a hundred files.

`task flux:lint` builds every Kustomization and substitutes with an allowlist
built from the two ConfigMaps, so an unknown placeholder survives verbatim into
the rendered output and fails the job. That, plus the render invariant tests, is
what stands between a typo and a silently empty value in production.

---

## Network

One flat LAN, three floating addresses:

| Address | Held by | Purpose |
|---|---|---|
| `k3s_api_vip` | kube-vip, on a server node | HA endpoint for the Kubernetes API |
| `metallb_public_vip` | MetalLB, on an ingress-labelled agent | internet-facing ingress |
| `metallb_internal_vip` | MetalLB | LAN-only ingress |

Inside the cluster: k3s' default pod (`10.42.0.0/16`) and service
(`10.43.0.0/16`) networks, with flannel using WireGuard-native encryption for
node-to-node traffic. Both are ConfigMap keys and both are fixed at install time.

Firewall policy is default-deny at the hypervisor, expressed as IP sets and
security groups derived from the inventory rather than written out per host, so
adding a node updates every rule that mentions it. In-cluster, a baseline
component applies default-deny ingress per namespace and each workload opens
exactly what it needs.

---

## DNS

Split-horizon, two zones, no overlap:

```
LAN client                          Internet client
    │                                     │
    ▼                                     ▼
filtering resolver (x2, HA)         public authoritative zone
    │  rewrites *.internal_domain          (managed by Terraform +
    │  to the internal ingress VIP          external-dns from Ingress objects)
    ▼                                     │
validating recursive resolver             ▼
    │  DNSSEC, DNS-over-TLS upstream   public ingress VIP
    ▼
upstream resolvers
```

The internal zone never leaves the LAN, so internal service names cannot leak
and internal addresses are never published. The external zone is managed as
code: Terraform owns the records that must exist before the cluster does,
external-dns owns the ones derived from live ingress objects, and cert-manager
proves ownership over DNS-01 for both zones.

Two resolver instances run as HA-managed guests on different hosts; the second
syncs its filtering configuration from the first.

---

## Storage

Tiered ZFS on one storage node, exposed three ways:

| Path | Mechanism | Used by |
|---|---|---|
| bulk / share datasets | NFS export | media and shared data, mounted by pods and guests |
| app-data zvols | passed through as a block device to a VM | databases, anything wanting a real disk |
| app-data datasets | NFS export | Kubernetes `PersistentVolume`s |

Rules the template enforces:

- **Pools are created by hand.** Ansible sets properties, creates datasets and
  zvols, and mounts them. It never runs `zpool create` or `zpool destroy`.
- **Every PV is static.** No dynamic provisioner, and `storageClassName: ""`
  everywhere, so nothing can land on the cluster-default local path — which lives
  on a stateless VM disk excluded from every backup. An alert fires if anything
  ever does.
- **NFS is authenticated in transit.** Exports require TLS (`xprtsec=tls`) and
  mounts address the server **by hostname**, because the certificate has no
  address in its SAN.
- **Application state survives its consumer.** A zvol outlives the VM it is
  attached to; a PV outlives the pod and the node.

At rest, datasets holding anything sensitive are their own encryption roots, with
passphrases fetched from the secrets backend at boot by a unit ordered before the
mount.

---

## Secrets

Two consumers, one source, no plaintext anywhere in git:

```
                    ┌─────────────────────────────────┐
                    │  vault (secrets_backend)        │
                    └───────────┬──────────┬──────────┘
       op:// references at      │          │   replicated to
       run time (op run --)     │          │   an in-cluster Connect server
                                ▼          ▼
                    Ansible, Terraform,   External Secrets Operator
                    Taskfile, CI          → ExternalSecret → Kubernetes Secret
```

- Host-side tooling never stores a credential; it injects
  `op://<vault>/<item>/<field>` at the moment of use.
- In-cluster, an `ExternalSecret` names the item and field it wants and ESO
  produces the `Secret`. Workloads consume the produced Secret and know nothing
  about the backend.
- Exactly **two** Kubernetes Secrets are created by hand — the Connect server's
  credentials and its access token. They are what the machinery uses to
  authenticate to its own source, so they cannot bootstrap themselves.
- The in-cluster path talks to Connect, not to a cloud API, so reconciliation
  does not depend on internet reachability or a rate limit.

---

## Observability

Metrics, logs and alerts are a platform property, not a per-application chore:

- **Metrics** — Prometheus with the operator CRDs; every workload is scraped
  through a `ServiceMonitor` or `PodMonitor`, host-level metrics come from a
  node exporter on the hypervisors themselves (on a distinct port from the
  in-cluster DaemonSet), and storage, DNS and virtualization each have an
  exporter.
- **Logs** — Loki, fed by an in-cluster agent for container logs and by a
  host-side agent shipping journald from the hypervisors and guests. Both write
  through the ingress, so there is one authenticated path.
- **Dashboards** — provisioned from ConfigMaps, so a dashboard is a reviewable
  file rather than a thing someone edited in a UI at 2am.
- **Alerts** — rules live beside the stack; a dead-man's-switch alert fires
  continuously and pages if it *stops*, which is the only way to detect that
  alerting itself is down. `task lint:prometheus-config` checks the rules and
  the Alertmanager configuration with `promtool` / `amtool`. It will also run
  `promtool test rules` over `tests/prometheus-rules/*.test.yaml` — the
  generated tree ships no such tests, so that step reports "skipping" until you
  write the first one. Rule *unit tests* are a hook the template provides, not
  coverage it gives you.
- **Runbooks** — every alert carries a `runbook_url` built from
  `cluster_runbook_base_url`, pointing at `docs/RUNBOOKS.md` **in the generated
  repository**, which the template ships. Three headings there are a contract
  with the rules: `#where-to-look-first`, `#certificates`,
  `#backups-and-restore`.

The expectation the template encodes: a new service is not done until it has
logs, metrics, a down-or-stale alert, and a probe if users reach it directly.

---

## Backups

Defence in depth, each layer independently restorable:

```
live datasets ──snapshot──▶ local snapshots        (fast undo, same disks)
      │
      ├──replicate──▶ archive pool                 (survives a pool loss)
      │
      ├──dump──▶ per-application logical backups   (databases: consistent, restorable elsewhere)
      │
      └──restic──▶ offsite object storage          (client-side encrypted, survives the building)
```

Guests are additionally captured by hypervisor-level backups with their own
retention. The offsite copy is encrypted before it leaves the network, so the
provider holds ciphertext and the object-store credentials are scoped so they
cannot delete — deletions happen through a lifecycle policy, not through the key
the backup job holds.

**The bottom two layers of that diagram are opt-in and ship off.** A generated
cluster gives you local snapshots, per-application logical dumps and
hypervisor-level guest backups; the archive replication
(`nas_storage_archive_backup_enabled: false`) and the offsite restic link
(`restic_offsite_enabled: false`, both in `group_vars/nas.yml`) are switches you
turn on once you have somewhere to send them. The template cannot know your
archive pool or your object store, and a backup job pointed at a repository that
does not exist fails nightly.

Enabling offsite means setting `restic_offsite_repo`,
`restic_offsite_cache_dir` and `restic_offsite_sources` in the inventory,
putting `restic_offsite_repo_password` and the rclone remote's credentials in
the vault, and adding the matching `op://` references to the `storage:deploy`
task's `env:` block. Put the cache directory on an encrypted dataset: it holds
the repository tree — file *paths* — in plaintext even though the data blobs are
client-encrypted.

Note the asymmetry that makes this worth doing promptly: the `OffsiteBackup*`
alert rules ship **enabled**, so a fresh cluster alerts on a chain it does not
yet have. Either configure it or gate the alerts off deliberately.

The important discipline is not the chain but the rehearsal: a restore that has
never been performed is a plan, not a backup.

---

## Security posture

1. **Default-deny at the hypervisor firewall.** Admin access comes from the LAN
   admin set and, when enabled, the overlay VPN — nothing else.
2. **Key-only SSH**, no root login, per-host intrusion blocking.
3. **TLS everywhere**, including internal services; certificates are issued over
   DNS-01 so nothing needs to be publicly reachable to be renewed.
4. **Default-deny NetworkPolicy** per namespace, with explicit egress.
5. **Single sign-on** in front of anything with a UI, with the identity provider
   itself managed as code.
6. **No credential in git**, ever — see § Secrets.
7. **At-rest encryption** for datasets holding personal data, and for every
   offsite copy.

---

## Backend seams

The template is opinionated but not welded shut. Each seam below is isolated to
a small number of touchpoints; the "provide" column is the contract a new
implementation has to satisfy.

| Seam | Today | Touchpoints | A new backend must provide |
|---|---|---|---|
| Secrets | 1Password | `op://` refs in Taskfile/CI/group_vars, one `ClusterSecretStore`, one controller | a store the operator supports, a run-time injection mechanism for host tooling, and the two bootstrap credentials |
| DNS | Cloudflare | Terraform zone module, external-dns provider, ACME DNS-01 solver | provider credentials, a Terraform module of the same shape, an operator-supported DNS-01 solver |
| Git / CI | self-hosted GitLab | `.gitlab-ci.yml`, Flux `GitRepository`, in-cluster runners and agent | a pipeline definition, a Flux source the controller supports, a token model for both |
| Overlay VPN | Tailscale (optional) | host role, in-cluster operator, ACL Terraform module, firewall admin set | node enrolment, a way to publish cluster services, policy as code |
| SSO | Authentik | one application namespace, forward-auth middleware, Terraform object inventory | an OIDC/forward-auth provider and a declarative object model |
| GPU | NVIDIA (optional) | VFIO host prep, node driver, device plugin, telemetry | passthrough prep, a device plugin, a scheduling label |
| Virtualization | Proxmox VE | `proxmox_*` roles, guest definitions in the inventory | guest lifecycle, HA, backup, firewall primitives |
| Storage | ZFS + NFS + zvols | storage role, static PVs, encryption units | pools or their equivalent, a PV mechanism, an at-rest encryption story |

The first six are genuine seams: swapping one is a bounded change. The last two
are **assumptions** — Proxmox and ZFS are woven through the inventory model, the
guest lifecycle and the storage layer, and replacing either is a fork, not a
configuration change. If that is a problem, this template is the wrong starting
point, and it is better to know now.

`copier.yml` exposes each seam as a choice with exactly the values that are
implemented; anything else fails during generation rather than producing a
repository that cannot reconcile.

---

## Repository map

```
<cluster>/
├── .copier-answers.yml         what this cluster was generated from
├── ansible/
│   ├── requirements.yml        weisssrv.infra pinned at lib_ref
│   ├── inventories/prod/       hosts.yml + group_vars/ — the site's data
│   └── playbooks/              site, base, dns, storage, k3s, proxmox-*, maintenance/
├── terraform/                  DNS zone, tailnet ACL, SSO objects
├── kubernetes/
│   ├── clusters/<name>/        Flux entrypoint, stage Kustomizations, tenants/
│   ├── components/             reusable kustomize components
│   ├── infrastructure/         sources, crds, controllers, configs, observability
│   └── apps/                   one directory per application
├── scripts/                    verification, generators, the version registry
├── docs/                       RUNBOOKS.md (what every alert links to),
│                               ci-pipeline.md, plus anything you add
├── Taskfile.yml                every operation, grouped by namespace
└── .gitlab-ci.yml              lint, validate, deploy — from the library's templates
```

Notice what is **not** there: `ansible/roles/`. Playbooks address
`weisssrv.infra.<role>` by FQCN and the collection is installed from
`requirements.yml` at `lib_ref`, so the generated tree carries no role source at
all. That is the single biggest reason a generated cluster stays small, and the
reason the variables you set in `group_vars/` are documented somewhere else.

---

## Where the platform is documented

A generated repository documents *one cluster*. Everything it is assembled from
is documented in [weisssrv-lib](https://git.ericsweiss.com/eric/weisssrv-lib),
and both the generated docs and the generated agent skill link there rather than
restating it:

| What | Where in weisssrv-lib |
|---|---|
| Role variables and behaviour | [`ansible_collections/weisssrv/infra/roles/<role>/README.md`](https://git.ericsweiss.com/eric/weisssrv-lib/-/tree/main/ansible_collections/weisssrv/infra/roles) |
| The inventory-wide variables roles alias (the "Use" table) | [collection README](https://git.ericsweiss.com/eric/weisssrv-lib/-/blob/main/ansible_collections/weisssrv/infra/README.md) |
| Role breaking changes across refs | [MIGRATING.md](https://git.ericsweiss.com/eric/weisssrv-lib/-/blob/main/ansible_collections/weisssrv/infra/MIGRATING.md) |
| CI template inputs | [docs/INCLUDE-CONTRACT.md](https://git.ericsweiss.com/eric/weisssrv-lib/-/blob/main/docs/INCLUDE-CONTRACT.md) |
| What a `lib_ref` bump can break | [docs/VERSIONING.md](https://git.ericsweiss.com/eric/weisssrv-lib/-/blob/main/docs/VERSIONING.md) |
| The upstream of the vendored `scripts/` copies | [docs/SCRIPTS.md](https://git.ericsweiss.com/eric/weisssrv-lib/-/blob/main/docs/SCRIPTS.md) |

Filling in the inventory — the step [SETUP.md](SETUP.md) § 2 calls the one part
no template can generate — is done against those role READMEs. Reaching them
from your workstation is a prerequisite, not a convenience: see
[PRE-SETUP.md](PRE-SETUP.md) § 5a.
