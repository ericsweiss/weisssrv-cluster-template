# Before you run copier

Everything on this page has to exist, or at least be decided, before generating
a cluster repository. Copier asks for values it cannot discover — domains you
own, addresses you have reserved, accounts you control — and a wrong answer is
cheap to fix now and expensive to fix after the first deploy.

Work top to bottom. The last section is a checklist you can tick off.

---

## 1. Hardware baseline

The template assumes a small Proxmox VE cluster with one storage-heavy node and
two or more compute nodes. Fewer nodes work, with caveats noted below.

### Per-node minimum

| Role | CPU | RAM | Disks | Network |
|---|---|---|---|---|
| Any Proxmox host | x86-64 with VT-x/AMD-V | 8 GB | 32 GB+ for the OS | 1 GbE |
| Storage node | 4+ modern cores | 32 GB (64 GB comfortable — ZFS ARC) | OS disk, plus a bulk pool, an SSD pool for app data, optionally an NVMe scratch device | 1 GbE, 10 GbE if you can |
| Compute node | 4+ cores | 16 GB (32 GB once k3s workloads land) | OS disk plus one SSD for guest storage | 1 GbE |

### Cluster shape

- **Three hosts minimum** if you want the k3s control plane to tolerate a node
  failure: the template lays out three server VMs holding an etcd quorum, and
  putting two of them on the same host defeats it. The shipped default is
  `compute_node_count: 2`, which reaches three hosts by placing **one etcd
  member on the storage node** — so budget that node for a 2 vCPU / 6 GB server
  VM plus a 4 vCPU / 8 GB agent on top of its ZFS ARC.

  The storage node carries `k3s-srv-01` at **every** `compute_node_count`: the
  generator walks the host list starting at the storage node, so raising the
  answer adds compute hosts and agents rather than moving server 01 off the NAS.
  If you want it elsewhere, re-point that host's `proxmox_host` in the generated
  `hosts.yml` — a one-line edit, and the roster is yours to edit anyway.
- **One storage node.** It owns the ZFS pools, the NFS exports every stateful
  workload mounts, and the zvols passed through to application VMs. It is the
  one node whose loss stops the cluster.
- Proxmox VE **9.0 or newer**, clustered (`pvecm create` / `pvecm add`) so HA and
  replication are available. A single-node install works for evaluation; HA
  rules and storage replication are then inert.
- Virtualization extensions and, if you plan to pass a GPU through, **IOMMU**
  enabled in firmware.
- A UPS is not required by the template and is strongly recommended by physics.

### Storage layout

`storage_backend: zfs` is the implemented choice, and it is structural rather
than a flag: the storage playbook, the guest disks and the pool scrape are all
ZFS. See [ARCHITECTURE.md](ARCHITECTURE.md) § Backend seams for what a
different one would have to bring.

Create the ZFS pools **by hand, before Ansible runs**. Nothing in the generated
repository creates or destroys a pool — that is deliberate; Ansible only sets
properties, creates datasets and zvols, and mounts things.

The template expects up to four pool roles on the storage node. Pool *names* are
inventory values you choose; the roles are:

| Role | Typical topology | Holds |
|---|---|---|
| bulk | raidz2 over large HDDs | media, shares, backup staging |
| app data | raidz1 or mirror over SSDs | databases, per-app zvols, cluster PV data |
| scratch | single NVMe | downloads, hot staging |
| archive | raidz1 over HDDs | cold copies, replication target |

Compute nodes want one local SSD pool for guest disks. Always create pools with
`ashift=12` and reference disks by `/dev/disk/by-id/` — neither is changeable
later.

**A zpool is not yet a Proxmox storage.** Every guest the template creates lands
on a Proxmox *storage ID*, chosen from `proxmox_storage_defaults` in
`ansible/inventories/prod/group_vars/all.yml`, and the generated file ships two:
`ssd` on the storage node (`proxmox_role: nas`) and `local-ssd` on the compute
nodes. A stock Proxmox VE install registers only `local` and `local-zfs`, and
nothing in the generated repository runs `pvesm` — pool *registration*, like
pool *creation*, is left to you. Register them after creating the pools:

```bash
# on the storage node, for the app-data pool
pvesm add zfspool ssd       -pool <app-data-pool> -content images,rootdir
# on every compute node, for its local guest pool
pvesm add zfspool local-ssd -pool <local-pool>    -content images,rootdir
```

Either use those two IDs, or re-point `proxmox_storage_defaults` at the IDs you
already have. Getting it wrong is loud rather than subtle — `pct create` / `qm
create` names the storage it could not find and lists what exists — but it lands
on every guest of the first deploy.

One pool name is *not* free: the shipped `vm_additional_disks` entries in
`hosts.yml` name zvol paths under `ssd/appdata/...`, so the app-data **zpool**
is assumed to be called `ssd` as well. Rename it there if yours is not.

If you plan to use at-rest encryption, decide now: the template's encryption
model makes each dataset its own encryption root and loads keys at boot from the
secrets backend. Retrofitting encryption means recreating datasets.

---

## 2. Network plan

One flat LAN. Everything — hosts, guests, VIPs — lives on it. Fill this in
before you start; the answers marked → become copier answers.

| Item | → answer | Notes |
|---|---|---|
| LAN network in CIDR | `lan_cidr` | e.g. `192.168.1.0/24` |
| First three octets | `lan_prefix` | derived from the above for a /24 |
| Router / gateway address | `lan_gateway` | usually `.1`; **required** — both guest-provisioning roles assert it before creating anything |
| DHCP pool range | — | must **not** overlap anything below |
| Proxmox host addresses | — | one per node, static, contiguous block |
| Resolver addresses | `upstream_dns_servers` | space-separated, preference order; the DNS containers are **created** at these, not forwarded to |
| Infrastructure guest addresses | — | DNS, mail relay, application VMs |
| k3s server VM addresses | — | three, contiguous |
| k3s agent VM addresses | — | one per compute node, contiguous |
| Kubernetes API VIP | `k3s_api_vip` | floating, assigned by kube-vip |
| Public ingress VIP | `metallb_public_vip` | the address you port-forward to |
| Internal ingress VIP | `metallb_internal_vip` | LAN-only services |

This is the plan the **starter inventory actually ships with**, so copying it
means the generated `hosts.yml` needs re-addressing only if you disagree with
it. The blocks are contiguous and, deliberately, all sit below the VIPs:

```
x.x.x.1          router
x.x.x.11         storage (NAS) host
x.x.x.12-.19     compute hosts
x.x.x.21/.22     DNS resolvers (LXC)
x.x.x.23         SMTP relay (LXC)
x.x.x.31-.39     k3s server VMs
x.x.x.41-.49     k3s agent VMs
x.x.x.50-.99     application guests you add later
x.x.x.100        public ingress VIP        ← metallb_public_vip
x.x.x.101        internal ingress VIP      ← metallb_internal_vip
x.x.x.161        Kubernetes API VIP        ← k3s_api_vip
x.x.x.170-.254   DHCP pool and workstations
```

Two of those blocks are composed from `compute_node_count`: the compute hosts
from `.12` up, and one k3s agent per Proxmox host from `.41` up. Their bands are
what caps the answer at **8 compute hosts** — copier rejects a count that would
reach a resolver, the relay, the server band or past `.49`. For a bigger
cluster, generate at 8 or fewer and add the rest to `hosts.yml` on a scheme of
your own; nothing outside that file derives an address from the answer.

The DHCP pool goes at the **top** here for one reason: most consumer routers
default to handing out `.100`–`.199` or the whole `.2`–`.254` range, and every
address above is static. Shrink the pool to a block that contains none of them
before you generate, or you will spend the first deploy chasing address
conflicts. If you would rather keep your existing pool where it is, re-address
`hosts.yml` after generation — every hostname and address in it is a placeholder
and the file says so.

Also decide and note:

- **Port forwards** on the router: `443/tcp` (and `80/tcp` if you want HTTP-01
  fallback or redirects) to the public ingress VIP. Nothing else has to be
  exposed; the overlay VPN covers remote administration.
- **DHCP reservations** are not used for anything the template manages — every
  managed address is static.
- Two of the VIPs answer ARP from whichever node currently holds them. If your
  switch does port security or your network does IP-MAC binding, allow that.

The Kubernetes pod and service networks (`k3s_pod_cidr`, `k3s_service_cidr`)
default to k3s' own `10.42.0.0/16` and `10.43.0.0/16`. Change them only if they
collide with your LAN, and if you do, change them before the first `k3s`
deploy — they are not re-negotiable later.

### Two names that must move together

`nas_host` and `smtp_host` are FQDNs, but they are also **inventory hostnames**:
their short names are what the storage host and the relay container are called
in `ansible/inventories/prod/hosts.yml`. SETUP § 2 has you rename the placeholder
roster hosts to your own; if you rename either of those two there without having
answered the matching FQDN here, the two halves stop agreeing — NFS PVs mount a
name nothing resolves (they mount **by name**, because the wildcard certificate
has no IP SAN) and every host's mail null-client submits to a relay that is not
there. Decide both names now and use them in both places.

Both must sit **under `internal_domain`**, and copier rejects them if they do
not. The internal wildcard covers `*.<internal_domain>` and nothing else, and
the NFS PVs mount with `xprtsec=tls`, which verifies that certificate's SAN — so
a storage box that already answers to a name in another zone fails the handshake
exactly as an IP mount does, and the internal resolver does not answer for it
either. Give it a second name in the internal zone rather than answering its
existing one.

---

## 3. Domains and DNS

The design is split-horizon and needs **two zones**:

- `internal_domain` — resolved on the LAN to internal addresses by the cluster's
  own DNS servers. It is also used as the namespace for Kubernetes node labels,
  so it must be a domain you control, not `.local`.
- `external_domain` — the public zone, served by the DNS backend, for services
  you actually publish.

If you only own one domain, use a subdomain for the internal zone
(`lan.example.com` alongside `example.com`). They must differ; copier rejects
identical values because the per-zone certificate and ingress pairs would
collide.

Two addresses also belong here rather than to any zone: `admin_email` is where
system mail (cron, SMART, ZFS events) lands and the account address ACME
registers with, and `alert_email` is where critical Alertmanager notifications
go. They default to the same inbox; separate them if pages should not land in
the same place as nightly SMART reports.

### Cloudflare (`dns_backend: cloudflare`, the implemented DNS backend)

1. Add `external_domain` to a Cloudflare account and move its nameservers.
   Adding `internal_domain` too is optional — it is only needed if you want
   DNS-01 certificates for internal names, which the template does want, so in
   practice add both.
2. Create **two API tokens**, because they have different blast radius:

   | Token | Scopes | Used by |
   |---|---|---|
   | DNS token | `Zone:Read`, `DNS:Edit` on the two zones | cert-manager, external-dns, the dynamic-DNS job, the host-side ACME client |
   | Terraform token | `Zone:Read`, `DNS:Edit`, **`Zone Settings:Edit`** | Terraform only — it manages zone-level settings |

   Keeping them separate means the credential sitting in the cluster cannot
   change your zone's TLS posture.
3. Note your **account ID** (Cloudflare dashboard, right-hand column). Both
   token records carry it.

### Choosing what resolves where

Internal names resolve through the DNS servers the template deploys (a filtering
resolver in front of a validating recursive resolver). Your router should hand
those addresses out via DHCP once they exist — that is a post-deploy step, not a
prerequisite, but plan for it: until then, only machines pointed at them see
internal names.

---

## 4. Secrets backend

`secrets_backend: onepassword` is the implemented choice. Nothing else in the
generated repository ever holds a plaintext credential.

There are **three** distinct consumers, and each needs its own setup:

1. **Your workstation and Ansible** — the 1Password CLI (`op`), signed in, with
   `op run --` injecting `op://<vault>/<item>/<field>` references at run time.
2. **The cluster** — External Secrets Operator talking to a **1Password Connect**
   server running *inside* the cluster. Connect holds a copy of the vault and
   never calls out to 1Password's cloud during reconciliation.
3. **CI** — a **1Password service account** token, stored as a masked CI
   variable, used by pipeline jobs through `op run` / `op read`.

### Setup

```bash
# CLI
op account add --address my.1password.com --email you@example.com
eval "$(op signin)"

# Create the vault that will hold this cluster's items → onepassword_vault
op vault create Homelab

# Connect server credentials. The server NAME IS LOAD-BEARING: the generated
# `task flux:bootstrap-secrets` looks for `<cluster_name>-connect` and mints its
# own token against it. Use exactly this form.
op connect server create <cluster_name>-connect --vaults Homelab

# CI service account, scoped read-only to the same vault
op service-account create ci --vault Homelab:read_items
```

`op connect server create` writes **`1password-credentials.json`** into the
current directory. Do not mint a token by hand — `task flux:bootstrap-secrets`
creates `<cluster_name>-eso` itself during bring-up, which is also how you
rotate it later. Move the JSON file into the generated repository's root before
running that task; the task's precondition looks for
`./1password-credentials.json` and nowhere else. It is gitignored there — that
file is a read credential for the whole vault, and it is the one artifact that
cannot be re-derived from anything else.

The credentials file and the minted token become the only two Kubernetes Secrets
ever created by hand; everything else in the cluster is produced from them.
Losing them is recoverable (regenerate the server and re-run the task); losing
the vault is not.

### Items to create

**Item titles and field names are load-bearing.** A title is a path segment in
an `op://<vault>/<Item Title>/<field>` reference, and it is also the
`remoteRef.key` an `ExternalSecret` sends to 1Password Connect; a field name is
the last path segment / `remoteRef.property`. A mismatch is not a warning: `op
run` hard-fails the whole task, and an `ExternalSecret` sits in
`SecretSyncError` forever.

The names below are exactly what the generated repository asks for. Create the
items with these titles, or rename both sides — the references live in
`ansible/inventories/prod/group_vars/all.yml`, `Taskfile.yml` and the
`ExternalSecret` manifests. `task secrets:show` in the generated repository
prints the live list (references only, never values) if you ever need to
re-derive it.

Note the two 1Password quirks the field names reflect: a Login item's fields are
`username` and `password`, and an API Credential item's secret field is
`credential`. That is why an account ID lives in `username` on the Cloudflare
items.

#### Host-side — resolved by `op run` (Ansible, Terraform, Task, CI)

| Item title | Fields | Needed by |
|---|---|---|
| `SSH Key` | `public key`, `private key` | base host config, guest cloud-init; CI reads the private half for deploy jobs |
| `Email Config` | `root_alias` | destination for cron, fail2ban and SMART mail |
| `AdGuard Home` | `password` | the filtering resolver's admin UI |
| `Cert Distribution Key` | `private key`, `public key` | pushing renewed certificates to non-cluster hosts |
| `Cloudflare DNS Token` | `credential`, `username` (account ID) | ACME DNS-01 on the hosts; also read by the cluster (below) |
| `Cloudflare Terraform Token` | `credential`, `username` (account ID) | `task terraform:*` — the zone module only |
| `SMTP Smarthost` | `username`, `password` | the relay authenticating upstream |
| `SMTP Relay Auth` | `username`, `password` | hosts authenticating to the relay; also read by the cluster |
| `Samba NAS User` | `password` | SMB access to the NAS |
| `K3s Cluster Token` | `credential` | server node join |
| `K3s Agent Token` | `credential` | agent join — worker-only, so a compromised agent cannot register a server |
| `Loki Push Auth` | `username`, `password` | host-side Alloy pushing journald through the ingress |
| `1Password Connect` | `token` | the boot-time ZFS key fetch (`task zfs:encrypt`) |
| `Git Access Token` | `credential` | `task flux:bootstrap` — needs `api`, `read_repository`, `write_repository` |
| `Git Terraform State Token` | `credential` | the GitLab-managed Terraform HTTP state backend |
| `GitHub Token` | `credential` | `task maintenance:check-versions` (upstream release lookups; a bare read-only PAT) |
| `Authentik Terraform Token` | `credential` | the `terraform:authentik-init` / `-plan` / `-apply` tasks — created after Authentik is up |
| `Tailscale Auth Key` | `credential` | enrolling hosts, only with `vpn_tailscale` |
| `Tailscale OAuth` | `client id`, `credential` | the `terraform:tailscale-init` / `-plan` / `-apply` tasks, only with `vpn_tailscale` |

`Loki Push Auth` deserves a call-out: it is in the `env:` block of
`infra:deploy`, `dns:deploy` and `storage:deploy`, so a missing item fails all
three at the very start of bring-up, long before anything logs anything.

#### In-cluster — fetched by External Secrets from Connect

These never appear in an `op://` reference; they are `remoteRef.key` /
`remoteRef.property` pairs in `ExternalSecret` manifests.

| Item title | Fields | Consumed by |
|---|---|---|
| `Cloudflare DNS Token` | `credential` | cert-manager, external-dns, the DDNS job (same item as above) |
| `SMTP Relay Auth` | `username`, `password` | Alertmanager and Authentik outbound mail (same item as above) |
| `Loki Push Auth` | `htpasswd` | the Loki ingress basic-auth middleware — an **htpasswd line**, not the plain password |
| `Alertmanager Webhook` | `url` | the chat receiver |
| `Healthchecks Watchdog` | `ping url` | the dead-man's-switch heartbeat (note the **space** in the field name) |
| `Grafana SSO` | `oidc-client-id`, `oidc-client-secret`, `admin-password` | Grafana's OIDC login, plus the break-glass built-in admin (login form and basic auth are both off, so it authenticates nothing until you turn one back on) |
| `Authentik Secrets` | `secret-key`, `postgresql-password`, `postgresql-admin-password` | the identity provider and its database |
| `GitLab Runner` | `runner-token` | the shared in-cluster runner |
| `GitLab Runner Privileged` | `runner-token` | the privileged runner (image builds) |
| `Registry Cache Upstream` | `username`, `password` | the pull-through registry cache's upstream credentials |
| `Tailscale Operator OAuth` | `client-id`, `client-secret` | the Kubernetes operator, only with `vpn_tailscale` |

Two of these block the **first** reconcile rather than degrading gracefully:
without `Healthchecks Watchdog` the `alertmanager-config` ExternalSecret never
syncs, so Alertmanager gets no `configSecret` at all; without `Grafana SSO`
Grafana sits in `CreateContainerConfigError`. Create both before phase 6 even if
you have no heartbeat service and no identity provider yet.

**A placeholder is enough for most of it, and wrong for two fields.** On
`Healthchecks Watchdog` → `ping url`, and on `Grafana SSO` →
`oidc-client-id`, a placeholder is genuinely temporary: the value is read on
every reconcile, so replacing it later takes effect. The other two are not:

- `Grafana SSO` → **`admin-password`** must be a real random value *from the
  start*. Grafana creates its built-in admin only while its user table is empty,
  so this password is applied exactly once, at Grafana's first start against an
  empty database — and that database is on a persistent volume, so there is no
  second first start. Editing the vault field afterwards changes nothing; the
  fix is `grafana cli admin reset-admin-password` (SETUP § 7 step 4). This is the
  one credential that works when the identity provider does not, so leaving it
  at a placeholder puts a Grafana Admin behind a guessable password on the
  internal ingress, reachable exactly during the incident nobody is watching.
- `Grafana SSO` → **`oidc-client-secret`** must be a real random value before
  `task terraform:authentik-apply`. Terraform is authoritative in that
  direction: it *sets* Authentik's client secret **from** this field, so a
  placeholder here becomes the live OIDC client secret rather than being
  replaced by one Authentik generated.

Generate both with `openssl rand -base64 32` when you create the item.

Four items cannot exist until the thing that issues them exists, so create them
as the bring-up reaches them: `GitLab Runner` and `GitLab Runner Privileged`
(registration tokens, from the GitLab project), `Authentik Terraform Token`
(minted in Authentik after it is running), and `Registry Cache Upstream` (your
registry account). Until then, those workloads stay in `SecretSyncError`, which
is loud and harmless.

Generate every random value with something like `openssl rand -base64 32` and
paste it in; do not invent memorable ones. Encrypted-pool passphrases are not in
this list: the boot-time key load reaches Connect with the `1Password Connect`
token above, and each pool's passphrase item is named by your own
`zfs_encryption` inventory settings.

> One value deserves special care: if you enable offsite backups, the backup
> repository password can never be rotated and its loss makes every offsite copy
> undecryptable. Keep a copy **outside** the vault.

---

## 5. Git host

`git_backend: gitlab_selfhosted` is the implemented choice. GitHub is a declared
seam; copier will refuse it until it exists. What such a consumer would have to
build — the library ships GitLab CI YAML, forge-neutral scripts and a
`--platform github` release path — is mapped in the library's
[docs/EXTENSIBILITY.md](https://git.ericsweiss.com/eric/weisssrv-lib/-/blob/main/docs/EXTENSIBILITY.md)
§ Forge portability.

You need, before generating:

- A reachable GitLab instance (`git_host`) with a group or user namespace
  (`git_namespace`) you can create projects in. Self-hosting it *on* the cluster
  you are about to build is a chicken-and-egg problem: bootstrap against a
  hosted instance or a temporary one and migrate later.
- An **empty project** named after `cluster_name` in that namespace. Do not
  initialize it with a README — the first push comes from the generated tree.
- A **personal access token** for Flux with `api`, `read_repository`,
  `write_repository`. Flux commits its own controller manifests to the
  repository during bootstrap, so read-only is not enough.
- A plan for **runners**. CI jobs are ordinary GitLab jobs; the generated
  pipeline expects at least one runner that can reach the cluster. The template
  ships in-cluster runner manifests, which means the first pipeline runs cannot
  happen until the cluster exists — expect to bring the platform up manually
  first and let CI take over afterwards. Two answers come out of that plan:
  - `ci_runner_tag` — the tag **every** generated job carries. It must match a
    tag on a runner registered to the project, or the pipeline queues forever
    rather than failing.
  - `ci_cpu_selector` — a `label=value` node selector pinning the secret-detection
    job to a modern CPU (gitleaks SIGILLs without SSE4.2). It has to satisfy the
    runner's `node_selector_overwrite_allowed` regex **and** name a label that
    is actually on a node; a selector nothing carries leaves the job Pending
    forever. You apply it via the agents' `k3s_labels` in `hosts.yml`.
  - `enable_semantic_release` — off by default; turn it on only if you want the
    generated pipeline to carry a release stage. A cluster repo is normally
    released by hand.

---

## 5a. Library access

The generated repository is not self-contained: it consumes **weisssrv-lib** in
four places, and three of them run from *your workstation*, not from CI. Decide
this before you generate, because all three are copier answers.

| Where | What it fetches | How |
|---|---|---|
| `ansible/requirements.yml` | the `weisssrv.infra` collection | `git+<lib_url>` over HTTPS, at `lib_ref` |
| `terraform/*/main.tf` | the zone / ACL / SSO modules | `git::<lib_url>` module sources, at `lib_ref` |
| `task lib:sync` | the source of the vendored `scripts/` copies | `git clone <lib_url>` |
| `.gitlab-ci.yml` | the lint / validate / test / security jobs | `include: project: <lib_project>` — resolved **on the GitLab instance the pipeline runs on** |

Three answers control it:

- **`lib_url`** — the clone URL your workstation uses. If you cannot reach
  `git.ericsweiss.com`, fork or mirror the library somewhere you can and point
  this at your copy. Nothing else changes.
- **`lib_ref`** — the tag everything is pinned to. It must exist on whatever
  `lib_url` points at; a ref that does not exist fails at
  `task ansible:install-collections`, at `terraform init`, and in CI, each with a
  different error message.
- **`lib_project`** — the GitLab *project path* for `include: project:`. This
  one is instance-local: `include: project:` cannot cross instances. If your
  cluster does not live on the instance hosting the library, mirror the library
  onto your instance and set `lib_project` to its path there, or vendor the CI
  templates.

Confirm access before generating:

```bash
git ls-remote <lib_url> 'refs/tags/<lib_ref>*'   # must print a SHA
```

Role variables, the CI templates' inputs and what a `lib_ref` bump can break are
documented in the library, not here. The generated tree carries **no**
`ansible/roles/` at all — every play addresses `weisssrv.infra.<role>`, so the
inventory you write in SETUP § 2 is written against these:

| What | Where in weisssrv-lib |
|---|---|
| Per-role variables and behaviour | [role READMEs](https://git.ericsweiss.com/eric/weisssrv-lib/-/tree/main/ansible_collections/weisssrv/infra/roles) |
| The inventory-wide variables every role aliases | [collection README](https://git.ericsweiss.com/eric/weisssrv-lib/-/blob/main/ansible_collections/weisssrv/infra/README.md) |
| Variable renames and newly asserted inputs across refs | [MIGRATING.md](https://git.ericsweiss.com/eric/weisssrv-lib/-/blob/main/ansible_collections/weisssrv/infra/MIGRATING.md) |
| CI template inputs | [docs/INCLUDE-CONTRACT.md](https://git.ericsweiss.com/eric/weisssrv-lib/-/blob/main/docs/INCLUDE-CONTRACT.md) |
| What a `lib_ref` bump can break | [docs/VERSIONING.md](https://git.ericsweiss.com/eric/weisssrv-lib/-/blob/main/docs/VERSIONING.md) |
| Which roles are a backend rather than a seam | [docs/EXTENSIBILITY.md](https://git.ericsweiss.com/eric/weisssrv-lib/-/blob/main/docs/EXTENSIBILITY.md) |

---

## 6. Host access

Ansible does not bootstrap itself. On **every Proxmox node**, before the first
playbook:

- The `admin_user` account exists, is in the sudo group, and has
  `NOPASSWD: ALL` in `/etc/sudoers.d/<user>`.
- Your SSH public key is in that user's `~/.ssh/authorized_keys` (mode 600, `.ssh`
  mode 700).
- `python3` is installed — Ansible needs an interpreter on the target.
- The node has a static address, a working default route, and can resolve public
  names.
- Package repositories are configured and the system is up to date on the latest
  kernel.

Generate a key pair if you do not have one:

```bash
ssh-keygen -t ed25519 -C "<admin_user>@workstation"
```

Store both halves in the secrets backend as the SSH key item. The public half is
what the base role distributes; the private half is what disaster recovery needs.

Guests (LXC containers and VMs) do **not** need this treatment — the template
provisions them with cloud-init and the key from the vault.

---

## 7. Optional: overlay VPN (`vpn_tailscale`)

Enable it if you want remote administration without exposing SSH or the Proxmox
UI. It requires, before generation:

- A Tailscale account and tailnet.
- Your tailnet's **MagicDNS suffix** (`<tailnet>.ts.net`, on the admin console's
  DNS page) → `tailnet_dns_suffix`. Fetch it *before* generating: the question
  has no default and rejects the `CHANGEME.ts.net` placeholder, because a
  cluster generated with it has a tailnet resolver handing out CNAMEs into a
  domain that does not exist — remote access to internal names silently broken,
  with nothing failing to tell you. If you do not have the suffix to hand,
  answer `vpn_tailscale` as false and re-run `copier update` later.
- A reusable **auth key** for enrolling hosts.
- An **OAuth client** with `acl` and `dns` write scope, if you want the tailnet
  policy and split-DNS managed as code by Terraform.
- A second **OAuth client** tagged for the Kubernetes operator, with write scope
  on devices, auth keys and services, if you want cluster services published to
  the tailnet.

With it disabled, remote access is your own problem and the firewall rules that
would have trusted the tailnet simply are not generated.

---

## 8. Optional: GPU (`gpu: nvidia`)

Requires:

- An NVIDIA card in **one** compute node, and that node's IOMMU groups clean
  enough to pass the card through (check `dmesg | grep -i iommu` and the group
  membership of the card's PCI address).
- IOMMU enabled in firmware, and the host **not** using the card for display.
- Awareness that the first apply is disruptive: the host binds the card to VFIO
  and reboots.

---

## 8a. Answers to accept as they come

Not everything copier asks is a decision. `node_exporter_job_regex` looks like
free text, but its two members name things the template *ships*: `node-exporter`
is the kube-prometheus chart's own DaemonSet job and `node-exporter-host` is the
static scrape of the Proxmox hosts and VMs. The node and storage alert rules
scope themselves to that alternation, so dropping either name leaves those rules
matching zero series — and a rule that matches nothing never fires, so nothing
ever tells you the alerts went quiet. Copier now rejects an answer that omits
either; press enter unless you have added a third exporter source of your own,
in which case append `|your-job` rather than replacing what is there.

The same goes for `k3s_pod_cidr` and `k3s_service_cidr` (§ 2): the k3s defaults
are right unless they collide with your LAN.

---

## 9. Workstation tooling

`task lint` in the generated repository is the authoritative completeness check:
every gate names the binary it is missing in its precondition message. The list
below is what it takes to get a clean run.

```bash
# macOS
brew install copier go-task/tap/go-task ansible jq yq
brew install hashicorp/tap/terraform
brew install --cask 1password-cli
brew install kubernetes-cli fluxcd/tap/flux
# the gates task lint runs that the list above does not cover:
brew install kustomize kubeconform gettext shellcheck helm
pip install ansible-lint yamllint pyyaml ruff

# Debian/Ubuntu: pipx install copier; apt install kubectl-equivalents, shellcheck,
#   gettext-base; pip install ansible-lint yamllint pyyaml ruff; the rest from each
#   project's instructions
```

`gettext` is for `envsubst`, which `flux:lint` uses to expand `${cluster_...}`
placeholders before schema validation; `kustomize` and `kubeconform` are the
other two halves of that gate. `helm` is the fourth: `flux:lint` renders the
value-heavy HelmReleases with `helm template` to validate them against each
chart's schema, so it also wants network access the first time (`helm repo
add`/`update`). `ansible-lint` and `yamllint` are the lint stage's
first two steps, and `ruff` is `lint:ruff` — the same check the CI python-lint
job runs over `scripts/` and `tests/`.

Not required for `task lint`, but required by the gate you run after touching
alert rules (`task lint:prometheus-config`): **`promtool`** and **`amtool`**,
which ship in the Prometheus and Alertmanager release tarballs.

`ansible-galaxy` comes with Ansible; the generated repository wraps it as
`task ansible:install-collections`, which is the first command to run after
generating (SETUP § 3).

Versions the generated repository expects: Task 3.x, Ansible core 2.18+ (the
collection's declared floor), Terraform 1.x within the range each generated
`versions.tf` declares, `op` 2.x, Python 3.11+, copier 9+.

---

## 10. Pre-flight checklist

Hardware and OS

- [ ] Proxmox VE 9+ installed on every node, clustered
- [ ] Static addresses, gateway, and outbound DNS working on every node
- [ ] Repositories configured, systems updated, rebooted
- [ ] ZFS pools created on the storage node, `zpool status` clean
- [ ] Compute-node local pools created
- [ ] Those pools registered as Proxmox storages (`pvesm add zfspool ssd …` on
      the storage node, `local-ssd` on each compute node), or
      `proxmox_storage_defaults` planned to point at the IDs you already have
- [ ] Time synchronized across nodes

Access

- [ ] `admin_user` exists on every node with passwordless sudo
- [ ] SSH key deployed; `ssh <admin_user>@<node>` works without a password
- [ ] `python3` present on every node

Network

- [ ] Address plan written down, DHCP pool does not overlap it
- [ ] Three VIPs reserved and unused
- [ ] Router port-forward planned to the public ingress VIP

Names and accounts

- [ ] Two domains decided, both in the DNS provider account
- [ ] Both API tokens created with the scopes above; account ID noted
- [ ] Vault created; CLI signed in
- [ ] Connect server created as `<cluster_name>-connect`;
      `1password-credentials.json` saved somewhere safe (no token minted by hand)
- [ ] CI service account token created
- [ ] Git project created empty, named exactly `cluster_name`, in `git_namespace`;
      Flux token created
- [ ] `ci_runner_tag` matches a tag on a runner registered to that project
- [ ] Optional: tailnet auth key and OAuth clients, **and the MagicDNS suffix
      noted for `tailnet_dns_suffix`** (its default is a sentinel that renders a
      broken resolver)
- [ ] Every host-side item from § 4 present in the vault
- [ ] Every in-cluster item from § 4 present, including `Healthchecks Watchdog`
      and `Grafana SSO` — placeholders are fine **except** `Grafana SSO` →
      `admin-password` (applied once, at Grafana's first start) and
      `oidc-client-secret` (Terraform pushes it into Authentik); both must be
      real random values

Library and tooling

- [ ] `lib_url` decided and reachable: `git ls-remote <lib_url> 'refs/tags/<lib_ref>*'`
      prints a SHA
- [ ] `lib_project` decided — the library exists on the GitLab instance your
      pipelines run on, or you plan to vendor the CI templates
- [ ] Workstation tooling from § 9 installed, including the `task lint` gates
      (kustomize, kubeconform, gettext, helm, shellcheck, yamllint, ansible-lint,
      ruff)

Then continue with [SETUP.md](SETUP.md).
