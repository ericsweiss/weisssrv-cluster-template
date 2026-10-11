# AGENTS.md

Guidance for AI agents working in **weisssrv-cluster-template**. It points; it
does not restate. Read the file it points at before changing anything.

## What this repository is

A copier template that generates a complete Proxmox + ZFS + k3s GitOps
repository. `copier.yml` is the answer schema, `template/` is the payload,
`partials/` holds Jinja fragments the payload imports (never copied out), and
nothing here is deployed — the OUTPUT is.

Start with [README.md](README.md): "The four repositories" for how this sits
next to `weisssrv-lib`, `weisssrv-app-template` and a generated cluster, and
"Developing this template" for the conventions that constrain every edit.

## Before you change anything

| You are changing | Read first |
|---|---|
| a question, default or validator | [README.md](README.md) § The answers, and `tests/test_copier_config.py` — the schema is an API replayed on `copier update` |
| anything under `template/kubernetes/` | [template/kubernetes/README.md.jinja](template/kubernetes/README.md.jinja) — manifests must NOT interpolate answers; site values arrive through the `cluster-config` ConfigMap |
| CI runner `concurrent` or either ResourceQuota | [partials/ci-sizing.jinja](partials/ci-sizing.jinja) — the capacity model for BOTH tiers, imported by all four manifests; `tests/test_render.py` § CI runner sizing holds it to the reference cluster |
| a backend seam (secrets / dns / git-CI / vpn / sso / gpu / gateway / storage / ingress) | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) § Backend seams, and weisssrv-lib `docs/EXTENSIBILITY.md` |
| the library pin | [docs/VERSIONING.md](docs/VERSIONING.md) § Which library release the template is validated against and [docs/CI.md](docs/CI.md) § Library pin — `copier.yml`'s `lib_ref` default and this repo's `include:` refs move in one MR |
| a vendored script under `scripts/` or `template/scripts/` | [docs/CI.md](docs/CI.md) — these are byte-identical copies of weisssrv-lib's; fix them THERE, tag, re-vendor. TWO manifests register them: `scripts/vendored-manifest.yml` for this repository's copies, `template/scripts/vendored-manifest.yml` for the ones every generated cluster carries — adding or moving a copy edits both |
| CI | [docs/CI.md](docs/CI.md) |
| operator prose | [docs/PRE-SETUP.md](docs/PRE-SETUP.md), [docs/SETUP.md](docs/SETUP.md), [docs/RUNBOOKS.md](docs/RUNBOOKS.md) |

## Gates to run before proposing a change

The commands are in [README.md § Local gates](README.md#local-gates). Run that
whole set, including the ruff, shellcheck and link lines, and do not keep a
second copy here. It is the CI lint and test stages, and both answer fixtures
are part of it.

## House rules

- Never push to `main`; every change is a branch and a merge request.
- No secrets in the tree — `op://` references and item titles only.
- No AI or assistant attribution anywhere: commits, MRs, code or docs.
- Comments state the current rule and why it holds. No history, no narration, no
  commented-out manifests — ship an alternate as a real file excluded from the
  kustomization instead.
- A comment block in any file the template ships runs to three content lines.
  That covers `copier.yml`, `partials/` and everything under `template/`. A
  render gates the rendered tree with `task lint:comment-length`, and
  `tests/test_comment_length_sources.py` runs the same gate over `copier.yml`
  and `partials/`, which no render contains. A block that guards a trap
  causing an outage may open `CRITICAL:` and run to eight; that marker is for
  outages, not for emphasis or for a long file header. This repository's own
  `scripts/` and `tests/` sit outside that scope: the scripts are vendored
  copies, trimmed upstream, and the test suite keeps its proofs in long
  docstrings.

## Rules for generated content

- Gate a module on `git_backend` only when a different forge would leave it with
  no consumer at all. The `ci-jobs` PriorityClass is gated because only the
  runner modules name it; a module that merely defaults to a `git_host`-derived
  value, like `registry-cache`, is not — its upstream is an edit-here knob, not
  a forge dependency. `git_backend: github` is validator-blocked today, so every
  such gate is forward-compatible scaffolding.
- A manifest comment is at most three lines and describes the current state.
  Longer explanation goes to the nearest `README.md` or to
  `template/docs/RUNBOOKS.md.jinja`, and the manifest points at it by name.
- A new question is answered in BOTH `tests/answers-*.yml`, gated questions
  included. A fixture that leaves one out renders the default, so the seam the
  question exists for is never exercised.

## Parity with the reference cluster

`weisssrv` is the cluster this template was extracted from. Nothing pins the two
together, so the flow direction is a review-time rule per area:

- `Taskfile.yml`, `.gitlab-ci.yml`, `scripts/check-*.py` and the
  `kubernetes/clusters/<name>/` stage layout flow weisssrv -> template. A
  generic improvement landed only in weisssrv is a defect every generated
  cluster ships with.
- Library adoption flows template -> weisssrv. The template adopts a library
  `include:` or module first, and weisssrv follows.
- Site data never flows either way.

[README.md](README.md) § The four repositories is the map.
