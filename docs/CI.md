# CI for this template

Two pipelines are involved and they are easy to confuse:

| Pipeline | File | Runs on | Gates |
|---|---|---|---|
| **This template's** | `.gitlab-ci.yml` | changes to the template | lint, the copier schema, and a full render that is then validated with the real toolchain |
| **A generated cluster's** | `template/.gitlab-ci.yml.jinja` | the repository copier produces | lint, flux-lint, secret detection, AI review, version bumps, deploys |

The generated pipeline is documented from the operator's side in the rendered
repository at `docs/ci-pipeline.md`.

## The gate that matters: `render-validate`

Structural tests can only prove that files exist and match each other. The
`render-validate` job proves the output is *usable*: it renders the template
with `tests/answers-weisssrv-shaped.yml` and then runs, over the result,

1. `yamllint` across `ansible/`, `kubernetes/`, `terraform/` and `.gitlab-ci.yml`;
2. the **Flux render loop** — for every Kustomization under
   `kubernetes/clusters/<name>/`: `kustomize build` the target path, check that
   every `${placeholder}` in the output is a key of one of the two postBuild
   ConfigMaps, substitute, then `kubeconform`. This mirrors what
   `ci/validate/flux-lint.yml` does inside a real cluster repo, including using
   the generated repo's own `scripts/flux-render.sh`;
3. `ansible-playbook --syntax-check` on every rendered playbook, with
   `weisssrv.infra` resolved from a checkout of the library at the ref the
   answer fixture pins.

A template change that produces a cluster which cannot reconcile fails here
instead of in someone's homelab.

## Running the tests locally

```bash
python3 -m pytest tests -q                      # structure + copier schema
python3 tests/validate_render.py                # + the real toolchain
python3 tests/validate_render.py --lib-path ~/src/weisssrv-lib
python3 tests/render_cluster.py --out /tmp/x    # just render, and keep it
```

The render always happens from a **copy** of the working tree with `.git`
removed. Copier treats a git checkout as a VCS source and would otherwise
render the last commit, silently testing something other than the diff under
review.

Requirements: `copier>=9`, `pytest`, `pyyaml` for the pytest suite; plus
`yamllint`, `kustomize`, `kubeconform` and `ansible-playbook` for the
validator. Any missing tool is reported by name; `--skip yamllint,flux,ansible`
narrows the run.

`--lib-path` points at a weisssrv-lib checkout — the directory that *contains*
`ansible_collections/`. Use it to exercise an unmerged library change, or
simply to avoid the network. Without it the collection is installed from the
git ref in the render's `requirements.yml`. The library's galaxy dependencies
(`ansible.posix`, `community.general`) are installed either way, with the
operator's own `~/.ansible/collections` as the offline fallback.

## The answer fixture

`tests/answers-weisssrv-shaped.yml` has the same shape as the reference cluster
— flat `/24`, split-horizon domains, three VIPs — with placeholder identity and
both optional modules (`vpn_tailscale`, `gpu`) turned **on**, because the
conditional branches are the ones a defaults-only render never reaches.

`tests/test_copier_config.py` fails if a question is added to `copier.yml`
without an entry here: an unanswered question falls back to its default and
stops being exercised.

## Adding a check

Structural assertions that need only PyYAML belong in `tests/test_render.py`.
Anything requiring a tool belongs in `tests/validate_render.py`, as a
`check_*` function registered in `main()`.

Invariants about a **cluster** rather than about the template belong in
`template/tests/test_cluster_invariants.py` — they ship to every generated
repository, run in its own `python-tests` job, and are executed here too
(`test_generated_repo_passes_its_own_invariants`), so a cluster that would fail
its own gate cannot be generated.

## Library pin

Every `include:` in both pipelines points at `eric/weisssrv-lib`. This
repository's own includes are on the `feat/v0.2.0-building-blocks` branch and
must move to `v0.2.0` when the library cuts the tag; the generated pipeline
pins whatever `lib_ref` the operator answered.

`render-validate` clones the library with `CI_JOB_TOKEN`, so the job does not
depend on anonymous access — the library project must list this project on its
CI/CD job-token allowlist if it is not public.
