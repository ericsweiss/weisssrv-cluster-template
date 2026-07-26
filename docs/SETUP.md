# Generating and bringing up a cluster

Prerequisite: everything in [PRE-SETUP.md](PRE-SETUP.md). If the hosts are not
reachable over SSH with passwordless sudo and the vault items do not exist, stop
there first — the phases below fail loudly and late otherwise.

The order is fixed and each phase leaves the cluster in a working state:

```
generate → inventory → gates → base infra → k3s → Flux → publish
```

---

## 1. Generate

```bash
pipx install copier          # or: pip install --user copier
copier copy https://git.ericsweiss.com/eric/weisssrv-cluster-template.git ~/src/mycluster
```

Copier prompts for each answer, validating as it goes: addresses must sit inside
your LAN prefix, the two domains must differ, the three VIPs must be distinct.
A rejected answer is re-asked; nothing is written until every question is
answered.

Useful flags:

| Flag | Why |
|---|---|
| `--vcs-ref v0.1.0` | pin a template release instead of `main` |
| `--data cluster_name=homelab` | answer one question from the command line |
| `--data-file answers.yml --defaults` | fully non-interactive; copy `tests/answers-weisssrv-shaped.yml` for the shape |
| `--pretend` | show what would be written, write nothing |

The template runs no post-generation scripts, so `--trust` is not required.

Through the library CLI, which validates the source and destination before
handing off to copier:

```bash
pipx install 'weisssrv-lib-cli[cluster] @ git+https://git.ericsweiss.com/eric/weisssrv-lib.git@v0.2.0#subdirectory=cli'
weisssrv-new-cluster ~/src/mycluster --vcs-ref v0.1.0
```

### First commit

```bash
cd ~/src/mycluster
git init -b main
git add -A
git commit -m "Generate cluster from weisssrv-cluster-template"
git remote add origin git@<git_host>:<git_namespace>/<cluster_name>.git
```

Do not push yet. Flux bootstrap (phase 5) will want to commit to this repository
too, and it is easier to push once the tree is real.

`.copier-answers.yml` at the root records what you answered and which template
ref produced the tree. Keep it committed — `copier update` reads it.

---

## 2. Fill in the inventory

This is the one part no template can generate: your host roster.

```
ansible/inventories/prod/
├── hosts.yml          nodes, groups, addresses, VM/CT ids
├── group_vars/
│   ├── all.yml        platform defaults and version pins
│   └── <group>.yml    per-group settings
└── host_vars/
    └── <host>.yml     per-host facts: disks, pools, NIC names, zvols
```

Work through, in order:

1. **`hosts.yml`** — one entry per Proxmox node under the virtualization group,
   with its address; then the guests you want (DNS, mail relay, applications);
   then the k3s server and agent VMs with the addresses from your network plan.
   The generated file ships the group skeleton and a commented example of each
   shape.
2. **`host_vars/<storage-node>.yml`** — pool names, dataset list, export
   allowlists, and the zvols to carve for application VMs. This is the longest
   file you will write and the one worth writing carefully; changing a pool name
   later is not a rename.
3. **`group_vars/all.yml`** — check the platform defaults copier filled in
   (domains, addresses, timezone, admin user) and the version pins. Versions are
   single-sourced here; nothing else in the repository names a version.

Then regenerate the derived files and commit them alongside:

```bash
task hosts:sync           # inventory → scripts/hosts.env, consumed by the shell tooling
task flux:sync-versions   # group_vars versions → kubernetes/.../versions-configmap.yaml
```

Both are drift-gated in CI: if the generated file does not match its source, the
pipeline fails. Never hand-edit either output.

### The site-values ConfigMap

`kubernetes/infrastructure/sources/cluster-config.yaml` holds your domains,
VIPs and CIDRs as a ConfigMap, and every Flux Kustomization substitutes from it.
Manifests reference `${cluster_internal_domain}` and friends rather than
literals. Copier filled it from your answers.

When an address changes later, edit that ConfigMap — not the manifests — and
update the matching value in `.copier-answers.yml` so a future `copier update`
does not reintroduce the old one. See
[ARCHITECTURE.md](ARCHITECTURE.md) § Substitution.

---

## 3. Run the gates

```bash
task lint          # ansible-lint, terraform fmt/validate, flux:lint, script tests
task ansible:ping  # every host in the inventory answers
```

`task lint` is the local mirror of the CI lint stage. Fix everything it reports
before touching a host — most first-run failures are inventory typos it catches
for free.

---

## 4. Base infrastructure (Ansible)

Idempotent throughout: re-running is always safe, and is the normal way to apply
a change.

```bash
task infra:check                      # full dry run, no changes
task infra:deploy                     # everything below, in dependency order
```

If you would rather watch it happen in stages — recommended the first time:

| Order | Command | What lands |
|---|---|---|
| 1 | `ansible-playbook ansible/playbooks/base.yml` | users, SSH hardening, packages, timezone, resolver config, node exporters |
| 2 | `task storage:deploy` | ZFS properties and datasets, NFS exports, SMB, SMART monitoring |
| 3 | `task dns:deploy` | filtering resolver + validating recursive resolver, internal records, secondary sync |
| 4 | `task certs:deploy` | ACME account, DNS-01 issuance, distribution to non-cluster hosts |
| 5 | `ansible-playbook ansible/playbooks/mail.yml` | SMTP relay and the per-host null clients |
| 6 | `ansible-playbook ansible/playbooks/proxmox-ha.yml` | HA rules, resource pools, replication jobs |
| 7 | `task infra:verify` | post-deploy verification across all of the above |

Point your router's DHCP at the new resolvers once step 3 is green. Until you
do, only machines configured by hand resolve internal names — including your
workstation, which will need it for the ingress hostnames later.

**SSH hardening is a one-way door.** Step 1 disables password authentication and
root login. Confirm key-based access works from a second terminal before you
close the first.

---

## 5. Kubernetes nodes (Ansible)

```bash
task k3s:provision-vms      # cloud-init VMs on Proxmox: three servers, one agent per compute node
task k3s:deploy             # base config, then k3s + kube-vip, then labels and taints
task k3s:kubeconfig         # fetch admin kubeconfig
export KUBECONFIG=~/.kube/config-<cluster_name>
kubectl get nodes
```

Expect every node `Ready`, and the API VIP to answer:

```bash
ping -c3 <k3s_api_vip>
kubectl get nodes -o wide
kubectl get pods -n kube-system
```

The servers form an etcd quorum; with three of them the cluster survives losing
one. Agents carry the workloads and are labelled by role (storage-adjacent,
ingress, general, GPU) — the labels are what every workload's affinity rule
selects on, so a missing label surfaces later as an unschedulable pod.

Both tasks are idempotent. Re-running `task k3s:deploy` after changing a version
pin upgrades in place; use `task maintenance:update-k3s-nodes` when you want the
draining, one-node-at-a-time version.

---

## 6. Flux (GitOps)

From here on, the cluster's contents come from git. There is no `kubectl apply`
and no `helm upgrade` in the normal flow.

### 6a. Bootstrap the secrets backend

External Secrets Operator needs credentials for the secret store *before* it can
fetch any secret — the one bootstrapping problem GitOps cannot solve for itself.

```bash
task flux:bootstrap-onepassword         # prints exactly what to create and how
task flux:bootstrap-onepassword-apply   # creates both Secrets from the artifacts
```

That produces the only two Kubernetes Secrets ever created by hand: the Connect
server's credentials file and its access token. Every other Secret in the
cluster is generated by ESO from an `ExternalSecret` in git.

### 6b. Bootstrap Flux

```bash
git push -u origin main     # Flux needs the repository to exist and contain kubernetes/
task flux:bootstrap
```

This reads the git token from the vault, installs the Flux controllers, commits
their manifests to `kubernetes/clusters/<cluster_name>/flux-system/`, and creates
the `GitRepository` plus the top-level `Kustomization` that watches this repo.

### 6c. Watch it converge

```bash
task flux:status      # concise: every Kustomization, HelmRelease, ExternalSecret
flux get all -A       # the long form
```

Reconciliation walks the stage graph — sources, then CRDs, then controllers, then
configs, then observability and applications in parallel. A fresh bootstrap
takes several minutes; charts are pulled, certificates are issued over DNS-01,
and the ingress controller cannot be Ready before MetalLB assigns it an address.

Verify the platform is actually serving:

```bash
kubectl get svc -A | grep LoadBalancer     # the two VIPs are assigned
kubectl get certificates -A                # Ready=True, not stuck Pending
kubectl get externalsecrets -A             # SecretSynced
task flux:verify
```

If a stage is stuck, [RUNBOOKS.md](RUNBOOKS.md) § When Flux is unhappy is the
entry point. The two failures that account for most first bootstraps are a
missing vault item (the ExternalSecret names the item it wanted) and a DNS-01
challenge that cannot complete because the API token lacks a zone.

---

## 7. Publish and hand over

Once the platform is Ready:

1. **Terraform** — apply the external state that could not exist before now:
   ```bash
   task terraform:plan     # review
   task terraform:apply
   ```
   This creates the public DNS records, and the tailnet policy if you enabled it.
2. **Router** — forward `443/tcp` to `metallb_public_vip`.
3. **CI** — register runners, add the CI service-account token as a masked
   variable, and push a branch to watch the pipeline run. From here changes ship
   as merge requests, not as local `task` invocations.
4. **Snapshot** — `task collect-state` writes a full picture of what you just
   built. Keep it; it is what disaster recovery diffs against.
5. **Backups** — configure the offsite target and run one restore test before
   you trust it. An untested backup is a hypothesis.

---

## 8. What "working" looks like

| Check | Command | Expected |
|---|---|---|
| Hosts converged | `task infra:verify` | no failures |
| Nodes healthy | `task k3s:status` | all Ready, etcd quorum intact |
| GitOps healthy | `task flux:status` | every resource Ready=True |
| Secrets flowing | `kubectl get externalsecrets -A` | all SecretSynced |
| Certificates | `kubectl get certificates -A` | all Ready=True |
| Internal name | `dig +short <anything>.<internal_domain>` | the internal ingress VIP |
| External name | `dig +short <anything>.<external_domain> @1.1.1.1` | your public address |
| Ingress serving | `curl -I https://<host>.<internal_domain>` | 200/302 with a valid certificate |

---

## 9. First-run troubleshooting

| Symptom | Usually |
|---|---|
| Ansible: `UNREACHABLE` | the address in `hosts.yml`, or the key not deployed for `admin_user` |
| Ansible: `/usr/bin/python3: not found` | no interpreter on the target — install it, it is a prerequisite |
| Playbook stops after SSH hardening | you were authenticating with a password; deploy the key first |
| VMs provision but never answer SSH | cloud-init did not get the key — check the vault item's field names |
| Nodes `NotReady` | check the k3s service on the node; then that the API VIP is reachable from it |
| API VIP does not answer | kube-vip needs the address free and on the same L2 as the servers |
| `LoadBalancer` stuck `<pending>` | the MetalLB pool does not contain the VIP, or the pool is not reconciled yet |
| `ExternalSecret` `SecretSyncError` | the item or field name in the vault does not match the manifest |
| `Certificate` stuck | DNS-01 propagation, or a token scoped to the wrong zone |
| Literal `${cluster_...}` in a live object | the ConfigMap is missing a key, or the Kustomization does not substitute from it |

Day-two operations, upgrades and incident procedures continue in
[RUNBOOKS.md](RUNBOOKS.md).
