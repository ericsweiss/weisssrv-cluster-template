# Runbooks

Day-two operations for a cluster generated from this template. Procedures only;
the reasoning behind the shapes is in [ARCHITECTURE.md](ARCHITECTURE.md).

`task --list` in the generated repository is the authoritative command
reference — the names below are the ones the template ships with.

---

## The change workflow

Every change, including one-line fixes, takes the same path:

```bash
git switch -c <branch>
# edit
task lint                      # the local mirror of the CI lint stage
task flux:lint                 # if kubernetes/ changed
git commit && git push -u origin <branch>
# open a merge request; let the pipeline and review run
# merge → Flux reconciles kubernetes/, CI deploys the ansible side
task flux:status && task infra:verify
```

Never push to the default branch. Flux and the deploy jobs act on it, so a push
is a deploy without a review.

Gates worth remembering, because forgetting them fails the pipeline rather than
your machine:

| You touched | Also run | Commit |
|---|---|---|
| a version pin in `group_vars/all.yml` | `task flux:sync-versions` | the ConfigMap alongside |
| `hosts.yml` | `task hosts:sync` | `scripts/hosts.env` alongside |
| anything under `kubernetes/` | `task flux:lint` | — |
| alert or recording rules | `task lint:prometheus-config` | — |

---

## Making Flux act now

```bash
task flux:status                     # every Kustomization, HelmRelease, ExternalSecret
task flux:verify                     # flux check + the full resource list
task flux:reconcile                  # force source refresh + reconcile everything

flux reconcile kustomization apps --with-source
flux reconcile helmrelease <name> -n <namespace>
```

Reconciliation is push-triggered, with a short poll as the fallback, so a merged
change normally lands within a minute. Forcing is for when you are watching.

Fast local iteration, reverted on the next pass — use it to check a render, not
to deploy:

```bash
task flux:dev-apply -- kubernetes/apps/<app>
```

---

## Deploying an Ansible change

```bash
task infra:check                                   # dry run, everything
task infra:check -- --limit <host>                 # dry run, one host
ansible-playbook ansible/playbooks/<play>.yml --limit <host> --tags <tag>
task infra:deploy                                  # the whole base layer
task infra:verify
```

All roles are idempotent; re-running is the normal way to apply a change and the
first thing to try when a host has drifted.

---

## Upgrades

### An application or chart version

```bash
task maintenance:check-versions           # what is outdated, across every source
task maintenance:update-version SERVICE=<name>
task flux:sync-versions
git add ansible/inventories/prod/group_vars/all.yml kubernetes/infrastructure/sources/versions-configmap.yaml
git commit -m "Bump <name>"
```

Versions are pinned deliberately. Nothing tracks `latest`, and there is no bot
merging bumps unattended — the scheduled bump job opens one merge request and
leaves it for a human.

### Host packages

```bash
task maintenance:update-packages          # add -- -e auto_reboot=true to reboot
task infra:verify
```

Silence the alerts that a planned restart will trip **before** starting, or the
next real incident arrives in a channel everyone has learned to ignore:

```bash
task observability:silence ALERT=<name> DURATION=1H
```

### Kubernetes nodes

```bash
# bump k3s_version in group_vars/all.yml, then:
task maintenance:update-k3s-nodes         # drains, upgrades and uncordons node by node
task k3s:status
```

Servers first, one at a time, watching the etcd quorum. Rolling back is a
version bump in the other direction plus the same task.

### The template itself

See § Updating the template.

---

## Adding a node

### A hypervisor host

1. Install the OS, static address, join the cluster (`pvecm add <existing-node>`).
2. Create `admin_user` with passwordless sudo and deploy your SSH key.
3. Add it to `hosts.yml` under the virtualization group; add a `host_vars` file
   with its disks and NIC names.
4. `task hosts:sync` and commit.
5. `ansible-playbook ansible/playbooks/base.yml --limit <host>`
6. Redeploy the firewall so the new address enters the derived IP sets:
   `ansible-playbook ansible/playbooks/site.yml --tags proxmox_firewall`
7. Verify: `pvecm status`, `pve-firewall status`, `ansible <host> -m ping`.

### A Kubernetes agent

1. Add the VM to `hosts.yml` under the agent group with its address and the
   host it lives on; give it the role labels you want it scheduled by.
2. `task hosts:sync`, commit.
3. `task k3s:provision-vms -- --limit <vm>`
4. `task k3s:deploy -- --limit <vm>`
5. `kubectl get nodes` — the new node is `Ready` and carries its labels.

Adding a **server** additionally changes the etcd quorum: go from three to five,
never to four, and verify `kubectl get nodes -l node-role.kubernetes.io/etcd`
before and after. The API-server egress allowlist is derived from the inventory,
so the new address propagates into the NetworkPolicies through the ConfigMap —
nothing to hand-edit.

---

## Adding an application

**In the cluster**: create `kubernetes/apps/<name>/` — namespace, workload,
storage, `ExternalSecret` if it needs credentials, certificate, ingress route,
NetworkPolicy, autoscaling policy — and add it to the parent kustomization. Copy
the closest existing neighbour rather than writing from scratch; the shapes are
deliberately uniform. Reference site values as `${cluster_...}` substitutions,
never as literals.

**As a guest**: define the VM or container in `hosts.yml` with its resources and
any zvols, add a playbook, and route to it through the ingress. Guests need the
same treatment as pods: firewall rules, backups, log shipping, metrics, an
alert.

Either way the service is not finished until it has logs, metrics, a
down-or-stale alert, and a probe if users hit it directly.

---

## Rotating a secret

**In-cluster** (anything an `ExternalSecret` produces):

```bash
# update the item in the vault, then:
task flux:rotate-secret -- <app>              # force sync + restart consumers
task flux:refresh-secret -- <ns>/<name>       # sync only, no restart
```

Restarting matters more than it looks: the operator re-fetches on a long
interval and the reloader deliberately ignores Secret changes, so a rotation
without a restart is a silent no-op.

**Host-side** (anything injected with `op run`): update the item, then re-run the
playbook that consumes it.

**The bootstrap credentials** cannot rotate themselves — regenerate the Connect
credentials and token and recreate the two Kubernetes Secrets by hand, exactly
as during bootstrap.

---

## Certificates

```bash
kubectl get certificates -A                                    # in-cluster
kubectl describe certificaterequest -n <ns> <name>             # why it is stuck
ansible-playbook ansible/playbooks/site.yml --tags acme_certs  # host-side renewal
```

Almost every failure is one of: the DNS token cannot see the zone, the challenge
record has not propagated yet, or the account hit a rate limit after a loop of
failed attempts. Check propagation with `dig +short TXT _acme-challenge.<name>
@1.1.1.1` before assuming anything is broken.

Renewed host-side certificates are distributed to the hosts that need them over
SSH with a pinned host key; a distribution failure after a rebuild usually means
the pinned key is stale.

---

## Suspending, rolling back, and breaking glass

```bash
flux suspend kustomization apps                # stop reconciling a stage
flux suspend helmrelease <name> -n <ns>        # stop reconciling one release
flux resume  ...                               # same arguments

git revert <commit> && git push                # the normal rollback
helm history <release> -n <ns>                 # while suspended, if you must
```

Suspend from leaf to root so no stage reconciles into a half-suspended state.
Anything you do by hand while suspended is undone by the resume unless you have
also changed git — which is the point.

---

## Storage

**A degraded pool**

```bash
zpool status -v <pool>          # which device, which errors
zpool replace <pool> <old> /dev/disk/by-id/<new>
zpool status 1                  # watch the resilver
```

Always reference disks by `/dev/disk/by-id/`. Check SMART on the surviving
members before assuming the failure is isolated; drives bought together fail
together.

**NFS clients hung after a server restart**

Established mounts do not recover from a server that went away: the export is
back but the client holds a stale handle. Restart the consumers — for pods, delete
them so they remount. If the server itself refuses to stop, reset the failed unit
and start it again rather than forcing a reboot.

**Something landed on the wrong storage class**

Every PV in the cluster is static with `storageClassName: ""`. If an alert says
one is on the default class, the claim was created before the manifest was
fixed — a StatefulSet's volume template is immutable, so the fix is: scale to
zero, copy the data to the intended volume, delete the claim, recreate it bound
to the right volume, scale back up.

---

## Backups and restore

```bash
task disaster-recovery:list                    # what exists, where
task b2:check                                  # offsite repository health
```

Rehearse restores on a schedule, not after an incident:

1. Restore one application's most recent logical dump into a scratch database
   and diff a table you know.
2. Restore one file from the offsite repository and compare checksums.
3. Boot one guest from its hypervisor backup on a spare host.

Two values are unrecoverable by design and belong somewhere outside the vault
that holds them: the offsite repository password, and any application's own
backup-encryption key. Losing either makes the corresponding copies decorative.

---

## When Flux is unhappy

| Symptom | First command | Usual cause |
|---|---|---|
| Kustomization stuck `Reconciling` | `flux get kustomizations` then `kubectl describe kustomization <n> -n flux-system` | a dependency stage is not Ready, or `wait: true` on something that never becomes healthy |
| `HelmRelease Ready=False` | `flux get hr -A` then `kubectl describe hr <n> -n <ns>` | values schema change across a chart major, or a CRD it expects is missing |
| `ExternalSecret SecretSyncError` | `kubectl describe externalsecret <n> -n <ns>` | item or field name does not exist in the vault |
| Literal `${cluster_...}` in a live object | `task flux:lint` | key missing from `cluster-config`, or the Kustomization does not substitute from it |
| Pod `Pending` forever | `kubectl describe pod` | affinity selecting a node label that no node carries |
| Everything Ready, nothing works | `kubectl get svc -A \| grep LoadBalancer` | address pool exhausted or the VIP is not being advertised |

```bash
kubectl logs -n flux-system deploy/kustomize-controller --tail=100
kubectl logs -n flux-system deploy/helm-controller --tail=100
```

---

## Post-failover reconciliation

After the HA layer moves a guest, the repository and the cluster disagree about
where things live. Reconcile deliberately: confirm the guest is healthy on its
new host, check whether replication jobs still point at the right source, and
either accept the new placement in the inventory or migrate it back. Leaving it
undecided means the next deploy fights the HA manager.

---

## Updating the template

```bash
cd <cluster-repo>
copier update --vcs-ref v0.2.0       # replays .copier-answers.yml against a newer template
git diff                             # review like any other change
task lint && task flux:lint
```

Copier three-way-merges: files you never touched update cleanly, files you
rewrote produce conflicts you resolve by hand. Review the diff — a template
update can change defaults, not just add files.

To change a site value copier baked in (a VIP, a domain), edit the value in
`.copier-answers.yml`, run `copier update`, and check that the corresponding
`cluster-config` key changed. Editing only the ConfigMap works until the next
update reintroduces the old answer.

Bumping `lib_ref` in the same pass upgrades the Ansible collection, the CI
templates and the Terraform modules together — read the library's changelog
first, then:

```bash
ansible-galaxy collection install -r ansible/requirements.yml --force
task lint
```

---

## Where to look first

| Question | Command |
|---|---|
| Is the platform healthy? | `task flux:status` |
| Are the hosts converged? | `task infra:verify` |
| Are the nodes healthy? | `task k3s:status` |
| What changed recently? | `git log --oneline -20` |
| What does the cluster actually look like? | `task collect-state` |
| Why is this service down? | its dashboard, then its logs, then its NetworkPolicy |
