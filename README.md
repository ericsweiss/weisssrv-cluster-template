# weisssrv-cluster-template

> **Note**: the canonical source for this repository is
> [git.ericsweiss.com](https://git.ericsweiss.com/eric/weisssrv-cluster-template).
> GitHub is a read-only mirror updated by push mirroring; issues and merge
> requests go to the GitLab instance.

A [copier](https://copier.readthedocs.io/) template that generates a complete
GitOps repository for a **Proxmox + ZFS + k3s** homelab cluster: Ansible for the
host and guest layer, Terraform for external state (DNS, tailnet, SSO), and Flux
reconciling everything inside the cluster.

You answer questions — domains, LAN, VIPs, backends — and get a repository that
lints clean, has no site literals scattered through it, and is ready to point at
real hardware.

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
  terraform/modules/                   cloudflare-zone, tailscale-acl, authentik-sso, unifi-network
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
weisssrv-app-template ..... one application deployed onto such a cluster
```

| Repository | Where |
|---|---|
| weisssrv-lib | <https://git.ericsweiss.com/eric/weisssrv-lib> |
| weisssrv-cluster-template | <https://git.ericsweiss.com/eric/weisssrv-cluster-template> (this repo) |
| weisssrv-app-template | <https://git.ericsweiss.com/eric/weisssrv-app-template> |

`weisssrv` is the reference instantiation this template was generalized from.
Kubernetes manifests live **here**, not in the library: a generated cluster is
self-contained, with no remote kustomize bases to break.

The app template is a copier template too, with the same cluster-identity
answers (`external_domain`, `internal_domain`, `internal_vip`, the registry
hosts, `runbook_url`) and the same rule that none of them has a default — so
onboarding a tenant onto a cluster you generated is `copier copy` plus that
cluster's answers, not a hand sweep for site values. The cluster side of the
contract (the `Kustomization.spec.path`, the namespace labels, the
`ClusterSecretStore` name) is documented in the generated
`kubernetes/clusters/<cluster_name>/tenants/README.md`, which is what the
operator applies.

### Where the platform is documented

This repository documents *assembling a cluster*. The pieces it assembles are
documented in the library — see
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) § Where the platform is documented
for the table of which library document owns what.

The inventory that [docs/SETUP.md](docs/SETUP.md) § 2 calls "the one part no
template can generate" is filled in against the library's role READMEs — they
define every variable it sets.

## Quickstart

```bash
# 1. Read this first — it lists everything that must exist before you generate.
open docs/PRE-SETUP.md

# 2. Install copier (any of these)
pipx install 'copier>=9.15.0'   # or: uv tool install 'copier>=9.15.0'
pipx install 'weisssrv-lib-cli[cluster] @ git+https://git.ericsweiss.com/eric/weisssrv-lib.git@vX.Y.Z#subdirectory=cli'  # vX.Y.Z = the release you'll answer for lib_ref (default in copier.yml)

# 3. Generate
copier copy https://git.ericsweiss.com/eric/weisssrv-cluster-template.git ~/src/mycluster
#   or, through the library CLI (console script: weisssrv-new-project), whose
#   new-cluster subcommand checks source and destination before calling copier.
#   It takes TWO positionals, and the library marks it EXPERIMENTAL:
weisssrv-new-project new-cluster \
  https://git.ericsweiss.com/eric/weisssrv-cluster-template.git ~/src/mycluster

# Both generation blocks above are runnable as written: an unpinned VCS source
# resolves to the template's latest release tag. docs/SETUP.md lists the flags
# that pin a different one, and docs/VERSIONING.md names the library release
# the template is validated against.

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
| `lan_gateway` | `<lan_prefix>.1` | Default route for every host and guest; both guest-provisioning roles assert it |
| `k3s_api_vip` | — | kube-vip API endpoint |
| `metallb_public_vip` / `metallb_internal_vip` | — | Ingress entrypoints, public and LAN-only |
| `k3s_pod_cidr` / `k3s_service_cidr` | `10.42.0.0/16` / `10.43.0.0/16` | k3s defaults; change only on a LAN collision |
| `upstream_dns_servers` | `<lan_prefix>.21 <lan_prefix>.22` | LAN resolvers the in-cluster forwarders use |
| `admin_user` / `admin_email` | — | SSH login on every host; system-mail and ACME address |
| `alert_email` | `admin_email` | Alertmanager critical receiver |
| `timezone` | `UTC` | IANA name |
| `git_backend` / `git_host` / `git_namespace` | `gitlab_selfhosted` / — / — | Where Flux reads from and CI runs. The repository is `git_namespace/cluster_name` — there is no separate repo-name answer |
| `secrets_backend` / `onepassword_vault` | `onepassword` / — | Credential source for hosts and cluster |
| `storage_backend` | `zfs` | What the NAS node serves datasets and PVs from; also selects the ZFS-only pool scrape |
| `dns_backend` | `cloudflare` | Zone module, external-dns, ACME DNS-01 |
| `compute_node_count` | `2` | Compute hosts in the starter inventory (plus the NAS node) |
| `k3s_image_gc_high_threshold` | `70` | Root-filesystem percentage the kubelet starts image GC at, against a 50% low watermark. The `KubeletImageGCIneffective` alert uses the same number. Bounded 55-79 |
| `nas_host` / `smtp_host` | derived from `internal_domain` | NFS server (mounted by name) and SMTP relay; both must stay under `internal_domain` — the wildcard certificate covers that zone only |
| `node_exporter_job_regex` | `node-exporter\|node-exporter-host` | Prometheus jobs the host alert rules scope to; both shipped names are required |
| `vpn_tailscale` | `false` | Overlay VPN: host role, operator, ACL module |
| `tailnet_dns_suffix` | *(none — asked)* | Asked only with `vpn_tailscale`; MagicDNS suffix, rejected if left at the `CHANGEME` placeholder |
| `gpu` | `none` | `nvidia` adds VFIO prep, driver + container toolkit, device plugin; GPU telemetry is a documented add-on, **not shipped** (`kubernetes/infrastructure/observability/README.md`) |
| `use_unifi` | `false` | Manages a UniFi gateway as code: `terraform/unifi`, its supervised tasks, a drift-plan job. The generated site data is a worked example to edit |
| `compose_app_guests` | `[]` | Single-VM docker-compose guests `task collect-state` collects from. Each entry needs `host`, `label` and `compose_dir` |
| `lib_url` / `lib_ref` | defaults in `copier.yml` | weisssrv-lib source and pin for collection, CI includes, TF modules |
| `lib_project` | path part of `lib_url` | GitLab project path for `include: project:` (instance-local) |
| `ci_runner_tag` / `ci_cpu_selector` | `infrastructure` / `<internal_domain>/cpu=modern` | Runner tag and the secret-detection CPU pin |
| `license` | `mit` | `mit` writes LICENSE and a README section; `none` leaves the repository unlicensed |
| `license_holder` / `license_year` | — | Copyright line in LICENSE; asked only when `license` is `mit` |
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
├── docs/                           RUNBOOKS.md (what the alerts link to), ci-pipeline.md, security-posture.md
├── .claude/skills/                 the cluster-development agent skill; CLAUDE.md
│                                   and AGENTS.md at the root point at it
├── Taskfile.yml                    every operation, grouped by namespace
├── taskfiles/                      one file per namespace, included by Taskfile.yml
└── .gitlab-ci.yml                  lint / validate / deploy, from the library templates
```

No Ansible roles ship in the generated tree — playbooks address the collection's
40 roles as `weisssrv.infra.<role>` by FQCN, so a platform upgrade is a one-line
bump of `lib_ref`. The variables those roles take are documented in the library:
each role's README lists every default it has and why.
See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Backends

| Seam | Implemented | Where it plugs in |
|---|---|---|
| Virtualization | Proxmox VE | `proxmox_*` roles, `vm_additional_disks` in the inventory |
| Storage (`storage_backend`) | ZFS on the NAS node, NFS + zvol passthrough | `nas_storage`, `zvol_mount`, static PVs, the pool scrape |
| Git / CI | Self-hosted GitLab | `.gitlab-ci.yml`, Flux `GitRepository`, runners |
| Secrets | 1Password (CLI + Connect) | `op://` refs, `ClusterSecretStore`, ExternalSecrets |
| DNS | Cloudflare | Terraform zone module, external-dns, ACME DNS-01 |
| Ingress | Traefik + cert-manager | `infrastructure/controllers`, `infrastructure/configs` |
| Overlay VPN | Tailscale (`vpn_tailscale`) | host role, operator, ACL module |
| GPU | NVIDIA (`gpu`) | VFIO host prep, node driver, `nvidia-device-plugin` — telemetry (DCGM) is operator-added |
| Gateway / VLANs | UniFi (`use_unifi`) | `terraform/unifi`, the `unifi-drift-plan` job — off by default, and nothing in the cluster depends on it |
| SSO | Authentik | `apps/authentik`, `terraform/authentik` |

Where a seam is selectable it is a copier question that accepts only the values
that are implemented, so an unimplemented choice fails during generation with a
message naming what is missing rather than producing a repository that will not
reconcile. Virtualization has no such question: nothing in the generated tree
would be left to select.
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) § Backend seams records what a new
backend has to provide on the cluster side, and the library's
[docs/EXTENSIBILITY.md](https://git.ericsweiss.com/eric/weisssrv-lib/-/blob/main/docs/EXTENSIBILITY.md)
is the contract on the role side — the named variables whose default is today's
behaviour, and the rule that a second backend is a sibling role family, never a
fork.

## Documentation

| Document | Read it when |
|---|---|
| [docs/PRE-SETUP.md](docs/PRE-SETUP.md) | Before running copier — hardware, network plan, accounts, tokens, keys |
| [docs/SETUP.md](docs/SETUP.md) | Generating, then bringing the cluster up the first time |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Understanding the shape: Flux stages, substitution, storage, DNS, backups |
| [docs/CI.md](docs/CI.md) | What the generated pipeline runs, and what it needs from your instance |
| [docs/RUNBOOKS.md](docs/RUNBOOKS.md) | Day two — reconcile, upgrade, add a node, rotate a secret. The runbooks are *shipped into* the generated cluster (that is what every alert's `runbook_url` points at); this page is the index and the source link |
| [docs/VERSIONING.md](docs/VERSIONING.md) | What this template's MAJOR/MINOR/PATCH mean, how a cluster pins a template release, and what `copier update` does across versions |

## Updating a generated cluster

```bash
cd ~/src/mycluster
copier update --vcs-ref <template-tag>   # replays .copier-answers.yml against a newer template
task lint && task flux:lint
```

`<template-tag>` is a tag of **this** repository — not `lib_ref`, which is
weisssrv-lib's own version and is answered separately. List what exists with
`git ls-remote --tags https://git.ericsweiss.com/eric/weisssrv-cluster-template.git`.

`copier update` produces a diff you review like any other change: it never
touches files you rewrote beyond recognition without telling you. Bumping
`lib_ref` in the same pass upgrades the Ansible collection, CI templates and
Terraform modules together. See
[docs/RUNBOOKS.md](docs/RUNBOOKS.md) § Updating the template for the procedure,
and [docs/VERSIONING.md](docs/VERSIONING.md) for what a given bump is allowed to
change — read the target release's notes before updating across a MAJOR.

## Developing this template

### Local gates

Run these before opening a merge request. This is the canonical list;
`AGENTS.md` points here rather than restating it.

```bash
python3 -m pytest tests -q                      # copier.yml schema + render invariants
python3 tests/validate_render.py --lib-path ~/src/weisssrv-lib
python3 tests/validate_render.py --lib-path ~/src/weisssrv-lib \
  --answers tests/answers-unlike.yml
ruff check scripts tests template/tests template/scripts template/kubernetes
shellcheck template/scripts/*.sh
python3 scripts/check-doc-links.py
yamllint -c lint/yamllint-relaxed.yml copier.yml tests/ lint/ .gitlab-ci.yml scripts/vendored-manifest.yml
```

That is the CI lint and test stages, in the order the pipeline runs them.
`template/` itself is linted through the rendered tree, not directly. A change
that skips the ruff, shellcheck or link lines can still go red in the pipeline
on a file the tests never open.

Both fixtures, always, and `--lib-path` on both. `pytest tests` also runs
`scripts/check-lib-pins.py` over this repository's own `include:` refs.

[docs/CI.md](docs/CI.md) owns the rest: § The gate that matters for why the
shaped render alone proves nothing, and § Running the tests locally for the
validator's flags and the tools each check needs — including `--skip <check>`,
which runs everything but one check when a tool is missing locally. CI never
passes `--skip`.

### Conventions

- Template content lives under `template/`; files needing substitution carry a
  `.jinja` suffix. Paths may contain answers (`clusters/{{ cluster_name }}/`).
- `trim_blocks` and `lstrip_blocks` are **off** — use explicit `{%- -%}`
  whitespace control.
- A derivation two template files must agree on lives in `partials/`, imported
  as `{% import "partials/<name>.jinja" as x with context %}`. That directory is
  outside `template/`, so it is never copied into a generated cluster — only its
  results are. `partials/ci-sizing.jinja` is the worked example.
- Kubernetes manifests must not interpolate answers. Site values reach them
  through the `cluster-config` ConfigMap and Flux `postBuild.substituteFrom`;
  copier fills the ConfigMap and the genuinely structural spots only.
- Ansible roles are never vendored here. If a role needs a change, it changes in
  weisssrv-lib and the template bumps `lib_ref`.
- Every library tag written literally in this repository's docs is the `lib_ref`
  default in `copier.yml`. Bump them together; the test suite compares them.
- A `task <name>` in any document must be a task the generated `Taskfile.yml`
  defines — the render tests resolve every reference against it.

## License

MIT — see [LICENSE](LICENSE).
