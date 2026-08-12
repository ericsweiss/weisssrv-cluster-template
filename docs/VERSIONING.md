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
| **MAJOR** | A generated repo cannot take the update without hand work. A question renamed or removed (its recorded answer no longer binds and the operator is re-prompted, or the value is silently lost); a question added with **no** default (`copier update` cannot run non-interactively); a validator tightened so a previously accepted answer is now refused; a rendered path moved, renamed or deleted; a change that makes a rendered file's *content* incompatible with state the cluster already holds (a renamed Flux Kustomization, a changed PV/zvol mount path, a renamed ConfigMap key that live manifests substitute). |
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
  Terraform module `?ref=`, the Taskfile's `LIB_REF` — is enumerated per
  consumer in the library's
  [docs/CONSUMERS.yml](https://git.ericsweiss.com/eric/weisssrv-lib/-/blob/main/docs/CONSUMERS.yml).

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

| Template release | Rendered and validated against |
|---|---|
| `v0.1.0` | weisssrv-lib `v0.2.0` |
| `v0.2.0` | weisssrv-lib `v0.5.2` |
| `main` (unreleased) | weisssrv-lib `v0.6.2` |

Rules that keep the table meaningful:

- The `lib_ref` **default** in `copier.yml`, the fixture's `lib_ref`, and this
  repository's own `include:` refs move together, in one MR. They are compared
  by the test suite, so a partial bump fails rather than shipping a default the
  pipeline never rendered against.
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

`scripts/semantic-release.py` is **vendored** from weisssrv-lib and must stay
byte-identical to the library's copy at the ref `.gitlab-ci.yml` pins;
`tests/validate_render.py`'s `vendored` check enforces that for this
repository's copy and for the one under `template/scripts/`. Re-copy it in the
same MR that bumps the library ref.

## Related

- [RUNBOOKS.md](RUNBOOKS.md) § Updating the template — the operator-side procedure
- [CI.md](CI.md) — what the generated pipeline runs
- [weisssrv-lib VERSIONING.md](https://git.ericsweiss.com/eric/weisssrv-lib/-/blob/main/docs/VERSIONING.md)
  — the library's own tags, which `lib_ref` selects
