# CI for this template

Two pipelines are involved and they are easy to confuse:

| Pipeline | File | Runs on | Gates |
|---|---|---|---|
| **This template's** | `.gitlab-ci.yml` | changes to the template | lint, the copier schema, a full render that is then validated with the real toolchain, and — on `main` only — the release tag |
| **A generated cluster's** | `template/.gitlab-ci.yml.jinja` | the repository copier produces | lint, flux-lint, secret detection, AI review, version bumps, deploys |

The generated pipeline is documented from the operator's side in the rendered
repository at `docs/ci-pipeline.md`.

## The gate that matters: `render-validate`

Structural tests can only prove that files exist and match each other. The
`render-validate` job proves the output is *usable*. It renders the template
**twice** — once from `tests/answers-weisssrv-shaped.yml` and once from
`tests/answers-unlike.yml` — and runs the validator over each, because a value
hard-coded from the reference cluster renders byte-identically to a correct
substitution in the shaped fixture and is only visible in a second, unlike one.

`tests/validate_render.py` runs these checks over a render:

| Check | What it proves | Needs |
|---|---|---|
| `yamllint` | the tree passes the generated repo's own `.yamllint` | `yamllint` |
| `terraform` | `terraform fmt -check -recursive` over the rendered `terraform/`, plus the tailnet policy still parses. Five of those files are templates, so this is what catches a Jinja bug that lands as invalid HCL | `terraform` |
| `flux` | for every Kustomization under `kubernetes/clusters/<name>/`: `kustomize build` the target path, assert every `${placeholder}` is a key of one of the two postBuild ConfigMaps, substitute, `kubeconform`. Mirrors `ci/validate/flux-lint.yml`, through the generated repo's own `scripts/flux-env.sh` | `kustomize`, `kubeconform` |
| `cluster-gates` | the five invariant gates the generated pipeline wires — `check-scrape-netpol`, `check-secretstore-scope`, `check-pvc-storageclass` and `check-hpa-vpa-invariant` over the whole rendered corpus, `check-netpol-except-parity` over `kubernetes/` on disk — actually PASS on the manifests the template ships. The render suite proves they are wired; without this, a generated cluster's first pipeline can be red on manifests nobody edited, and the template change that caused it went green | `kustomize` |
| `ci-policy` | the generated pipeline pins a `default: image:`, sets `default: interruptible: true` with the `workflow.auto_cancel` split (`interruptible` globally, `none` on `main`), and leaves every deploy/gate/plan job uninterruptible. All three are defaults a job inherits by saying nothing, so no rendered job fails when they go missing | — |
| `inventory-addresses` | no two hosts in `inventories/prod/hosts.yml` share an `ansible_host` or a `vmid`, and every address sits inside `cluster_lan_cidr`. Copier answers are validated one at a time, never against the address plan `hosts.yml` composes them into — and a resolver's vmid is derived from its answer (`100 + last octet`), so `upstream_dns_servers` landing in the `.31+` server band duplicates both. Reads the rendered inventory rather than the answer, so a hand re-address reaches the same gate | — |
| `version-coverage` | every pin in the rendered vars file has a `scripts/version-registry.py` entry. Both are template output, so an entry added to one `.jinja` and not the other renders a cluster whose weekly bump bot silently never reports that pin | — |
| `versions-configmap` | the rendered `cluster-versions` ConfigMap matches the rendered vars file — `flux` substitutes FROM the ConfigMap, so a stale value is otherwise a valid render | — |
| `vendored` | every script in the render's `scripts/` **and** in this repository's own `scripts/` is byte-identical to the library's copy, plus the library's own registry gate (`scripts/check-vendored-copies.py` against `scripts/vendored-paths.yml`) over this repository. The second half is what holds `scripts/semantic-release.py` — the file that cuts the tag a generated cluster's `copier update` resolves to — to the library at the ref the includes pin, and it refuses to compare at all if those includes and `copier.yml`'s `lib_ref` default disagree. The registry arm adds the copies no directory walk reaches: the canonical `tests/test_check_lib_pins.py` suite, and the lint profiles this repository deliberately forks | `--lib-path` |
| `role-opt-ins` | no playbook invokes a `<role>_enabled: false` role without the inventory setting the flag — a role that runs and does nothing, successfully | `--lib-path` |
| `role-inputs` | every input an invoked role *asserts* and has no **usable** default for is assigned in `inventories/prod` — the shape that took out `proxmox_lxc_gateway`. "Usable" is decided by rendering the default against the inventory, not by reading it: `proxmox_lxc_nameserver` defaults to `{{ dns_servers \| default([]) \| join(' ') }}`, which is non-empty as text and empty as a value the moment `dns_servers` is unset | `--lib-path` |
| `terraform-validate` | `terraform validate` per module against the library checkout, with each `git::…?ref=` source rewritten to it — otherwise the RELEASED module is what validates | `--lib-path`, `terraform` |
| `ansible` | `ansible-playbook --syntax-check` on every rendered playbook, with `weisssrv.infra` resolved from the library | `ansible-playbook` |

**`vendored`, `role-opt-ins`, `role-inputs` and `terraform-validate` are
silently skipped without `--lib-path`** — they read the library's roles,
scripts and Terraform modules, so there is nothing to compare against. The validator prints `skipped (no --lib-path)` for each. A
local run without it is therefore weaker than CI, which always passes one.

A template change that produces a cluster which cannot reconcile fails here
instead of in someone's homelab.

## Running the tests locally

```bash
python3 -m pytest tests -q                      # structure + copier schema
python3 tests/validate_render.py --lib-path ~/src/weisssrv-lib          # every check above
python3 tests/validate_render.py --lib-path ~/src/weisssrv-lib \
  --answers tests/answers-unlike.yml            # the contrast fixture
python3 tests/render_cluster.py --out /tmp/x    # just render, and keep it
python3 tests/validate_render.py --render-dir /tmp/x --lib-path ~/src/weisssrv-lib
```

The render always happens from a **copy** of the working tree with `.git`
removed. Copier treats a git checkout as a VCS source and would otherwise
render the last commit, silently testing something other than the diff under
review.

Requirements: `copier>=9`, `pytest`, `pyyaml` for the pytest suite; plus
`yamllint`, `terraform`, `kustomize`, `kubeconform` and `ansible-playbook` for
the validator. Any missing tool is reported by name. `--skip` takes any of
`yamllint,terraform,flux,cluster-gates,ci-policy,inventory-addresses,version-coverage,versions-configmap,vendored,role-opt-ins,role-inputs,terraform-validate,ansible`
— the same names `validate_render.py --help` prints, and the same order the
table above lists them in. `test_ci_doc_lists_every_validator_check` holds the
three together, so a check added to the registry without a row here fails the suite;
an unknown `--skip` name is rejected rather than silently skipping nothing.

`--lib-path` points at a weisssrv-lib checkout — the directory that *contains*
`ansible_collections/`. Use it to exercise an unmerged library change, to avoid
the network, and — as the table above says — to run the four library-reading
checks at all. Without it the collection is installed from the git ref in the render's
`requirements.yml`. The library's galaxy dependencies (`ansible.posix`,
`community.general`) are installed either way, with the operator's own
`~/.ansible/collections` as the offline fallback.

## The two answer fixtures

Both are complete answer sets, and every question must be answered in **both** —
except `lib_ref`, which neither answers so that both inherit `copier.yml`'s
default (`test_lib_ref_is_inherited_by_the_validated_fixture` asserts exactly
that). `tests/test_copier_config.py` fails if any other question is added to
`copier.yml` without an entry in the shaped fixture, and the render suite's
`test_the_two_fixtures_answer_differently` asserts that the two files answer the
same question set — so a new question that reaches only one of them fails there
instead.

| Fixture | Shape | Reaches |
|---|---|---|
| `answers-weisssrv-shaped.yml` | the reference cluster's shape with placeholder identity: flat `/24`, split-horizon domains, three VIPs, smallest roster | both optional modules **on** (`vpn_tailscale`, `gpu: nvidia`), multi-resolver, semantic-release off |
| `answers-unlike.yml` | deliberately unlike it in every answer that can differ: other LAN, other domains, other names, other vault, other runner tag, largest roster | both optional modules **off**, a single resolver, a third exporter job, semantic-release on |

They are a pytest *parameter* (the `cluster` fixture), so most assertions run
against both renders for the price of one render each. `answers-unlike.yml` is
also what `test_render_b_carries_no_fixture_a_values` diffs against: any answer
from the shaped fixture appearing in the unlike render is a hard-coded literal,
not a substitution.

## Adding a check

Structural assertions that need only PyYAML belong in `tests/test_render.py`.
Anything requiring a tool belongs in `tests/validate_render.py`, as a
`check_*` function listed in `RENDER_CHECKS` (or `LIB_CHECKS`, if it needs the
library checkout). Add its row to the table above in the same change —
`test_ci_doc_lists_every_validator_check` compares the two.

Invariants about a **cluster** rather than about the template belong in
`template/tests/test_cluster_invariants.py` — they ship to every generated
repository, run in its own `python-tests` job, and are executed here too
(`test_generated_repo_passes_its_own_invariants`), so a cluster that would fail
its own gate cannot be generated.

## Library pin

Every `include:` in both pipelines points at `eric/weisssrv-lib`. This
repository's own includes are pinned to the release tag `lib_ref` resolves to
(its default lives in `copier.yml`); the generated pipeline pins whatever
`lib_ref` the operator answered.
Never pin a branch: it moves under every consumer at once, and it disappears
when it merges — which takes every include, module source and collection install
with it. `scripts/check-lib-pins.py` fails the pipeline on a branch pin or on an
include that drifts from `variables.WEISSSRV_LIB_REF`.

`render-validate` clones the library with `CI_JOB_TOKEN`, so the job does not
depend on anonymous access — the library project must list this project on its
CI/CD job-token allowlist if it is not public.

## The release stage

`release` is the LAST stage of this repository's own pipeline, and the
`semantic-release` job sets no `needs:` — stage ordering is what gates a tag on
every job above it, `render-validate` included. Merging to `main` reads the
conventional commits since the last tag, cuts `vMAJOR.MINOR.PATCH`, and creates
the GitLab Release with generated notes in one Releases-API call; nothing
releasable means no tag and a green pipeline.

That tag is not decoration: `copier update` resolves to the **latest tag** of
the template and falls back to the branch tip only when there is none. What a
given bump is allowed to change — and what `copier update` does across one — is
[VERSIONING.md](VERSIONING.md).

The job runs `scripts/semantic-release.py`, vendored from the library and held
byte-identical by the `vendored` check above. It needs no credential beyond
`CI_JOB_TOKEN`; if protected tags restrict who may create `v*`, pass a PAT
reference through the template's `release_token` / `token_header` inputs.

The generated cluster's pipeline gets the same stage only when the operator
answers `enable_semantic_release: true` — off by default, because a cluster
repository is normally released by hand. `answers-unlike.yml` turns it on, so
the rendered wiring is exercised on every run.
