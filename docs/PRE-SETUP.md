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

- **Three compute nodes minimum** if you want the k3s control plane to tolerate
  a node failure: the template lays out three server VMs holding an etcd quorum,
  and putting two of them on the same host defeats it.
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
| Router / gateway address | — | usually `.1` |
| DHCP pool range | — | must **not** overlap anything below |
| Proxmox host addresses | — | one per node, static, contiguous block |
| Infrastructure guest addresses | — | DNS, mail relay, application VMs |
| k3s server VM addresses | — | three, contiguous |
| k3s agent VM addresses | — | one per compute node, contiguous |
| Kubernetes API VIP | `k3s_api_vip` | floating, assigned by kube-vip |
| Public ingress VIP | `metallb_public_vip` | the address you port-forward to |
| Internal ingress VIP | `metallb_internal_vip` | LAN-only services |

A worked plan for a `/24`, which you are free to copy or ignore — the point is
that the blocks are contiguous and outside DHCP:

```
x.x.x.1          router
x.x.x.2-.99      DHCP pool and workstations
x.x.x.100        public ingress VIP
x.x.x.101        internal ingress VIP
x.x.x.102-.109   Proxmox hosts
x.x.x.150-.169   infrastructure and application guests
x.x.x.161        Kubernetes API VIP
x.x.x.200-.219   k3s agent VMs
x.x.x.220-.229   k3s server VMs
```

Also decide and note:

- **Port forwards** on the router: `443/tcp` (and `80/tcp` if you want HTTP-01
  fallback or redirects) to the public ingress VIP. Nothing else has to be
  exposed; the overlay VPN covers remote administration.
- **DHCP reservations** are not used for anything the template manages — every
  managed address is static.
- Two of the VIPs answer ARP from whichever node currently holds them. If your
  switch does port security or your network does IP-MAC binding, allow that.

The Kubernetes pod and service networks default to k3s' own `10.42.0.0/16` and
`10.43.0.0/16`. Change them only if they collide with your LAN, and if you do,
change them before the first `k3s` deploy — they are not re-negotiable later.

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

### Cloudflare (the implemented DNS backend)

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

# Connect server credentials — produces ./1password-credentials.json plus a
# token you mint separately. Keep the JSON file out of git; it is the one
# artifact that cannot be re-derived.
op connect server create <cluster-name> --vaults Homelab
op connect token create eso --server <cluster-name> --vaults Homelab

# CI service account, scoped read-only to the same vault
op service-account create ci --vault Homelab:read_items
```

Both Connect artifacts become the only two Kubernetes Secrets ever created by
hand; everything else in the cluster is produced from them. Losing them is
recoverable (regenerate and re-create the two Secrets); losing the vault is not.

### Items to create

Create these before the phase that needs them — the generated repository's docs
list the exact field names, and each application adds its own. The platform set:

| Item | Fields | Needed by |
|---|---|---|
| SSH key | public key, private key | base host configuration |
| DNS provider token | credential, account id | certificates, external DNS, DDNS |
| DNS provider Terraform token | credential, account id | Terraform |
| SMTP relay upstream | username, app password | outbound system mail |
| SMTP relay auth | username, password | hosts authenticating to the relay |
| Mail alias | root alias address | where system mail lands |
| DNS admin | username, password | the filtering resolver's web UI |
| File share user | password | SMB access to the NAS |
| Certificate distribution key | private key, public key | pushing renewed certs to non-cluster hosts |
| k3s cluster token | credential | node join |
| Git access token | credential | Flux reading the repository |
| CI service account token | credential | pipeline secret injection |
| Connect access token | credential | External Secrets Operator |
| Storage pool passphrases | passphrase, one item per encrypted pool | unlocking pools at boot |
| Alert webhook | url | alert delivery |
| Virtualization API token | user, token name, token secret | the Proxmox metrics exporter |

Generate every random value with something like `openssl rand -base64 32` and
paste it in; do not invent memorable ones.

> One value deserves special care: if you enable offsite backups, the backup
> repository password can never be rotated and its loss makes every offsite copy
> undecryptable. Keep a copy **outside** the vault.

---

## 5. Git host

`git_backend: gitlab_selfhosted` is the implemented choice. GitHub is a declared
seam; copier will refuse it until it exists.

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
  first and let CI take over afterwards.
- The library, `weisssrv-lib`, must be **resolvable from your instance** for
  `include: project:` to work. If you are not on the instance that hosts it,
  plan to vendor the CI templates instead; this is the one part of the generated
  pipeline that assumes a shared instance.

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

## 9. Workstation tooling

```bash
# macOS
brew install copier go-task/tap/go-task ansible jq yq
brew install hashicorp/tap/terraform
brew install --cask 1password-cli
brew install kubernetes-cli fluxcd/tap/flux

# Debian/Ubuntu: pipx install copier; the rest from each project's instructions
```

Versions the generated repository expects: Task 3.x, Ansible core 2.18+,
Terraform 1.15+, `op` 2.x, Python 3.11+, copier 9+.

---

## 10. Pre-flight checklist

Hardware and OS

- [ ] Proxmox VE 9+ installed on every node, clustered
- [ ] Static addresses, gateway, and outbound DNS working on every node
- [ ] Repositories configured, systems updated, rebooted
- [ ] ZFS pools created on the storage node, `zpool status` clean
- [ ] Compute-node local pools created
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
- [ ] Vault created; CLI signed in; Connect credentials and token generated
- [ ] CI service account token created
- [ ] Git project created empty; Flux token created
- [ ] Optional: tailnet auth key and OAuth clients
- [ ] Every item from § 4 present in the vault

Then continue with [SETUP.md](SETUP.md).
