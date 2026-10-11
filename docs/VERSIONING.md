# Versioning & release tags

This repository is a **copier template**. Its releases are `vMAJOR.MINOR.PATCH`
tags, cut automatically from conventional commits by the `release` stage in
`.gitlab-ci.yml`. What follows is what a bump *means* for a template, and what a
generated cluster does with one.

## The public API of a template

A library's API is its functions. A template's API is **what a generated
repository has to live with**, which is two distinct surfaces:

1. **The answer schema** — every question in [`copier.yml`](../copier.yml). The
   names are recorded verbatim in each generated repo's `.copier-answers.yml`
   and replayed on every `copier update`, so a question name is as public as any
   exported symbol.
2. **The rendered file layout** — the paths, and the *meaning* of the paths,
   under [`template/`](../template). An operator edits those files, writes
   runbooks against them, and points alert `runbook_url`s at them. Moving one is
   not a private refactor; it is a rename in somebody else's repository, applied
   by a tool that will ask them to resolve the conflict.

Everything else — comments, the wording of `help:` text, the docs in this
repository — is not API.

## The `cluster-config` key set

`template/kubernetes/infrastructure/sources/cluster-config.yaml.jinja` is part
of that second surface. kustomize-controller runs with
`StrictPostBuildSubstitutions`, so every `${cluster_*}` a manifest spells has to
be a key the ConfigMap declares — renaming or dropping one is MAJOR, and
`template/tests/test_check_cluster_literals.py` holds the declared set to a
recorded list in both directions so neither happens by accident. The rendered
table in `template/kubernetes/README.md.jinja` is the per-key reference.

The set is a deliberate **superset** of the reference cluster's: a generated
cluster is configured by answer, where the reference cluster hand-writes the
same values into its manifests and inventory. `cluster_api_vip` is the name both
spell; `cluster_k3s_api_vip` is this template's alias for it, read by
`template/taskfiles/k3s.yml.jinja`, not a rename.

| Key only this template carries | Why a generated cluster needs it |
|---|---|
| `cluster_name` | Names the `clusters/<name>/` directory and the external-dns owner ID, which the reference cluster spells literally |
| `cluster_k3s_api_vip` | Reserved alias of `cluster_api_vip`, so a manifest written with the `k3s_` segment resolves instead of substituting empty |
| `cluster_apiserver_egress_cidr` | Pod egress to `:6443` must name node addresses, not the VIP kube-proxy DNATs; the starter roster is inventory data, so the key widens to the LAN |
| `cluster_etcd_endpoints` | `kubeEtcd` scrape roster, generated from the starter inventory's servers |
| `cluster_node_exporter_host_addresses` | EndpointSlice roster for the host node-exporters (`:9101`), generated the same way |
| `cluster_zfs_exporter_addresses` | Same roster for `zfs_exporter` (`:9134`), emitted only for the ZFS storage backend |
| `cluster_unbound_exporter_addresses` | Same roster for `unbound_exporter` (`:9167`), one entry per resolver |
| `cluster_offsite_backup_probe_metric`, `cluster_archive_backup_probe_metric`, `cluster_vzdump_probe_metric`, `cluster_backup_artifact_probe_metric` | A backup tier a generated cluster has not enabled yet points its alert's `absent()` arm at `up` (the per-app dump tier ships armed); the reference cluster runs every tier, so its rules name each metric outright |
| `cluster_issuer` | The ClusterIssuer name several manifests reference, so a cluster can carry its own |
| `cluster_secret_store` | SECRETS seam — the ClusterSecretStore name comes from the backend answer |
| `cluster_secrets_provider_namespace`, `cluster_secrets_provider_deployment` | Same seam: the provider Deployment `SecretsProviderDown` watches |
| `cluster_secrets_vault` | The vault answer every ExternalSecret resolves against |
| `cluster_git_host` | Forge seam — the host `cluster_runbook_base_url` and the registry paths are built from |

Four of the reference cluster's keys are deliberately absent. Each needs the
feature that reads it before the key means anything, and a key nothing announces
or allowlists is drift of its own:

| Key not carried | What carrying it takes |
|---|---|
| `cluster_home_cidr`, `cluster_home_admin_cidr` | A client VLAN and its admin block are site topology; the answers describe one flat LAN. Adding them is a question pair plus the `lan-tailscale-only` / `lan-tailscale-strict` allowlist entries that read them |
| `cluster_syslog_vip` | The generated cluster ships no syslog receiver, so the key would name a VIP nothing announces — and `check-cluster-literals.py`'s VIP arm rejects a VIP with no inventory mirror. It arrives with the receiver app, a `syslog_vip` answer and its own MetalLB `/32` pool |
| `cluster_wg_easy_vip` | Same shape: `vpn_tailscale` is the only VPN module, so no generated manifest announces a WireGuard endpoint |

A manifest ported in either direction brings its keys with it. Port into this
template and the key becomes an answer or a generated value; port out of it and
the receiving cluster declares the key by hand.

## MAJOR / MINOR / PATCH

| Level | Meaning for this template |
|---|---|
| **MAJOR** | A generated repo cannot take the update without hand work. A question renamed or removed (its recorded answer no longer binds and the operator is re-prompted, or the value is silently lost); a question added with **no** default (`copier update` cannot run non-interactively); a validator tightened so a previously accepted answer is now refused; a raised `_min_copier_version`, which copier refuses on before rendering; a rendered path moved, renamed or deleted; a change that makes a rendered file's *content* incompatible with state the cluster already holds (a renamed Flux Kustomization, a changed PV/zvol mount path, a renamed ConfigMap key that live manifests substitute). |
| **MINOR** | New capability that an existing answer set can absorb. A new question **with** a default that reproduces today's render; a new rendered file; a new Ansible play, Flux stage member, or Taskfile task; a bumped `lib_ref` default (see below). |
| **PATCH** | A fix that changes no question, no path and no resolved value for an unchanged answer set. A corrected template expression, a fixed conditional, docs, comments, tests. |

Two consequences worth stating outright:

- **A default change is a real change.** Defaults are only consulted at *first*
  `copier copy` — an existing repo replays its recorded answer, so a moved
  default does not reach it. But it changes what every *new* cluster gets, so it
  is at least MINOR, and MAJOR when the old default is no longer accepted.
- **`lib_ref` is a question, not a pin this repository owns.** Moving its
  default selects a different weisssrv-lib release for new clusters. Read that
  release's notes and its
  [VERSIONING.md](https://git.ericsweiss.com/eric/weisssrv-lib/-/blob/main/docs/VERSIONING.md)
  first: a library MAJOR reaching a generated cluster through a template MINOR
  is exactly the surprise this table exists to prevent, so a `lib_ref` default
  bump that crosses a library MAJOR is a template MAJOR. Every place the answer
  lands in a generated repository — CI includes, the collection `version:`, the
  Terraform module `?ref=`, the Taskfile's `LIB_REF` — derives from that one
  answer, so a generated cluster is the easy case; the pin sites and their gates
  are tabulated in the library's
  [docs/VERSIONING.md](https://git.ericsweiss.com/eric/weisssrv-lib/-/blob/main/docs/VERSIONING.md)
  (§ "Upgrading a consumer"). The library keeps no registry of its consumers.

While the template is **0.x**, a breaking change bumps MINOR rather than cutting
1.0.0 (semver's pre-1.0 allowance, and the release job's `major_on_zero` input
stays `false`). The release notes lead with a **Breaking changes**
section — read it before running `copier update`.

## Which library release the template is validated against

`lib_ref` is an answer, so a generated cluster can pin any library tag it likes —
but exactly **one** pair is ever proved to work: the `lib_ref` default in
`copier.yml`, currently weisssrv-lib `v0.19.0`. `validate-rendered-cluster`
renders the template with the answer fixtures — which do not answer `lib_ref`,
so they inherit that default — and runs the real toolchain over the output
against a checkout of the library at that ref.

That default is the single source, and three places have to agree with it: the
fixtures inherit it, this repository's own `include:` refs repeat it as literals
(`include:` is resolved before job variables exist), and
`tests/validate_render.py`'s `_assert_one_lib_ref` fails any disagreement. They
move together, in one MR. `docs/CI.md` § Library pin carries the rest of the
gate detail.

An unpinned `copier copy` resolves to the template's **latest release tag**, so
when `main` carries a newer `lib_ref` than the newest tag does, the documented
quickstart generates a cluster on the older library. Cut a template release
after a `lib_ref` bump, or generate with `--vcs-ref HEAD` to take `main`.

**Other pairs are untested, not unsupported.** A cluster answering some other
`lib_ref` is a combination nothing here exercised; the library's own
[VERSIONING.md](https://git.ericsweiss.com/eric/weisssrv-lib/-/blob/main/docs/VERSIONING.md)
is what says whether that pin is allowed to break it.

## How a consumer pins a version

A generated repository records the template ref it was built from in
`.copier-answers.yml`:

```yaml
_commit: v0.1.0
_src_path: https://git.ericsweiss.com/eric/weisssrv-cluster-template.git
```

`_commit` is copier's version marker, and **a tag is what makes it one**. On an
untagged template copier falls back to the branch tip — it prints `No git tags
found in template; using HEAD as ref`, records a bare commit SHA, and every
subsequent `copier update` pulls whatever has landed on `main` since, reviewed
or not. With tags present, `copier update` resolves to the **latest tag** and
ignores unreleased commits, which is the entire difference between an upgrade
and a moving target.

Generate at a chosen release rather than at whatever is current. List what
exists first — `<template-tag>` is a tag of **this** repository, not `lib_ref`:

```bash
git ls-remote --tags https://git.ericsweiss.com/eric/weisssrv-cluster-template.git
copier copy --vcs-ref <template-tag> \
  https://git.ericsweiss.com/eric/weisssrv-cluster-template.git ~/src/mycluster
```

## What `copier update` does across versions

```bash
cd ~/src/mycluster
copier update                          # -> the latest tag
copier update --vcs-ref <template-tag> # -> a specific release
```

Copier renders the template **twice** — once at `_commit`, once at the target
ref — diffs the two renders, and applies that diff to your working tree as a
three-way merge. Three things follow from that mechanic:

- **Your edits survive.** A file you changed takes the template's *diff*, not
  the template's *version*; only a hunk that touches the same lines conflicts,
  and it arrives as ordinary conflict markers to resolve. Commit before
  updating — copier refuses to run on a dirty tree, and the diff is what you
  review.
- **Answers are replayed, not re-derived.** Your recorded answers come back as
  the defaults, so an interactive update is Enter-through and `--defaults`
  skips the prompts entirely; only a genuinely new question has anything new to
  offer. This is why renaming a question is MAJOR: the old key stays in
  `.copier-answers.yml` binding nothing, and the new one silently takes the
  template's default instead of your value.
- **Skipping versions is fine, and reading them is not optional.** The diff
  from the first tag to the fourth is applied in one pass, so every intervening
  MAJOR's hand work lands at once, unlabelled. Read each release's notes
  between the two tags before starting.

Deletions and moves are the sharp edge: copier applies them, and a rendered file
you had rewritten can come back as a conflict or be removed outright. That is
the whole reason a moved path is MAJOR here.

After any update, before committing:

```bash
task lint          # yamllint + shellcheck + ruff + ansible-lint
task flux:lint     # kustomize build + kubeconform, if kubernetes/ moved
```

## Releases are cut automatically (conventional commits)

Merging to `main` runs the vendored
[`scripts/semantic-release.py`](../scripts/semantic-release.py) through the
library's `ci/release/semantic-release.yml` template: it reads the conventional
commits since the last tag, decides the bump, and creates the tag **and** the
GitLab Release with generated notes in one Releases-API call.

| commit subject | bump |
|---|---|
| `feat:` | MINOR |
| `fix:` / `perf:` / `refactor:` | PATCH |
| any `type!:`, or a `BREAKING CHANGE:` trailer | MAJOR — MINOR while 0.x |
| `docs:` `ci:` `build:` `test:` `chore:` `style:` `revert:` | none — listed in the notes, never releases on its own |

The bump comes from the commit subject, so **a change that breaks a generated
repo must be written `feat!:` (or carry a `BREAKING CHANGE:` trailer)** or it
ships as a patch and nobody is warned. Map the table above onto the API
definition at the top of this page before you write the subject line: renaming a
copier question is a breaking change even when the diff is one word.

No releasable commit means no release (exit 0), so re-running on an
already-released commit is a no-op. The `release` stage is declared **last** and
the job sets no `needs:`, so a tag is only ever cut from a commit where
`validate-rendered-cluster` — both fixtures, the real toolchain — went green.

## Vendored copies

Some files here are copies of library files rather than local work.
`scripts/vendored-manifest.yml` records them, the library publishes the offer
list in `scripts/vendorable-paths.yml`, and its `check-vendored-copies.py` does
the comparison. Every `lib:` path must appear in that offer list at the pinned
ref.

The manifest records two relationships. A `vendored` entry must stay
byte-identical, and drift in either direction fails. A `forked` entry must stay
different, needs a `reason:` naming a difference the file actually contains, and
when it sets `reconciled_sha256` the check also fails if the library side moved
since the fork was last reconciled. A fork whose only divergence is comments and
blank lines must also set `comment_only: true`; without it the check rejects the
entry, because the `reason:` no longer describes real divergence.

The manifest lists both copy sets. Unprefixed paths are what
this repository runs on itself; `template/`-prefixed ones are rendered into
every generated cluster. The two sets drift separately, so re-vendoring one is
not re-vendoring the other. An entry is a bare string when both repos use the
same path, or a mapping with `lib:` and `consumer:` when they differ.
`template/.gitleaks.toml.jinja` and
`template/.gitlab/secret-detection-ruleset.toml.jinja` are Jinja sources rather
than copies, so they are not listed.

Absence from the manifest means template-local, not drift. Many gates under
`template/scripts/` are written here and have no library counterpart, among
them `check-cluster-literals.py`, `check-tenant-wiring.py` and
`deploy-verify.sh`. The validator's orphan scan holds that line: a script with
no library twin has to be declared in the render's own `scripts/README.md`
under "Local helpers".

`scripts/semantic-release.py` is **vendored** from weisssrv-lib and must stay
byte-identical to the library's copy at the ref `.gitlab-ci.yml` pins;
`tests/validate_render.py`'s `vendored` check enforces that for this
repository's copy and for the one under `template/scripts/`. Re-copy it in the
same MR that bumps the library ref.

A vendored gate that imports a sibling module needs that module vendored with
it. Register the pair in both manifests, or the gate ships without the module
it imports and exits 2 naming the missing file.

Four gates under `template/scripts/` share a name with a library gate and are
template-owned forks rather than copies: `check-flux-version-pin.py`,
`check-guest-endpoint-parity.py`, `check-secret-rotation-coverage.py` and
`flux-child-kustomizations.py`. Each takes its own flags and prints its own
output shape, so its call sites in `template/.pre-commit-config.yaml`,
`template/.gitlab-ci.yml.jinja`, `template/taskfiles/`,
`template/scripts/deploy-verify.sh` and the shipped test suites read the
template's CLI. Each is a declared `forked:` entry carrying the difference in
its `reason:`, held to the library by `reconciled_sha256` so a library-side
change forces a review.

## Related

- [RUNBOOKS.md](RUNBOOKS.md) § Updating the template — the operator-side procedure
- [CI.md](CI.md) — what the generated pipeline runs
- [weisssrv-lib VERSIONING.md](https://git.ericsweiss.com/eric/weisssrv-lib/-/blob/main/docs/VERSIONING.md)
  — the library's own tags, which `lib_ref` selects
