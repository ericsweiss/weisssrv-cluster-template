# weisssrv-cluster-template

> **Note**: the canonical source for this repository is
> [git.ericsweiss.com](https://git.ericsweiss.com/eric/weisssrv-cluster-template).
> GitHub is a read-only mirror updated by push mirroring; issues and merge
> requests go to the GitLab instance.

A [copier](https://copier.readthedocs.io/) template that generates a complete
GitOps repository for a **Proxmox + ZFS + k3s** homelab cluster: Ansible for the
host and guest layer, Terraform for external state (DNS, tailnet, SSO), and Flux
reconciling everything inside the cluster.

You answer about thirty-five questions — domains, LAN, VIPs, backends — and get
a repository that lints clean, has no site literals scattered through it, and is
ready to point at real hardware.

## What this is not

It is not a Helm chart collection and not a "one command and you have a cluster"
installer. Physical hosts, storage pools and DNS zones exist before the template
runs; see [docs/PRE-SETUP.md](docs/PRE-SETUP.md). What the template removes is
the thousand small decisions and the copy-paste between them.

## The four repositories

```
weisssrv-lib .................. the building blocks, pinned by tag
  ansible_collections/weisssrv/infra   generic host/guest roles (FQCN)
  ci/                                  GitLab CI templates (spec:inputs)
  terraform/modules/                   cloudflare-zone, tailscale-acl, authentik-sso
  scripts/                             the gates and generators CI runs
        |
        | consumed at `lib_ref` by
        v
weisssrv-cluster-template ..... THIS REPO — assembles a cluster from those blocks
        |
        | `copier copy` produces
        v
<your cluster repo> ........... one instantiation: inventory, cluster state, apps
        ^
        | tenant repos reconciled by the cluster's Flux
        |
weisssrv-project-template ..... one application deployed onto such a cluster
```

| Repository | Where |
|---|---|
| weisssrv-lib | <https://git.ericsweiss.com/eric/weisssrv-lib> |
| weisssrv-cluster-template | <https://git.ericsweiss.com/eric/weisssrv-cluster-template> (this repo) |
| weisssrv-project-template | <https://git.ericsweiss.com/eric/weisssrv-project-template> |

`weisssrv` is the reference instantiation this template was generalized from.
Kubernetes manifests live **here**, not in the library: a generated cluster is
self-contained, with no remote kustomize bases to break.

### Where the platform is documented

This repository documents *assembling a cluster*. The pieces it assembles are
documented in the library, and the generated cluster's docs and agent skill both
link there rather than restating it:

| What | Where |
|---|---|
| Role variables and behaviour | [`ansible_collections/weisssrv/infra/roles/<role>/README.md`](https://git.ericsweiss.com/eric/weisssrv-lib/-/tree/main/ansible_collections/weisssrv/infra/roles) |
| The inventory-wide variables roles alias | [collection README](https://git.ericsweiss.com/eric/weisssrv-lib/-/blob/main/ansible_collections/weisssrv/infra/README.md) |
| Role breaking changes across refs | [MIGRATING.md](https://git.ericsweiss.com/eric/weisssrv-lib/-/blob/main/ansible_collections/weisssrv/infra/MIGRATING.md) |
| CI template inputs | [docs/INCLUDE-CONTRACT.md](https://git.ericsweiss.com/eric/weisssrv-lib/-/blob/main/docs/INCLUDE-CONTRACT.md) |
| What a `lib_ref` bump can break | [docs/VERSIONING.md](https://git.ericsweiss.com/eric/weisssrv-lib/-/blob/main/docs/VERSIONING.md) |
| The vendored scripts' upstream | [docs/SCRIPTS.md](https://git.ericsweiss.com/eric/weisssrv-lib/-/blob/main/docs/SCRIPTS.md) |

The inventory that [docs/SETUP.md](docs/SETUP.md) § 2 calls "the one part no
template can generate" is filled in against those role READMEs — they define
every variable it sets.

## Quickstart

```bash
# 1. Read this first — it lists everything that must exist before you generate.
open docs/PRE-SETUP.md

# 2. Install copier (any of these)
pipx install copier
pipx install 'weisssrv-lib-cli[cluster] @ git+https://git.ericsweiss.com/eric/weisssrv-lib.git@v0.2.0#subdirectory=cli'

# 3. Generate
copier copy https://git.ericsweiss.com/eric/weisssrv-cluster-template.git ~/src/mycluster
#   or, through the library CLI (console script: weisssrv-new-project), whose
#   new-cluster subcommand checks source and destination before calling copier.
#   It takes TWO positionals, and the library marks it EXPERIMENTAL:
weisssrv-new-project new-cluster \
  https://git.ericsweiss.com/eric/weisssrv-cluster-template.git ~/src/mycluster \
  --vcs-ref v0.1.0

# 4. Bring it up
cd ~/src/mycluster
task ansible:install-collections   # the roles are fetched, not vendored
git init && git add -A && git commit -m "Generate cluster"
# then follow docs/SETUP.md in this repo (long form) or README.md "Bring-up"
# in the generated one (checklist form).
```

Non-interactive generation, for CI or a scripted rebuild —
`tests/answers-weisssrv-shaped.yml` is a complete worked answer set to copy:

```bash
copier copy --data-file my-answers.yml --defaults \
  https://git.ericsweiss.com/eric/weisssrv-cluster-template.git ~/src/mycluster
```

## The answers

`copier.yml` is the authoritative schema — help text, validators and cross-field
checks live there. Summary:

| Answer | Default | Notes |
|---|---|---|
| `cluster_name` | — | Proxmox cluster name, `kubernetes/clusters/<name>/`, hostname prefix |
| `internal_domain` | — | LAN zone; also the Kubernetes node-label namespace |
| `external_domain` | — | Internet zone; must differ from the internal one |
| `lan_cidr` | — | Drives firewall IP sets, NFS allowlists, NetworkPolicy egress |
| `lan_prefix` | derived from `lan_cidr` | First three octets, for composing host addresses |
| `k3s_api_vip` | — | kube-vip API endpoint |
| `metallb_public_vip` / `metallb_internal_vip` | — | Ingress entrypoints, public and LAN-only |
| `k3s_pod_cidr` / `k3s_service_cidr` | `10.42.0.0/16` / `10.43.0.0/16` | k3s defaults; change only on a LAN collision |
| `upstream_dns_servers` | `<lan_prefix>.21 <lan_prefix>.22` | LAN resolvers the in-cluster forwarders use |
| `admin_user` / `admin_email` | — | SSH login on every host; system-mail and ACME address |
| `alert_email` | `admin_email` | Alertmanager critical receiver |
| `timezone` | `UTC` | IANA name |
| `git_backend` / `git_host` / `git_namespace` | `gitlab_selfhosted` | Where Flux reads from and CI runs. The repository is `git_namespace/cluster_name` — there is no separate repo-name answer |
| `secrets_backend` / `onepassword_vault` | `onepassword` | Credential source for hosts and cluster |
| `dns_backend` | `cloudflare` | Zone module, external-dns, ACME DNS-01 |
| `compute_node_count` | `2` | Compute hosts in the starter inventory (plus the NAS node) |
| `nas_host` / `smtp_host` | derived from `internal_domain` | NFS server (mounted by name) and SMTP relay |
| `node_exporter_job_regex` | `node-exporter\|node-exporter-host` | Prometheus jobs the host alert rules scope to; both shipped names are required |
| `vpn_tailscale` | `false` | Overlay VPN: host role, operator, ACL module |
| `tailnet_dns_suffix` | `CHANGEME.ts.net` | Asked only with `vpn_tailscale`; MagicDNS suffix — must be replaced |
| `gpu` | `none` | `nvidia` adds VFIO prep, device plugin, DCGM |
| `lib_url` / `lib_ref` | upstream URL / `v0.2.0` | weisssrv-lib source and pin for collection, CI includes, TF modules |
| `lib_project` | path part of `lib_url` | GitLab project path for `include: project:` (instance-local) |
| `ci_runner_tag` / `ci_cpu_selector` | `infrastructure` / `<internal_domain>/cpu=modern` | Runner tag and the secret-detection CPU pin |
| `enable_semantic_release` | `false` | Adds the release stage to the generated pipeline |

Site identity has no default on purpose: a cluster cannot be generated from
someone else's addresses by accident.

## What you get

```
<cluster>/
├── ansible/
│   ├── requirements.yml            weisssrv.infra pinned at lib_ref
│   ├── inventories/prod/           hosts.yml + group_vars (yours to fill)
│   └── playbooks/                  site, base, dns, storage, k3s, maintenance/
├── terraform/                      DNS zone, tailnet ACL, SSO objects
├── kubernetes/
│   ├── clusters/<cluster_name>/    Flux entrypoint + the five stage Kustomizations
│   ├── infrastructure/             sources → crds → controllers → configs → observability
│   └── apps/                       one directory per application
├── scripts/                        verification, generators, version registry
├── docs/                           RUNBOOKS.md (what the alerts link to) + ci-pipeline.md
├── Taskfile.yml                    every operation, grouped by namespace
└── .gitlab-ci.yml                  lint / validate / deploy, from the library templates
```

No Ansible roles ship in the generated tree — playbooks address
`weisssrv.infra.<role>` by FQCN, so a platform upgrade is a one-line bump of
`lib_ref`. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Backends

| Seam | Implemented | Where it plugs in |
|---|---|---|
| Virtualization | Proxmox VE | `proxmox_*` roles, `vm_additional_disks` in the inventory |
| Storage | ZFS on the NAS node, NFS + zvol passthrough | `nas_storage`, `zvol_mount`, static PVs |
| Git / CI | Self-hosted GitLab | `.gitlab-ci.yml`, Flux `GitRepository`, runners |
| Secrets | 1Password (CLI + Connect) | `op://` refs, `ClusterSecretStore`, ExternalSecrets |
| DNS | Cloudflare | Terraform zone module, external-dns, ACME DNS-01 |
| Ingress | Traefik + cert-manager | `infrastructure/controllers`, `infrastructure/configs` |
| Overlay VPN | Tailscale (`vpn_tailscale`) | host role, operator, ACL module |
| SSO | Authentik | `apps/authentik`, `terraform/authentik` |

Unimplemented choices fail during generation with a message naming what is
missing, rather than producing a repository that will not reconcile.
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) records what a new backend has to
provide.

## Documentation

| Document | Read it when |
|---|---|
| [docs/PRE-SETUP.md](docs/PRE-SETUP.md) | Before running copier — hardware, network plan, accounts, tokens, keys |
| [docs/SETUP.md](docs/SETUP.md) | Generating, then bringing the cluster up the first time |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Understanding the shape: Flux stages, substitution, storage, DNS, backups |
| [docs/CI.md](docs/CI.md) | What the generated pipeline runs, and what it needs from your instance |
| [docs/RUNBOOKS.md](docs/RUNBOOKS.md) | Day two — reconcile, upgrade, add a node, rotate a secret. The runbooks are *shipped into* the generated cluster (that is what every alert's `runbook_url` points at); this page is the index and the source link |

## Updating a generated cluster

```bash
cd ~/src/mycluster
copier update --vcs-ref v0.2.0      # replays .copier-answers.yml against a newer template
task lint && task flux:lint
```

`copier update` produces a diff you review like any other change: it never
touches files you rewrote beyond recognition without telling you. Bumping
`lib_ref` in the same pass upgrades the Ansible collection, CI templates and
Terraform modules together. See
[docs/RUNBOOKS.md](docs/RUNBOOKS.md) § Updating the template.

## Developing this template

```bash
python3 -m pytest tests -q       # copier.yml schema + render invariants
copier copy --data-file tests/answers-weisssrv-shaped.yml --defaults . /tmp/render-smoke
yamllint -c .yamllint .
```

Conventions:

- Template content lives under `template/`; files needing substitution carry a
  `.jinja` suffix. Paths may contain answers (`clusters/{{ cluster_name }}/`).
- `trim_blocks` and `lstrip_blocks` are **off** — use explicit `{%- -%}`
  whitespace control.
- Kubernetes manifests must not interpolate answers. Site values reach them
  through the `cluster-config` ConfigMap and Flux `postBuild.substituteFrom`;
  copier fills the ConfigMap and the genuinely structural spots only. This is
  the rule that keeps a generated cluster free of the hundreds of hard-coded
  domains and addresses the reference repository accumulated.
- Ansible roles are never vendored here. If a role needs a change, it changes in
  weisssrv-lib and the template bumps `lib_ref`.

## License

MIT — see [LICENSE](LICENSE).
