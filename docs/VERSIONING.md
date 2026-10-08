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
stays `false`). The release notes still lead with a **Breaking changes**
section — read it before running `copier update`.

## Which library release a template release was validated against

`lib_ref` is an answer, so a generated cluster can pin any library tag it likes —
but exactly **one** pair per template release is ever proved to work, and that is
the pair `tests/answers-weisssrv-shaped.yml` holds. `render-validate` renders the
template with that fixture and runs the real toolchain over the output against a
checkout of the library at that ref, so the fixture is the record of what was
tested, not a preference.

An unpinned `copier copy` resolves to the template's **latest release tag**, so
when `main` carries a newer `lib_ref` than the newest tag does, the documented
quickstart generates a cluster on the older library. Cut a template release
after a `lib_ref` bump, or generate with `--vcs-ref HEAD` to take `main`.

| Template release | Rendered and validated against |
|---|---|
| `v0.1.0` | weisssrv-lib `v0.2.0` |
| `v0.2.0` | weisssrv-lib `v0.5.2` |
| `v0.3.0` | weisssrv-lib `v0.6.2` |
| `v0.4.0` | weisssrv-lib `v0.7.4` |
| `v0.5.0` | weisssrv-lib `v0.8.0` |
| `v0.6.0` | weisssrv-lib `v0.9.5` |
| `v0.7.0` | weisssrv-lib `v0.9.8` |
| `v0.8.0` | weisssrv-lib `v0.13.0` |
| `main` (unreleased) | weisssrv-lib `v0.18.0` |

Rules that keep the table meaningful:

- The `lib_ref` **default** in `copier.yml` and this repository's own `include:`
  refs move together, in one MR — the answer fixtures do not answer `lib_ref`
  at all, so they inherit that default and there is nothing to bump there. They
  are compared by the test suite, so a partial bump fails rather than shipping a
  default the pipeline never rendered against.
- Add the row in that same MR, labelled `main` until the tag exists, then
  relabel it when the release is cut. The release notes are generated from
  commit subjects and carry no pin, so this table is the only place the pair is
  written down.
- **Other pairs are untested, not unsupported.** A cluster on an older template
  release answering a newer `lib_ref` is a combination nothing here exercised; the
  library's own [VERSIONING.md](https://git.ericsweiss.com/eric/weisssrv-lib/-/blob/main/docs/VERSIONING.md)
  is what says whether that bump is allowed to break it.

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
`render-validate` — both fixtures, the real toolchain — went green.

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
them `check-guest-endpoint-parity.py`, `check-tenant-wiring.py` and
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

### Pending at the next library bump

Each entry is deleted by the pin bump that satisfies it.

- `template/scripts/check-scrape-wiring.py`: the library's port-granularity
  companion to `check-scrape-netpol.py`. It reads one namespace's directory, so
  a generated cluster needs the corpus-shaped arm first; vendor it once the
  library ships that, alongside `tests/test_check_scrape_wiring.py`.
- Three gates under `template/scripts/` are registered as declared `forked:`
  entries rather than copies, so `reconciled_sha256` forces a review when the
  library side moves. Adopting the library file means, for each, rewriting its
  call sites and its shipped test suite:
  - `check-flux-version-pin.py` takes `--root` and `--components`, where the
    library's takes `--repo-root`, `--ci-file`, `--versions-configmap`,
    `--gotk-glob`, `--components` and `--runbook`. The flags are passed from
    `template/.pre-commit-config.yaml`, `template/.gitlab-ci.yml.jinja` and
    `template/taskfiles/lint.yml.jinja`.
  - `check-secret-rotation-coverage.py` hard-codes `DOC` and
    `DECLARED_MANUAL`, where the library's requires `--doc` and accepts
    `--declared-manual`. Adapting means passing `--doc docs/RUNBOOKS.md` from
    `template/tests/test_check_secret_rotation_coverage.py` and the same three
    callers.
  - `flux-child-kustomizations.py` prints a bare `spec.path` for `--paths`,
    globs `*.yaml` only, and exposes `child_kustomizations()`. The library's
    excludes `flux-system`, globs `*.yml` too, tolerates an unreadable file,
    prints `name<TAB>spec.path`, exits 1 on a path-less Kustomization unless
    `--allow-missing-paths`, and exposes `child_kustomization_paths()` /
    `_order()`. Adopting it means reading the tab form in
    `template/scripts/deploy-verify.sh` (`while IFS=$'\t' read -r _KSNAME
    SRCPATH`), piping `KS_PATHS` through `cut -f2` in
    `template/taskfiles/flux.yml.jinja`, and rewriting
    `template/tests/test_flux_child_kustomizations.py` against
    `child_kustomization_paths` / `_order` and the tab output.
- `check-netpol-except-parity.py` leaves a `${cluster_*}` ipBlock CIDR
  unevaluated, and this template spells 15 of them across 8 manifests, so the
  fence arm examines no rule whose only peer is one. Closing it needs the
  library gate to read a manifest corpus on stdin, the way the other corpus
  gates do. `template/scripts/flux-corpus-gates.sh` then runs it over the
  substituted corpus, where every placeholder has a value.

## Related

- [RUNBOOKS.md](RUNBOOKS.md) § Updating the template — the operator-side procedure
- [CI.md](CI.md) — what the generated pipeline runs
- [weisssrv-lib VERSIONING.md](https://git.ericsweiss.com/eric/weisssrv-lib/-/blob/main/docs/VERSIONING.md)
  — the library's own tags, which `lib_ref` selects
