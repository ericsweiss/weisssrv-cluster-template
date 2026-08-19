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
| a backend seam (git / secrets / storage / dns) | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) § Backend seams, and weisssrv-lib `docs/EXTENSIBILITY.md` |
| the library pin | [docs/VERSIONING.md](docs/VERSIONING.md) — the default, the fixture, this repo's `include:` refs and the validated-pair table move in one MR |
| a vendored script under `scripts/` or `template/scripts/` | [docs/CI.md](docs/CI.md) — these are byte-identical copies of weisssrv-lib's; fix them THERE, tag, re-vendor. TWO manifests register them: `scripts/vendored-manifest.yml` for this repository's copies, `template/scripts/vendored-manifest.yml` for the ones every generated cluster carries — adding or moving a copy edits both |
| CI | [docs/CI.md](docs/CI.md) |
| operator prose | [docs/PRE-SETUP.md](docs/PRE-SETUP.md), [docs/SETUP.md](docs/SETUP.md), [docs/RUNBOOKS.md](docs/RUNBOOKS.md) |

## Gates to run before proposing a change

```bash
python3 -m pytest tests -q
python3 tests/validate_render.py --lib-path <weisssrv-lib checkout>
python3 tests/validate_render.py --answers tests/answers-unlike.yml --lib-path <…>
yamllint -c .yamllint copier.yml tests/ .gitlab-ci.yml
```

Both fixtures, always: the weisssrv-shaped one renders identically whether or not
a reference-cluster value leaked, so only the contrast render proves the
substitution happened.

## House rules

- Never push to `main`; every change is a branch and a merge request.
- No secrets in the tree — `op://` references and item titles only.
- No AI or assistant attribution anywhere: commits, MRs, code or docs.
- Comments state the current rule and why it holds. No history, no narration, no
  commented-out manifests — ship an alternate as a real file excluded from the
  kustomization instead. The one exception is a README arguing a *security*
  rule, where "this actually happened" is the argument: the
  `gitlab-runner-privileged` README keeps the instance-runner incident in one
  sentence for that reason, and it is the only place that carries it — pages
  that merely restate the rule point there. It stays in the prose, never in a
  manifest.
