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

  terraform/ ───push──▶ things outside both: public DNS, tailnet policy, SSO
                       objects, and the gateway's own networks
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

No Ansible role is vendored into a generated cluster. Playbooks address the
collection's 40 roles as `weisssrv.infra.<role>` by fully-qualified name, and
`ansible/requirements.yml` pins the collection at `lib_ref` — so a platform
upgrade is a one-line, reviewable bump, and a role fix benefits every cluster
generated from the template. Site data is an **input** to those roles rather
than a default: a value with no safe generic default is asserted by name at role
entry, so a missed rename fails the play instead of rendering an empty string.

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
most important rule in the template, and the one it exists to enforce: a cluster
that spells its domains and addresses into manifests cannot be re-addressed or
forked.

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
| Networks | `cluster_lan_cidr`, `cluster_pod_cidr`, `cluster_service_cidr`, `cluster_tailnet_cidr` (Tailscale module only) |
| Addresses | `cluster_api_vip` (and its reserved `cluster_k3s_api_vip` alias), `cluster_apiserver_egress_cidr`, `cluster_metallb_public_vip`, `cluster_metallb_internal_vip` |
| Certificates | `cluster_issuer`, `cluster_acme_email` |
| Secrets | `cluster_secret_store`, `cluster_secrets_vault`, `cluster_secrets_provider_namespace`, `cluster_secrets_provider_deployment` |
| Git | `cluster_git_host`, `cluster_runbook_base_url` |
| Host services | `cluster_nas_host`, `cluster_smtp_host`, `cluster_alert_email` |

Two of these have teeth. `cluster_node_label_domain` must match the prefix the
Ansible k3s layer actually applies to nodes, or every affinity rule silently
matches nothing. `cluster_apiserver_egress_cidr` must be a single CIDR that
covers the **server node addresses**, not the API VIP — kube-proxy rewrites the
destination before NetworkPolicy is evaluated, so allowing the VIP allows
nothing.

The second one ships **deliberately wide and needs narrowing by hand.** The
server addresses live in the inventory, not in a copier answer, so the generator
cannot know them: it writes the whole LAN CIDR and says so in a comment. The key
holds one CIDR, so narrowing all the way to per-server `/32`s means spelling
them in the policies instead — a post-inventory step called out in
[SETUP.md](SETUP.md) § 2. Until then the API-server egress allowance is
LAN-wide. It is the one shipped value in `cluster-config` that is knowingly
looser than the rule above.

A renamed or missing key fails the **reconcile**: kustomize-controller runs
with `StrictPostBuildSubstitutions`, so an unresolved `${placeholder}` takes the
whole stage down rather than applying a blank hostname. The repo-side gate below
exists to catch it before merge, not as the only catch.

`cluster-versions` is generated from `group_vars/all.yml` by
`task flux:sync-versions` and drift-gated by the generated pipeline's
`check-generated-files` job (and by `task lint:repo-sync` locally), which
regenerates it and diffs — so a
version can only change in one place. Without that gate the stale case is
silent: this file is what `flux-lint` substitutes *from*, so it renders and
passes on the old value, while `maintenance:check-versions` reads `all.yml` and
reports the pin as current.

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

In-cluster DNS is k3s's bundled CoreDNS, which k3s owns as an AddOn and resets
on every server restart or upgrade. A replicas patch is therefore a no-op, so
`infrastructure/configs/coredns/` holds the replica count with an HPA whose
`minReplicas` and `maxReplicas` are equal. Its metrics block exists only because
the v2 schema requires one and its threshold never fires; raising `maxReplicas`
turns it into a real autoscaler. Topology spread is soft-only for the same
ownership reason, so the scheduler may still co-locate both replicas and the PDB
cannot prevent it. The durable fix is to disable the bundled AddOn and ship your
own CoreDNS manifest.

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

The `OffsiteBackup*` alert rules ship inert rather than off: their `absent()`
arm is gated on `cluster_offsite_backup_probe_metric`, which ships pointed at a
metric that always exists, and their other arms match no series while the
exporter is absent. Flip that key to
`restic_offsite_last_success_timestamp_seconds` in the same change that enables
the tier — otherwise the chain is unmonitored rather than unalerting.

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
implementation has to satisfy. [README.md](../README.md) § Backends is the same
inventory in operator form, so the two are edited together.

| Seam | Today | Touchpoints | A new backend must provide |
|---|---|---|---|
| Secrets (`secrets_backend`) | 1Password | the macros in `partials/secrets.jinja`, which every reference in the generated Taskfile **and** `.gitlab-ci.yml` is composed from, the library deploy fragment the CI file includes, one `ClusterSecretStore`, one controller | a store the operator supports, a run-time injection mechanism for host tooling, and the two bootstrap credentials |
| DNS (`dns_backend`) | Cloudflare | the `solvers` stanza in `infrastructure/configs/cluster-issuer.yaml`, `infrastructure/controllers/external-dns/release.yaml` (provider, extraArgs, token env), the `dns_backend` block in `infrastructure/configs/kustomization.yaml.jinja` (shared-dns-secrets, cloudflare-ddns), `terraform/cloudflare/`, and `copier.yml`'s validator | provider credentials, a Terraform module of the same shape, an operator-supported DNS-01 solver |
| Git / CI | self-hosted GitLab | `.gitlab-ci.yml`, Flux `GitRepository`, in-cluster runners and agent | a pipeline definition, a Flux source the controller supports, a token model for both |
| Overlay VPN | Tailscale (optional) | host role, in-cluster operator, ACL Terraform module, firewall admin set | node enrolment, a way to publish cluster services, policy as code |
| SSO | Authentik | one application namespace, forward-auth middleware, Terraform object inventory | an OIDC/forward-auth provider and a declarative object model |
| GPU | NVIDIA (optional) | VFIO host prep, node driver, device plugin, telemetry (operator-added) | passthrough prep, a device plugin, a scheduling label |
| Virtualization | Proxmox VE | `proxmox_*` roles, guest definitions in the inventory | guest lifecycle, HA, backup, firewall primitives |
| Storage (`storage_backend`) | ZFS + NFS + zvols | storage role, static PVs, encryption units, the pool scrape | pools or their equivalent, a PV mechanism, an at-rest encryption story |
| Gateway / VLANs (`use_unifi`) | UniFi (optional) | `terraform/unifi`, the `unifi-drift-plan` job, the supervised `terraform:unifi-*` tasks | an API the provider covers, a Terraform module of the same shape, an import path for objects the console already holds |
| Ingress | Traefik + cert-manager | `infrastructure/controllers`, `infrastructure/configs`, the IngressRoute and middleware shapes every app copies | an ingress controller Flux can install, a certificate issuer, a forward-auth mechanism |

All but Virtualization and Storage are genuine seams: swapping one is a bounded
change to a named set of files. Those two are **structural** — Proxmox and ZFS run through the
inventory model, the guest lifecycle and the storage layer, so an alternative is
a second role family and a second storage playbook rather than a different value
in an existing one. `storage_backend` exists to mark where that boundary runs
(and to gate the ZFS-only pool scrape); virtualization has no
equivalent answer because nothing in the generated tree would be left to
select. If either is a problem, this template is the wrong starting point, and
it is better to know now.

`copier.yml` exposes every selectable seam as an enumeration carrying exactly the
values that are implemented; anything else fails during generation rather than
producing a repository that cannot reconcile. They are enumerations rather than
free text on purpose — the *shape* of a second implementation is decided, only
the implementation is missing, so adding one is a new choice value plus the
files behind it.

### What a different backend swaps

The role-side half of every seam is contracted in the library's
[docs/EXTENSIBILITY.md](https://git.ericsweiss.com/eric/weisssrv-lib/-/blob/main/docs/EXTENSIBILITY.md):
which variables exist so an alternative can be selected without a fork, which
roles *are* a backend (and so get a sibling role family instead of a flag), and
the rules a contributed alternative follows — role-prefixed variables, a default
that reproduces today's behaviour byte-for-byte, molecule coverage. Three worked
cases, cluster side:

| Wanted instead | Library side | This template's side |
|---|---|---|
| Ceph rather than ZFS+NFS | omit `weisssrv.infra.nas_storage` and the `zfs_*` roles from the plays and run a `ceph_*` family beside them — the collection is a flat FQCN namespace, so both families can be installed at once | `group_vars/nas.yml` and the storage playbook become that family's; the static `PersistentVolume`s stop being NFS; `vm_additional_disks` (zvol passthrough) has no analogue and its consumers move to PVs |
| GitHub rather than self-hosted GitLab | the `ci/` templates are GitLab YAML and are not included; the scripts they call are forge-neutral except `version-bump-mr.py`, which is GitLab-only (a GitHub client is additive, not a restructure), and `semantic-release.py --platform github` covers the release path | `.gitlab-ci.yml` is replaced by workflows, the Flux source becomes a GitHub `GitRepository`, the in-cluster runner manifests become whatever executes the jobs, and two values stop being GitLab-shaped: `cluster_runbook_base_url` in `sources/cluster-config.yaml.jinja` (the `/-/blob/` path shape is GitLab's, GitHub's is `/blob/`) and `REGISTRY_PROXY_REMOTEURL` in `apps/registry-cache/deployment.yaml` |
| A secrets store that is not 1Password | nothing to change for host-side roles — they take **values**, resolved by the caller before Ansible starts; the one exception is `zfs_encryption`, whose boot-time fetch is behind `zfs_encryption_key_command` | a branch in each of the macros in `partials/secrets.jinja` (below), the library `ci/deploy/deploy-base.yml` include that fetches the deploy SSH key, that provider's `ClusterSecretStore` under `infrastructure/configs`, and whatever replaces the two hand-made bootstrap Secrets |

> **Planned, on the GitHub row:** the reusable Actions cluster pipeline (deploy
> matrix, validation-gate → deploy → verify graph, integration fan-out) is a
> tracked weisssrv-lib addition — see its `docs/EXTENSIBILITY.md` § Forge
> portability, PLANNED entry. It is the library-side half only. `git_backend:
> github` stays validator-blocked on **two** blockers, which is exactly what
> `copier.yml`'s validator says: the generated pipeline *and* the Flux bootstrap
> task are GitLab-shaped. So the template still owes the GitHub bootstrap path
> (`flux bootstrap github` in `taskfiles/flux.yml.jinja`, a GitHub `GitRepository` as
> the Flux source) and the in-cluster runner manifests — the third column of that
> row is the template-side scope.

> **Instance-local on the GitHub row:** a cluster mirrored to GitHub while
> GitLab stays canonical also wants an inert `.github/workflows/` stub (`on: {}`,
> `if: false`) so the mirror cannot run a surprise pipeline. The template does
> not ship one — a mirror is a per-cluster choice, not a seam — so add the file
> by hand in the generated repository and keep it out of `copier update`'s way
> by leaving it unanswered here.

None of these ships today. What each one has is a written boundary — the files
it touches and the contract its replacement satisfies — which is the difference
between a fork and a contribution.

#### The secrets macros

`partials/secrets.jinja` is the one home for the five backend-aware macros, and
every secret reference in a generated cluster is emitted from them:
`secret_ref()` (a reference to one field), `secret_read()` (read one value now),
`secret_runner()` (wrap a command so the values are injected at run time),
`secret_vault()` (the vault name the pipeline's `op_vault` inputs take) and
`secret_gate_var()` (the CI variable every deploy rule gates on). Both consumer
kinds import that partial and use different subsets. `.gitlab-ci.yml.jinja`
takes all five. `Taskfile.yml.jinja` and each `taskfiles/<ns>.yml.jinja` that
emits a reference take `secret_runner()` and wrap `secret_ref()`.
`scripts/bootstrap-proxmox-host.sh.jinja` and the three
`.claude/skills/cluster-development/` pages import it too, for `secret_read()`,
`secret_ref()` and `secret_runner()`, so a new backend has to render for
generated shell and agent prose as well. Each macro's else-branch subscripts an
empty mapping to hand `mandatory` an undefined value, so an unimplemented
backend fails the **render** rather than generating a repository that resolves
nothing.

The Taskfile's wrapper passes the go-task `{{.OP_VAULT}}` variable as the vault
rather than the resolved name, so `task secrets:show`, which reads the Taskfile
tree as text, matches the variable. The pipeline has no such variable and emits the
name.

Four things are backend-specific by nature and deliberately not routed through
the macros: the `op_preconditions` anchor, `op:check` and `secrets:show`, the
`flux:bootstrap-secrets` task, and the library deploy fragment that fetches the
SSH key with `op read` itself.

---

## CI runner capacity

`partials/ci-sizing.jinja` is the one place both runner tiers' sizing constants
live. Each runner's `release.yaml.jinja` and `resourcequota.yaml.jinja` imports
it, so a tier's `concurrent` and the ResourceQuota that must admit it are
computed from one block. The file sits outside `template/`, so only its numbers
reach a cluster; nothing recomputes them after generation, and growing the fleet
means raising them by hand. Jinja's `import` does not export names with a
leading underscore, which is why every name in the partial is unprefixed.

The roster it assumes: `hosts.yml.jinja` gives every k3s agent 4 cores and 8Gi
and puts one agent on each Proxmox host, which
`tests/test_render.py::test_sizing_constants_match_the_rendered_inventory`
holds it to. Job pods are hard-excluded from the NAS agent on the privileged
tier only, so that tier's pool is `compute_node_count` agents and the shared
tier's is `compute_node_count + 1`.

On the privileged tier CPU is the governor. A job costs about 1.5 cpu (build
900m, DinD 500m, helper 100m), 1Gi of memory requests and 3.25Gi of declared
memory limits, and two cores stay with the platform pods co-tenanting those
agents. The quota dimensions are the per-job cost doubled, because a Terminating
pod still counts and a thin buffer 403s under churn, which GitLab reports as
`runner_system_failure`, plus slack for the manager pod. `requests.*` are the
scheduler's reservation; `limits.memory` is an admission ceiling deliberately
above what the pool can resident-hold, which is why job pods carry the
`ci-jobs` PriorityClass.

On the shared tier memory is the governor. A job is cheap on CPU (build 500m,
helper 100m) and expensive on memory: 1.25Gi of requests (1Gi plus the
LimitRange's 256Mi helper default) and 5Gi of declared limits (4Gi plus the
LimitRange's 1Gi default). `concurrent` comes off the memory pool, capped so a
full burst of declared limits stays at three quarters of it. `requests.*` carry
the doubled per-job cost plus manager slack; `limits.memory` and `pods` do not
double, because at 5Gi a job that would put the ceiling above the whole pool, so
churn gets two extra slots instead.

Both quotas deliberately omit a `cpu` / `limits.cpu` dimension. It would force
every pod in the namespace to declare a CPU limit, which rejects the runner
manager — it runs without one, to avoid CFS throttling. CPU is bounded through
`requests.cpu`, memory through both.

---

## Parity with the reference cluster

`weisssrv` is the cluster this template was extracted from, and nothing pins the
two together. [AGENTS.md](../AGENTS.md) § Parity with the reference cluster
states which direction each area flows and is the rule reviewers apply; the
static Flux `HelmRepository` sources under
`template/kubernetes/infrastructure/sources/` are the clearest case — they are
maintained in step with the reference cluster by hand, with no gate between
them.

---

## Repository map

```
<cluster>/
├── .copier-answers.yml         what this cluster was generated from
├── ansible/
│   ├── requirements.yml        weisssrv.infra pinned at lib_ref
│   ├── inventories/prod/       hosts.yml + group_vars/ — the site's data
│   └── playbooks/              site, base, dns, storage, k3s, proxmox-*, maintenance/
├── terraform/                  DNS zone, tailnet ACL, SSO objects, gateway networks
├── kubernetes/
│   ├── clusters/<name>/        Flux entrypoint, stage Kustomizations, tenants/
│   ├── components/             reusable kustomize components
│   ├── infrastructure/         sources, crds, controllers, configs, observability
│   └── apps/                   one directory per application
├── scripts/                    verification, generators, the version registry
├── docs/                       RUNBOOKS.md (what every alert links to),
│                               ci-pipeline.md, plus anything you add
├── Taskfile.yml                every operation, grouped by namespace
├── taskfiles/                  one file per namespace, included by Taskfile.yml
└── .gitlab-ci.yml              lint, validate, deploy — from the library's templates
```

Notice what is **not** there: `ansible/roles/`. Playbooks address
`weisssrv.infra.<role>` by FQCN and the collection is installed from
`requirements.yml` at `lib_ref`, so the generated tree carries no role source at
all. That is the single biggest reason a generated cluster stays small, and the
reason the variables you set in `group_vars/` are documented somewhere else.

---

## OIDC issuer host

Every OIDC consumer in a generated cluster uses the **external** issuer host,
`auth.<external_domain>`, set by `AUTHENTIK_WEB__BASE_URL` in authentik's
server.env and worker.env — including apps that are only reachable on the LAN.
`auth.<internal_domain>` is the LAN and tailnet route and the Terraform admin-API
URL, never an issuer. Mixing the two inside one cluster is not workable: a token
issued under one host is rejected under the other.

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
| CI template inputs | [docs/INCLUDE-CONTRACT.md](https://git.ericsweiss.com/eric/weisssrv-lib/-/blob/main/docs/INCLUDE-CONTRACT.md) |
| What a `lib_ref` bump can break | [docs/VERSIONING.md](https://git.ericsweiss.com/eric/weisssrv-lib/-/blob/main/docs/VERSIONING.md) |
| The upstream of the vendored `scripts/` copies | [docs/SCRIPTS.md](https://git.ericsweiss.com/eric/weisssrv-lib/-/blob/main/docs/SCRIPTS.md) |
| The seam contract behind § Backend seams | [docs/EXTENSIBILITY.md](https://git.ericsweiss.com/eric/weisssrv-lib/-/blob/main/docs/EXTENSIBILITY.md) |

Filling in the inventory — the step [SETUP.md](SETUP.md) § 2 calls the one part
no template can generate — is done against those role READMEs. Reaching them
from your workstation is a prerequisite, not a convenience: see
[PRE-SETUP.md](PRE-SETUP.md) § 6.
