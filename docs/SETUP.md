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
| `--vcs-ref <template-tag>` | pin an older template release, or `HEAD` for unreleased work; `git ls-remote --tags <template-url>` lists what exists |
| `--data cluster_name=homelab` | answer one question from the command line |
| `--data-file answers.yml --defaults` | fully non-interactive; copy `tests/answers-weisssrv-shaped.yml` for the shape |
| `--pretend` | show what would be written, write nothing |

The template runs no post-generation scripts, so `--trust` is not required.

There is also a library CLI wrapper that checks the source and destination
before handing off to copier. The console script is `weisssrv-new-project`;
`new-cluster` is a subcommand of it and takes **two** positionals — the template
source and the destination:

```bash
pipx install 'weisssrv-lib-cli[cluster] @ git+https://git.ericsweiss.com/eric/weisssrv-lib.git@v0.6.0#subdirectory=cli'
weisssrv-new-project new-cluster \
  https://git.ericsweiss.com/eric/weisssrv-cluster-template.git ~/src/mycluster
```

Both blocks above are runnable as written, which is why neither passes
`--vcs-ref`: copier resolves an unpinned VCS source to the template's **latest
release tag**, falling back to the branch tip only if the template has cut none.
Add the flag from the table to pin an older release, and only with a tag that
exists — a ref the template has not cut fails at clone time, before a single
question is asked. Whichever ref is used is recorded as `_commit` in
`.copier-answers.yml` and is what the next `copier update` diffs from.

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

Both outputs are drift-gated, locally by `task lint:repo-sync` (part of
`task lint`) and in CI by the `repo-sync` job: each regenerates the file from its
source and diffs, so a hand-edited or stale output fails. Never hand-edit
either.

That gate is also why these two commands belong **here** rather than later.
`scripts/hosts.env` ships as an inert placeholder — every list empty, so a task
that iterates one stops with an explicit message instead of SSHing somewhere
wrong — and a placeholder is by definition out of sync with the roster. Until you
run `task hosts:sync`, § 3's `task lint` reports exactly that, naming the command
to run.

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

The Ansible roles are not vendored: there is no `ansible/roles/` in the
generated tree at all. The playbooks address the collection's 40 roles as
`weisssrv.infra.<role>` by FQCN, and `ansible/requirements.yml` fetches the
collection at the pinned `lib_ref`. Install it **first** — without it every
playbook, and `task lint`'s `ansible:lint` step, fails with
`the role 'weisssrv.infra.<name>' was not found`:

```bash
task ansible:install-collections   # ansible-galaxy, from ansible/requirements.yml
```

Re-run it whenever `lib_ref` changes.

That is also where the inventory you just wrote is *documented*: each role's
`README.md` in the collection defines the variables it takes, the collection
README lists the inventory-wide ones every role aliases, and `MIGRATING.md` is
the map of what a `lib_ref` bump renames or newly asserts. Read it before a
bump, not after — a variable this collection renamed does not raise
`AnsibleUndefinedVariable`, it quietly falls back to the role's default on a
green play.

```bash
task lint          # yamllint, shellcheck, ruff, doc-links, taskfile-smoke,
                   # lib-pins, version-coverage, repo-sync, ansible-lint,
                   # terraform fmt-check + validate, flux:lint
task ansible:ping  # every host in the inventory answers
```

`task lint` is the local mirror of the CI lint stage, and doubles as the
tool-completeness check: each sub-task names the missing binary in its
precondition message. Fix everything it reports before touching a host — most
first-run failures are inventory typos it catches for free.

One expected exception, if you skipped the two sync commands in § 2:
**`lint:repo-sync` fails, and that is the design.** It names the command to run.

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
right: it provisions the DNS and relay containers before anything connects to
them, and it places the certificate plays *before* the NAS plays, because
`nfs_tls` fails loudly if the wildcard certificate and key are not on the
storage server yet. Running the storage playbook on its own first is the one
sequence that reliably breaks a first run.

It still takes **two passes** the first time, for the one thing Ansible cannot
do for you — read the next section before running anything.

### The one-time certificate step

Everything TLS in this cluster — NFS-over-TLS, DNS-over-TLS, SMTP submission —
hangs off a single wildcard certificate issued on the primary resolver and
pushed to the other hosts over SSH. Ansible installs and *renews* it, but the
**first issuance is one manual command**, and the push targets cannot be pinned
until the containers they name exist. So a fresh cluster takes two passes:

```bash
# 1. First pass. Hold back the storage server: its NFS-over-TLS server config
#    is the one thing that hard-fails without the wildcard cert.
task infra:deploy -- --limit '!<nas-host>'

# 2. Pin each distribution target's SSH host key — the containers exist now.
task certs:show-host-keys
#    Paste each printed host_key into the matching _acme_certs_targets entry in
#    ansible/inventories/prod/group_vars/dns.yml. Entries left empty are skipped,
#    not fatal: that host simply never receives the certificate.

# 3. Issue the wildcard, once, on the primary resolver. acme.sh never issues by
#    itself; the certificate play prints this command when no cert exists yet.
#    Read the DNS token on your workstation first — the resolver has no `op`:
op read "op://<vault>/Cloudflare DNS Token/credential"    # -> CF_Token
op read "op://<vault>/Cloudflare DNS Token/username"      # -> CF_Account_ID

ssh <admin_user>@dns-01
sudo -i
export CF_Token='<credential>' CF_Account_ID='<account id>'
/root/.acme.sh/acme.sh --issue --dns dns_cf --server letsencrypt \
  --keylength ec-256 -d '<internal_domain>' -d '*.<internal_domain>'

# 4. Second pass. Installs the cert locally, distributes it to every pinned
#    target, and lands the storage server.
task infra:deploy
```

acme.sh saves those two variables into its own account config, so renewals need
them only this once. `--server letsencrypt` is passed explicitly so the command
also works against an acme.sh install this role did not pin; acme.sh 3.x
otherwise defaults to ZeroSSL, which a Let's-Encrypt-only CAA record refuses.

From then on the acme.sh cron renews and re-pushes on its own, and `task
infra:deploy` is a single idempotent pass.

If you would rather watch it land in stages the first time, this order respects
the same dependency:

| Order | Command | What lands |
|---|---|---|
| 1 | `task infra:base -- --limit proxmox` | users, SSH hardening, packages, timezone on the bare-metal hosts — limited to them because no container exists yet |
| 2 | `task dns:deploy` | provisions the resolver containers, then the validating recursive resolver and the filtering frontend, then acme.sh + the renewal cron + the cert push channel, then the secondary sync |
| 3 | `task infra:deploy -- --limit mail` | provisions the SMTP relay container, its base config **and its Postfix config** — the last of which is written against a certificate that has not landed yet and is corrected by step 6. **Do not skip it or move it later:** it is a certificate distribution target, and step 4 can only pin the host key of a host that exists |
| 4 | the certificate step above | pin the host keys, issue the wildcard once, re-run `task dns:deploy` to distribute it |
| 5 | `task storage:deploy` | NFS-over-TLS, ZFS properties and datasets, exports, Samba, backups, exporters |
| 6 | `task infra:deploy` | everything else site.yml carries, plus the re-run that makes the relay's Postfix config work against the now-distributed certificate: Proxmox host config, firewall, host metrics and log shipping |
| 7 | `task proxmox:ha` | HA rules, resource pools, replication jobs |
| 8 | `task infra:verify` | post-deploy verification across all of the above |

The two-pass path at the top of this section gets step 3 for free — its first
pass is `site.yml` minus the storage host, which provisions the resolvers *and*
the relay before anything is pinned. Splitting the stages is what re-introduces
the ordering, which is why it is a numbered step here.

If you do end up pinning before a target exists, nothing is lost and nothing is
silent: the empty pin is skipped by the certificate play, `task infra:verify`
fails naming that host, and § 9's host-key row is the loop back — re-run
`task certs:show-host-keys`, paste the pin, re-run `task dns:deploy`.

Notes on that table, because each one has bitten someone:

- **Certificates are not a separate phase.** The acme.sh install, the renewal
  cron and distribution to non-cluster hosts all run inside `dns.yml` (and
  inside `site.yml`), on the DNS hosts. There is no `certs:deploy` — only
  `task certs:show-host-keys`, which captures the pins those pushes need.
- **Step 3 configures the relay as well as creating it, and that is fine.**
  Step 3 is `site.yml` limited to the `mail` group, and `--limit` selects hosts,
  not plays — so the `Deploy SMTP relay configuration` play runs too, writing
  `main.cf` with `smtpd_tls_cert_file` pointing into `/etc/postfix/tls/`, which
  is still an empty directory. Nothing fails: the role creates that directory
  and starts Postfix regardless. What you get from step 3 is a container with an
  address and an SSH host key — which is all step 4 needs — plus a submission
  service (587, `smtpd_tls_security_level: encrypt`) that cannot complete a TLS
  handshake **until step 6 re-runs against the distributed certificate**. Local
  mail queues rather than bouncing in the meantime, so nothing is lost. The
  two-pass path at the top of this section behaves identically, which is why it
  needs no equivalent caveat. `task mail:deploy` exists for redeploying the
  relay *alone* later (an upstream smarthost, a SASL credential, a Postfix
  parameter).
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

> **The first pipeline's `flux-lint` job is expected to fail, once.** It builds
> the cluster root, which lists `flux-system/` — and `flux-system/`'s
> kustomization names `gotk-components.yaml` and `gotk-sync.yaml`, which
> `flux bootstrap` has not written yet. The local `task flux:lint` guards
> exactly this case and prints "cluster root skipped"; the library's CI job runs
> the same build without the guard, so the push above is the one window where
> the two disagree. Bootstrap commits both files, and the next pipeline is
> green. Most operators never see it — PRE-SETUP § 5 notes that the runners are
> themselves in-cluster workloads, so the first pipelines usually queue rather
> than run.

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

1. **Terraform (public DNS)** — apply the external state that could not exist
   before now. Every module keeps its state in the GitLab HTTP backend, so each
   one needs `init` before its first plan — `task lint`'s `terraform:validate`
   inits with `-backend=false` and will not do it for you:
   ```bash
   task terraform:init     # once per module, per checkout
   task terraform:plan     # review
   task terraform:apply
   ```
   These three tasks are the **zone module only** (`terraform/cloudflare`): the
   public DNS records.

   The apex record is applied with a **placeholder address** (`apex_seed_ip`,
   a TEST-NET-1 address) and its content is owned from then on by the in-cluster
   `cloudflare-ddns` CronJob. Until that job's first run the apex resolves to
   the placeholder while the Terraform plan is clean, so verify the job exists
   and force one run rather than waiting on its schedule:
   ```bash
   kubectl -n cloudflare-ddns create job --from=cronjob/cloudflare-ddns ddns-first-run
   dig +short <external_domain> @1.1.1.1     # your public address, not 192.0.2.1
   ```
2. **Terraform (tailnet)** — only with `vpn_tailscale`. The ACL policy is its
   own module with its own state, and its apply is supervised — it overwrites
   the live policy in one shot and a bad one severs tailnet and SSH access to
   every node. Read `terraform/tailscale/README.md`, then:
   ```bash
   task terraform:tailscale-init
   task terraform:tailscale-plan
   task terraform:tailscale-apply   # refuses -auto-approve on purpose
   ```
   The SSO module (`terraform/authentik`) is the third one; it comes after the
   identity provider exists, in step 4.
3. **Router** — forward `443/tcp` to `metallb_public_vip`.
4. **Single sign-on** — the platform is green but nobody can sign in to
   anything yet. Grafana ships SSO-only (no login form, no basic auth) and its
   OIDC endpoints point at `auth.<internal_domain>` — this cluster's own
   Authentik, which Flux has just deployed with no objects in it. Bring it up
   before you hand the cluster to anyone.

   **In the Authentik UI** (this part cannot be code: the Terraform module
   authenticates with a token that only exists once an admin does):

   ```
   https://auth.<internal_domain>/if/flow/initial-setup/
   ```

   That flow sets the password for the built-in `akadmin` user and is available
   **only until an admin exists** — run it the moment the ingress answers, not
   next week. Then, still in the UI: Directory > Tokens > Create, for `akadmin`,
   and store the value in the vault as `Authentik Terraform Token` →
   `credential`.

   **In code**, everything else. `terraform/authentik/sso.tf` already ships the
   Grafana provider, application, `grafana-users` group and the policy binding
   that gates it, so the object inventory is one supervised apply:

   ```bash
   task terraform:authentik-init
   task terraform:authentik-plan     # review object by object
   task terraform:authentik-apply    # refuses -auto-approve on purpose
   ```

   Three things that apply does **not** decide for you:

   - **Admin access.** Grafana maps `grafana-admins` to Admin and
     `grafana-users` to Viewer, with `ROLE_ATTRIBUTE_STRICT` on: an account in
     neither group is refused, not defaulted. Add a `"grafana-admins"` entry to
     the `groups` map in `sso.tf`, put your account in **both** groups (the
     application's policy binding gates on `grafana-users`), and re-apply. The
     module manages membership, never users, so the username has to exist in
     Authentik already — on a fresh install that is `akadmin` and nobody else.
   - **The client id** is the map key — the literal string `grafana`. The vault
     item `Grafana SSO` → `oidc-client-id` must be exactly that.
   - **The client secret** is supplied *to* Authentik, not read back from it.
     The three `terraform:authentik-*` tasks share one env block, and it already
     injects `TF_VAR_oauth2_client_secret_grafana` from `Grafana SSO` →
     `oidc-client-secret`. So Terraform is authoritative: whatever is in that
     field becomes the provider's client secret, and Grafana reads the same
     field through its ExternalSecret — the two match by construction, and
     rotation is one vault edit plus a re-apply. What that means for you is that
     the field must hold a **real random value** (`openssl rand -base64 32`)
     *before* the apply. PRE-SETUP § 4 blesses placeholders on this item for the
     fields that are re-read every reconcile; this is not one of them.
     `terraform/authentik/README.md` § Adding an application is the pattern to
     repeat for every app you add.

   Then push the OIDC values into the cluster and sign in:

   ```bash
   task flux:refresh-secret -- observability/observability-secrets
   kubectl -n observability rollout restart deploy/kube-prometheus-stack-grafana
   open https://grafana.<internal_domain>
   ```

   (Reloader is configured with `ignoreSecrets: true`, so a credential Secret
   changing is deliberately a manual restart.)

   **The third field on that item does not work this way.** `Grafana SSO` →
   `admin-password` is the break-glass built-in admin, and Grafana applies it
   only while its user table is empty — that is, at its **first** start, which
   has already happened by the time you read this. The refresh and restart above
   change the OIDC credentials and leave that account exactly as it was. If the
   field held a placeholder in phase 0, the account still has the placeholder.
   Fix it now, in one change, so the vault and the database agree:

   ```bash
   # 1. put a real random value in Grafana SSO -> admin-password
   task flux:refresh-secret -- observability/observability-secrets
   # 2. make the database agree — nothing else does
   kubectl -n observability exec deploy/kube-prometheus-stack-grafana -c grafana -- \
     grafana cli --homepath /usr/share/grafana admin reset-admin-password '<new>'
   ```

   That is also the rotation procedure from here on.

   If the identity provider is ever down, or a group mapping locks everyone out,
   Grafana's break-glass is a two-line change: set `auth.disable_login_form:
   false` and `auth.basic.enabled: true` in
   `kubernetes/infrastructure/observability/kube-prometheus-stack/release.yaml`,
   reconcile, and sign in as `admin` with `Grafana SSO` → `admin-password`.
   Revert both when the IdP is back —
   `kubernetes/infrastructure/observability/README.md` explains why the account
   exists at all, and why its password is a one-shot.
5. **CI** — register runners, add the CI service-account token as a masked
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
6. **Protect the default branch.** Flux reconciles `kubernetes/` from it and the
   deploy jobs run Ansible against your hosts from it, so an unprotected default
   branch means a direct push is an unreviewed deploy. In **Settings >
   Repository > Protected branches**, on `main`:

   | Setting | Value |
   |---|---|
   | Allowed to merge | Maintainers |
   | Allowed to push and merge | No one — the merge-request path is the only one |
   | Allowed to force push | No |

   Do this **after** phase 6b, not before: `flux bootstrap` commits the
   controller manifests to this branch itself, and it is the one operation that
   needs to push directly. If you ever re-bootstrap, lift the protection for
   that push and put it back.

   The generated pipeline agrees with this shape already — the deploy jobs are
   default-branch-only, and `deploy-ansible-k3s` and `deploy-terraform` are
   manual on top of that.
7. **Encryption** — if your pools are encrypted, `task zfs:encrypt` now that
   1Password Connect answers inside the cluster.
8. **Backups** — offsite backup ships **disabled**
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
| SSO answering | `curl -I https://auth.<internal_domain>/if/flow/default-authentication-flow/` | 200 — the identity provider is reachable by name |
| Signed in | browse `https://grafana.<internal_domain>` | redirected to Authentik, back to Grafana, and your account has the role its group grants |

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
| Cert push fails with a host-key error | the target was rebuilt, or its pin is still empty — `task certs:show-host-keys`, paste into `_acme_certs_targets`, re-run `task dns:deploy` |
| `nfs_tls` on the NAS: cert/key missing | the wildcard has not been issued or has not reached that host — § 4's certificate step |
| Grafana redirects to a login you cannot pass | the OIDC objects, the groups or the client secret — § 7 step 4; the break-glass admin is in the same step |
| Literal `${cluster_...}` in a live object | the ConfigMap is missing a key, or the Kustomization does not substitute from it |

Day-two operations, upgrades and incident procedures continue in
`docs/RUNBOOKS.md` **inside your generated repository** — the template ships it
there because that is what every alert's `runbook_url` points at. Its index, and
the source it is rendered from, are [here](RUNBOOKS.md).
