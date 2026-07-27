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

There is also a library CLI wrapper that checks the source and destination
before handing off to copier. The console script is `weisssrv-new-project`;
`new-cluster` is a subcommand of it and takes **two** positionals — the template
source and the destination:

```bash
pipx install 'weisssrv-lib-cli[cluster] @ git+https://git.ericsweiss.com/eric/weisssrv-lib.git@v0.2.0#subdirectory=cli'
weisssrv-new-project new-cluster \
  https://git.ericsweiss.com/eric/weisssrv-cluster-template.git ~/src/mycluster \
  --vcs-ref v0.1.0
```

The library marks `new-cluster` **EXPERIMENTAL** and reserves the right to
change its flags. Plain `copier copy` is the stable path; use the wrapper only
if you want its pre-flight checks.

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
├── hosts.yml          nodes, groups, addresses, VM/CT ids, per-guest zvols
└── group_vars/
    ├── all.yml        platform defaults, secret references and version pins
    ├── proxmox.yml    hypervisor-wide settings
    ├── nas.yml        ZFS pools, datasets, NFS exports, Samba, backups
    ├── dns.yml        resolver settings
    ├── mail.yml       relay settings
    └── k3s.yml        cluster-wide k3s settings
```

There is **no `host_vars/` directory** in the generated tree, and you do not need
one to bring a cluster up: the starter roster carries per-host facts inline on
each host entry in `hosts.yml`. Create `host_vars/<host>.yml` later only if a
single host accumulates enough of its own facts to be worth splitting out —
Ansible picks it up automatically.

Work through, in order:

1. **`hosts.yml`** — rename every host and re-address it to your LAN. Every
   name and address in the shipped file is a placeholder. It contains the
   `nas` / `compute` / `dns` / `mail` / `k3s_servers` / `k3s_agents` groups
   already wired together, so this is editing, not authoring. Two entries need
   real values before their host works at all: `vfio_passthrough_pci_ids` and
   `proxmox_vm_hostpci` if you enabled the GPU.
2. **`group_vars/nas.yml`** — pool names, the dataset list, export allowlists,
   Samba shares and the backup settings. This is the longest file you will
   write and the one worth writing carefully; changing a pool name later is not
   a rename. Nothing here creates a pool — the pools must already exist
   (PRE-SETUP § 1), and a declared dataset that does not exist fails the deploy
   loudly rather than being created.
3. **Zvols for application data** are declared per guest, not in `nas.yml`: the
   `vm_additional_disks` block on a host entry in `hosts.yml` (there is a
   commented example on the NAS-adjacent k3s agent). Each entry pins a
   `scsi_slot` — never reorder or reuse one, that rebinds a data-bearing zvol to
   a different device.
4. **`group_vars/all.yml`** — check the platform defaults copier filled in
   (domains, addresses, timezone, admin user), the `secrets:` references, and
   the version pins. Versions are single-sourced here; nothing else in the
   repository names a version.

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

**One key needs narrowing by hand once the roster is real.** Copier ships
`cluster_apiserver_egress_cidr` as your whole LAN, because the k3s server
addresses live in the inventory rather than in a copier answer. Once `hosts.yml`
names your servers, replace it with their `/32`s so the API-server egress
allowance stops being LAN-wide:

```yaml
cluster_apiserver_egress_cidr: "192.168.0.31/32,192.168.0.32/32,192.168.0.33/32"
```

It must list the **server node addresses**, never the API VIP — kube-proxy
rewrites the destination before NetworkPolicy is evaluated, so allowing the VIP
allows nothing.

---

## 3. Install the collection, then run the gates

The Ansible roles are not vendored: the playbooks address `weisssrv.infra.<role>`
by FQCN and the collection is fetched at the pinned `lib_ref`. Install it
**first** — without it every playbook, and `task lint`'s `ansible:lint` step,
fails with `the role 'weisssrv.infra.<name>' was not found`:

```bash
task ansible:install-collections   # ansible-galaxy, from ansible/requirements.yml
```

Re-run it whenever `lib_ref` changes.

```bash
task lint          # yamllint, shellcheck, doc-links, taskfile-smoke,
                   # ansible-lint, terraform fmt-check + validate, flux:lint
task ansible:ping  # every host in the inventory answers
```

`task lint` is the local mirror of the CI lint stage, and doubles as the
tool-completeness check: each sub-task names the missing binary in its
precondition message. Fix everything it reports before touching a host — most
first-run failures are inventory typos it catches for free.

`task lint:prometheus-config` is **not** part of `task lint` (it needs
`promtool` and `amtool`). Run it separately if you edit alert rules.

---

## 4. Base infrastructure (Ansible)

Idempotent throughout: re-running is always safe, and is the normal way to apply
a change.

```bash
task infra:check                      # full dry run, no changes
task infra:deploy                     # the whole base layer, in dependency order
task infra:verify                     # post-deploy verification
```

**On a fresh cluster, `task infra:deploy` is the correct entry point.** It runs
`playbooks/site.yml`, and site.yml is the only thing that gets the ordering
right: it places the certificate plays *before* the NAS plays, because `nfs_tls`
fails loudly if the wildcard certificate and key are not on the storage server
yet. Running the storage playbook on its own first is the one sequence that
reliably breaks a first run.

If you would rather watch it land in stages the first time, this order respects
the same dependency:

| Order | Command | What lands |
|---|---|---|
| 1 | `task infra:base` | users, SSH hardening, packages, timezone, resolver config |
| 2 | `task dns:deploy` | filtering resolver + validating recursive resolver, **then ACME issuance and cert distribution**, then the secondary sync |
| 3 | `task storage:deploy` | NFS-over-TLS, ZFS properties and datasets, exports, Samba, backups, exporters |
| 4 | `task infra:deploy` | everything else site.yml carries: the SMTP relay, Proxmox host config, firewall, host metrics and log shipping |
| 5 | `task proxmox:ha` | HA rules, resource pools, replication jobs |
| 6 | `task infra:verify` | post-deploy verification across all of the above |

Notes on that table, because each one has bitten someone:

- **Certificates are not a separate phase.** The ACME account, DNS-01 issuance
  and distribution to non-cluster hosts run inside `dns.yml` (and inside
  `site.yml`), on the DNS hosts. There is no `certs:deploy`.
- **The SMTP relay lands in step 4, not in a step of its own.** It is
  provisioned and configured by `site.yml`, which is why step 4 is not optional.
  `playbooks/mail.yml` exists for redeploying the relay *alone* later (an
  upstream smarthost, a SASL credential); run it the same way the tasks do —
  `cd ansible && op run -- ansible-playbook -i inventories/prod/hosts.yml
  playbooks/mail.yml`. Check `task --list` first in case a wrapper task has been
  added since.
- **Run these through `task`, not bare `ansible-playbook`.** The tasks wrap the
  playbook in `op run --`; the inventory deliberately fails with
  `undef(hint=...)` when the injected values are absent, so a bare invocation
  aborts before it changes anything.
- If a host still prompts for a sudo password, `task infra:base-first-run`
  is `infra:base` with `-K`.

Point your router's DHCP at the new resolvers once step 2 is green. Until you
do, only machines configured by hand resolve internal names — including your
workstation, which will need it for the ingress hostnames later.

**SSH hardening is a one-way door.** Step 1 disables password authentication and
root login. Confirm key-based access works from a second terminal before you
close the first.

If your pools are encrypted, `task zfs:encrypt` deploys the key-load mechanism
and its boot units. It needs the in-cluster 1Password Connect endpoint, so it is
a post-Flux step, not part of this phase.

---

## 5. Kubernetes nodes (Ansible)

```bash
task k3s:provision-vms      # cloud-init VMs on Proxmox: three servers, one agent per compute node
task k3s:deploy             # base config, then k3s + kube-vip, then labels and taints
task k3s:kubeconfig         # fetch admin kubeconfig
export KUBECONFIG=~/.kube/config-k3s
kubectl get nodes
```

The task writes `~/.kube/config-k3s` — that exact name, not one derived from the
cluster name — with the server address rewritten to the API VIP. Export it
before anything else in this section: if `KUBECONFIG` points at a path that does
not exist, `kubectl` silently falls back to your default context, and every
check below (plus `task flux:bootstrap-secrets`) targets the wrong cluster.

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
cd ~/src/mycluster
ls 1password-credentials.json        # must be here, in the repository root
task flux:bootstrap-secrets
```

`1password-credentials.json` is the file `op connect server create` wrote in
PRE-SETUP § 4. Move it to the repository root — the task's precondition looks
for `./1password-credentials.json` and nowhere else. It is gitignored; do not
commit it.

The task creates the `external-secrets` namespace, mints a fresh Connect token
(`<cluster_name>-eso` against the `<cluster_name>-connect` server) and writes
the only two Kubernetes Secrets ever created by hand: `op-credentials` and
`onepassword-connect-token`. The token is deliberately not stored as a vault
item — it is re-mintable, and re-running this task is how you rotate it. Every
other Secret in the cluster is generated by ESO from an `ExternalSecret` in git.

If the task fails with an unknown-server error, the Connect server you created
is not named `<cluster_name>-connect`; create one with that name rather than
editing the task.

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

If a stage is stuck, `docs/RUNBOOKS.md` § When Flux is unhappy — in the
repository you just generated — is the entry point
([index and source](RUNBOOKS.md)). The two failures that account for most first
bootstraps are a missing vault item (the ExternalSecret names the item it
wanted) and a DNS-01 challenge that cannot complete because the API token lacks
a zone.

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
   as merge requests, not as local `task` invocations. `docs/ci-pipeline.md` in
   the generated repository is the operator-side reference: it covers which jobs
   need a **root-capable** runner (the three deploy jobs do), the CI/CD
   variables each job expects, and the node-selector regex a runner must allow.

   > **Register the privileged runner as a PROJECT runner, never instance-wide.**
   > It runs Docker-in-Docker as root, so an instance runner hands every project
   > on your GitLab — including anything a friend later hosts there — a root
   > shell on your cluster. GitLab's UI makes the wrong choice easy: registering
   > from Admin rather than the project's own Settings > CI/CD > Runners screen
   > silently creates an instance runner, and so does rotating the token through
   > the wrong screen. Verify after registering:
   >
   > ```bash
   > curl -s --header "PRIVATE-TOKEN: $TOKEN" \
   >   "$GITLAB/api/v4/runners/<id>" | jq .runner_type   # must be "project_type"
   > ```
   The runners themselves are in-cluster workloads, so this step necessarily
   comes after the platform is up.
4. **Encryption** — if your pools are encrypted, `task zfs:encrypt` now that
   1Password Connect answers inside the cluster.
5. **Backups** — offsite backup ships **disabled**
   (`restic_offsite_enabled: false`). Turning it on means filling in the repo,
   cache dir and rclone remote in `group_vars/nas.yml`, adding the credentials
   to the vault and the matching `op://` references to the `storage:deploy`
   task. Do it, then run one restore test — an untested backup is a hypothesis,
   and the `OffsiteBackup*` alerts fire against a chain that does not exist
   until you configure it.

There is no state-snapshot step: the repository is the intended state, and
`task flux:status` plus `task infra:verify` are how you diff the cluster against
it.

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
`docs/RUNBOOKS.md` **inside your generated repository** — the template ships it
there because that is what every alert's `runbook_url` points at. Its index, and
the source it is rendered from, are [here](RUNBOOKS.md).
