# CI for this template

Two pipelines are involved and they are easy to confuse:

| Pipeline | File | Runs on | Gates |
|---|---|---|---|
| **This template's** | `.gitlab-ci.yml` | changes to the template | lint, the copier schema, a full render that is then validated with the real toolchain, and — on `main` only — the release tag |
| **A generated cluster's** | `template/.gitlab-ci.yml.jinja` | the repository copier produces | lint, flux-lint, secret detection, AI review, version bumps, deploys |

The generated pipeline is documented from the operator's side in the rendered
repository at `docs/ci-pipeline.md`.

## The gate that matters: `validate-rendered-cluster`

Structural tests can only prove that files exist and match each other. The
`validate-rendered-cluster` job proves the output is *usable*. It renders the template
**twice** — once from `tests/answers-weisssrv-shaped.yml` and once from
`tests/answers-unlike.yml` — and runs the validator over each, because a value
hard-coded from the reference cluster renders byte-identically to a correct
substitution in the shaped fixture and is only visible in a second, unlike one.

`tests/validate_render.py` runs these checks over a render:

| Check | What it proves | Needs |
|---|---|---|
| `yamllint` | the tree passes the generated repo's own `lint/yamllint-relaxed.yml` | `yamllint` |
| `shellcheck` | the rendered `scripts/*.sh` and `terraform/*/*.sh` pass at the severity and exclusions the generated pipeline uses. `scripts/collect-state.sh` is Jinja in the template, so a render is the only place it exists as shell | `shellcheck` |
| `terraform` | `terraform fmt -check -recursive` over the rendered `terraform/`, plus the tailnet policy still parses. Most of those files are Jinja templates, so this is what catches a template bug that lands as invalid HCL | `terraform` |
| `flux` | for every Kustomization under `kubernetes/clusters/<name>/`: `kustomize build` the target path, assert every `${placeholder}` is a key of one of the two postBuild ConfigMaps, substitute, `kubeconform`. Mirrors `ci/validate/flux-lint.yml`, through the generated repo's own `scripts/flux-env.sh`. A path that builds to nothing, and a kind kubeconform validated against no schema outside `validate_render.EXPECTED_SKIPPED`, both fail: either would otherwise pass as a validated render | `kustomize`, `kubeconform` |
| `cluster-gates` | the invariant gates the generated pipeline wires actually pass on the manifests the template ships | `kustomize` |
| `flux-lint` | `task flux:lint` — the generated cluster's own first gate command — exits 0 on the fresh render. It is the only arm here that runs the corpus wrapper's HelmRelease-values validation, so a VPA cap above a chart-rendered container limit fails the template instead of the new cluster | `task`, `helm`, and everything `flux` and `cluster-gates` need |
| `ci-policy` | the generated pipeline pins a `default: image:`, sets `default: interruptible: true` with the `workflow.auto_cancel` split (`interruptible` globally, `none` on `main`), and leaves every deploy/gate/plan job uninterruptible. All three are defaults a job inherits by saying nothing, so no rendered job fails when they go missing | — |
| `include-contract` | every library `include:` in the rendered pipeline and in this repository's own resolves against the checkout: no undeclared `inputs:` key, every default-less input passed, and each job's resolved `stage:` declared. All three fail pipeline creation, which is otherwise first seen on the ref bump | `--lib-path` |
| `inventory-addresses` | cluster-config declares the LAN CIDR and all three VIPs, so the render's own address invariants assert instead of skipping | — |
| `version-coverage` | every pin in the rendered vars file has a `scripts/version-registry.py` entry. Both are template output, so an entry added to one `.jinja` and not the other renders a cluster whose weekly bump bot silently never reports that pin | — |
| `versions-configmap` | the rendered `cluster-versions` ConfigMap matches the rendered vars file — `flux` substitutes FROM the ConfigMap, so a stale value is otherwise a valid render | — |
| `vendored` | every script in the render and in this repository is byte-identical to the library copy it was vendored from | `--lib-path` |
| `rendered-vendored` | a generated cluster can run that same proof for itself, from its own manifest | `--lib-path` |
| `role-opt-ins` | no playbook invokes a `<role>_enabled: false` role without the inventory setting the flag — a role that runs and does nothing, successfully | `--lib-path` |
| `role-inputs` | every input an invoked role *asserts* and has no **usable** default for is assigned in `inventories/prod`. "Usable" is decided by rendering the default against the inventory, not by reading it: `proxmox_lxc_nameserver` defaults to `{{ dns_servers \| default([]) \| join(' ') }}`, which is non-empty as text and empty as a value the moment `dns_servers` is unset | `--lib-path` |
| `terraform-validate` | `terraform validate` per module against the library checkout, with each `git::…?ref=` source rewritten to it — otherwise the RELEASED module is what validates | `--lib-path`, `terraform` |
| `ansible` | `ansible-playbook --syntax-check` on every rendered playbook, with `weisssrv.infra` resolved from the library | `ansible-playbook` |

**`include-contract`, `vendored`, `rendered-vendored`, `role-opt-ins`, `role-inputs` and
`terraform-validate` are silently skipped without `--lib-path`** — they read the library's roles,
scripts and Terraform modules, so there is nothing to compare against. The validator prints `skipped (no --lib-path)` for each. A
local run without it is therefore weaker than CI, which always passes one.

A template change that produces a cluster which cannot reconcile fails here
instead of in someone's homelab.

### cluster-gates

The render suite proves the generated pipeline wires these gates. This check
runs them, over the manifests the template ships. The rendered
`scripts/flux-corpus-gates.sh` runs the corpus arms, the same wrapper
`task flux:lint` calls: `check-hpa-vpa-invariant`,
`check-netpol-except-parity`, `check-scrape-netpol`,
`check-default-deny-coverage`, `check-secretstore-scope`,
`check-pvc-storageclass` and `check-ephemeral-storage-cap` over the rendered
corpus, plus `check-backup-artifact-apps` over the inventory and the alert
rules. Its HelmRelease-values arm needs the network, so the validator calls the
wrapper without a versions ConfigMap and that arm is skipped. The validator then
runs `check-netpol-except-parity` again over `kubernetes/` on disk, where the
`${cluster_*}` CIDRs are unsubstituted and the flux-system tree is in scope, and
`lint-prometheus-config.sh` over the alert rules and the Alertmanager config.
Without this check, a generated cluster's first pipeline can be red on
manifests nobody edited, and the template change that caused it went green.

The alert rules and the Alertmanager config live inside a HelmRelease's
`.spec.values`, where `kubeconform` cannot reach them, so that last arm needs
`promtool` and `amtool`. `validate-rendered-cluster` fetches both, pinned and
sha256-verified like `kubeconform`, `kustomize` and `terraform`; a local run
without them skips that arm and says so.

A clean `check-scrape-netpol` proves the namespace admits observability, not
that the scrape lands: the allow policy's own `podSelector` and port go
unchecked. A `TargetDown` that survives a green pipeline is the signal to read
the live policy's targeting.

### flux-lint

`cluster-gates` runs the corpus wrapper without a versions ConfigMap, which the
wrapper reads as "skip the HelmRelease-values validation". That arm is where a
VPA `maxAllowed.memory` meets the limit the chart actually renders, and it is
the arm that failed on a fresh render while every check above was green. This
check closes that by running `task flux:lint` itself — the first command the
generated cluster's own README hands the operator — in the render.

It needs `task` and `helm` on PATH and neither is fetched by
`scripts/ci-fetch-tools.py`, so `validate-rendered-cluster` installs both in its
`before_script`: `helm` at the version and sha256 the library's
`ci/validate/flux-lint.yml` pins, read out of the `--lib-path` checkout, so this
gate and every generated cluster's own pipeline render with the same binary.
There is no silent skip: a missing tool fails the check by name.

### inventory-addresses

Copier answers are validated one at a time, never against the address plan
`hosts.yml` composes them into, and a resolver's vmid is derived from its
answer (`100 + last octet`), so `upstream_dns_servers` landing in the `.31+`
server band duplicates both. The assertions themselves ship in the render's own
`tests/test_cluster_invariants.py`, which reads the rendered inventory rather
than the answers, so a hand re-address reaches the same gate. It skips when
cluster-config is silent, which is what this check refuses to allow.

### The vendored checks

`vendored` compares this repository and the render against the library at the
ref the `include:` block pins, and refuses to compare at all when those
includes and `copier.yml`'s `lib_ref` default disagree. It also runs the
library's vendored-copy engine (`scripts/check-vendored-copies.py`) against
[`scripts/vendored-manifest.yml`](../scripts/vendored-manifest.yml), which
names the copies no directory walk reaches: the canonical
`tests/test_check_lib_pins.py` suite, the secret-detection ruleset under
`.gitlab/`, and the lint profiles this repository deliberately forks. The
manifest is consumer-owned. The library publishes only an offer list
(`scripts/vendorable-paths.yml`) bounding what a manifest may name, so moving a
copy here is an edit to the manifest in the same commit, not a library release.

`rendered-vendored` runs that same engine over the render's own
[`scripts/vendored-manifest.yml`](../template/scripts/vendored-manifest.yml),
rooted at the render: every listed copy identical, every declared fork still
divergent and still reconciled, every rendered script with a library twin
named, and `tests/test_vendored_byte_identity.py` rendered to read it. The
first check proves the copies are current *here*; this one proves a generated
cluster can prove it for *itself*, which the library cannot do for a repository
generated after its release.

## Running the tests locally

```bash
python3 -m pytest tests -q                      # structure + copier schema
python3 tests/validate_render.py --lib-path ~/src/weisssrv-lib          # every check above
python3 tests/validate_render.py --lib-path ~/src/weisssrv-lib \
  --answers tests/answers-unlike.yml            # the contrast fixture
python3 tests/validate_render.py --lib-path ~/src/weisssrv-lib \
  --answers tests/answers-unlike.yml \
  --data vpn_tailscale=true --data gpu=none --data use_unifi=false   # mixed modules
python3 tests/render_cluster.py --out /tmp/x    # just render, and keep it
python3 tests/validate_render.py --render-dir /tmp/x --lib-path ~/src/weisssrv-lib
```

The render always happens from a **copy** of the working tree with `.git`
removed. Copier treats a git checkout as a VCS source and would otherwise
render the last commit, silently testing something other than the diff under
review.

Requirements: `copier>=9.15`, `pytest`, `pyyaml` for the pytest suite; plus
`yamllint`, `shellcheck`, `terraform`, `kustomize`, `kubeconform`,
`ansible-playbook`, `promtool`, `amtool`, `helm` and `task` for the validator. Any missing tool is reported by name. `--skip` takes any of
`yamllint,shellcheck,terraform,flux,cluster-gates,flux-lint,ci-policy,include-contract,inventory-addresses,version-coverage,versions-configmap,vendored,rendered-vendored,role-opt-ins,role-inputs,terraform-validate,ansible`
— the same names `validate_render.py --help` prints, and the same order the
table above lists them in. `test_ci_doc_lists_every_validator_check` holds the
three together, so a check added to the registry without a row here fails the suite;
an unknown `--skip` name is rejected rather than silently skipping nothing.

`--lib-path` points at a weisssrv-lib checkout — the directory that *contains*
`ansible_collections/`. Use it to exercise an unmerged library change, to avoid
the network, and — as the table above says — to run the library-reading
checks at all. Without it the collection is installed from the git ref in the render's
`requirements.yml`. The library's galaxy dependencies (`ansible.posix`,
`community.general`) are installed either way, with the operator's own
`~/.ansible/collections` as the offline fallback.

The checkout must be at the `lib_ref` the template pins, or every
library-reading check above has another library tree as its subject. The
validator fails on a mismatch and names the tags it found. Add
`--allow-ref-mismatch` to run against an unreleased checkout: the mismatch is
then printed as a warning and the checks still run.

## Pipeline policy

MR refs set `auto_cancel.on_new_commit: interruptible`, so a new push cancels
the previous pipeline's interruptible jobs instead of letting `validate-rendered-cluster`,
the heaviest job, pile up on the quota-capped shared runner. GitLab's project
default, `conservative`, stops cancelling as soon as any non-interruptible job
has started, which leaves them running.

`main` overrides to `none`. A merged pipeline always runs to completion, because
`release` cuts the tag every generated cluster's `copier update` resolves to,
and a cancelled one leaves that tag uncut behind a green pipeline.

`validate-rendered-cluster` retries `runner_system_failure` and `scheduler_failure`: it
requests the largest limits in the pipeline and is the first job refused at
pod-creation time when several pipelines overlap, which arrives as a system
failure rather than a test failure.

## The two answer fixtures

Both are complete answer sets, and every question must be answered in **both** —
except `lib_ref`, which neither answers so that both inherit `copier.yml`'s
default (`test_lib_ref_is_inherited_by_the_validated_fixture` asserts exactly
that). `tests/test_copier_config.py` fails if any other question is added to
`copier.yml` without an entry in the shaped fixture, and the render suite's
`test_the_two_fixtures_answer_differently` asserts that the two files answer the
same question set — so a new question that reaches only one of them fails there
instead.

Neither fixture may answer a computed (`when: false`) entry. Copier takes a
`--data-file` value over a skipped question's default, so an entry there
silently replaces the expression the render is meant to prove.
`test_no_fixture_answers_a_computed_question` fails that.

| Fixture | Shape | Reaches |
|---|---|---|
| `answers-weisssrv-shaped.yml` | the reference cluster's shape with placeholder identity: flat `/24`, split-horizon domains, three VIPs, smallest roster | every optional module **on** (`vpn_tailscale`, `gpu: nvidia`, `use_unifi`), multi-resolver, semantic-release off |
| `answers-unlike.yml` | deliberately unlike it in every answer that can differ: other LAN, other domains, other names, other vault, other runner tag, largest roster | every optional module **off**, a single resolver, a third exporter job, semantic-release on |

They are a pytest *parameter* (the `cluster` fixture), so most assertions run
against both renders for the price of one render each.

`test_render_b_carries_no_fixture_a_values` diffs the shaped fixture's answers
against the unlike renders: any answer from the shaped fixture appearing there
is a hard-coded literal, not a substitution. It runs over the `cluster_b`
fixture's renders: `answers-unlike.yml` as shipped, the same answers with the
optional modules forced on, and the same answers with one of them on. Without
the modules-on render, every file those answers gate would be outside the scan,
because the only render that carries them is the one whose values are the
needles. The mixed render takes the arms a conditional coupling two modules has,
which neither an all-on nor an all-off render reaches; `validate-rendered-cluster` puts
the same set through the toolchain with `--data`, and
`test_ci_validates_the_same_mixed_module_set_the_suite_renders` holds the two
together.

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

`include:` is resolved before job variables exist, so each entry repeats the tag
as a literal rather than reading `variables.WEISSSRV_LIB_REF`. The ref
`validate-rendered-cluster` actually clones is `copier.yml`'s `lib_ref` default, so the
gate, the render and the generated `requirements.yml` cannot disagree.

`validate-rendered-cluster` clones the library with `CI_JOB_TOKEN`, so the job does not
depend on anonymous access — the library project must list this project on its
CI/CD job-token allowlist if it is not public. It also sets `USER` and
`LOGNAME`: the runner pod's uid has no passwd entry, and ansible resolves the
current user from them, so every `--syntax-check` would die before parsing a
playbook.

## The release stage

`release` is the LAST stage of this repository's own pipeline, and the
`semantic-release` job sets no `needs:` — stage ordering is what gates a tag on
every job above it, `validate-rendered-cluster` included. Merging to `main` reads the
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
repository is normally released by hand. `scripts/semantic-release.py` ships in
every generated cluster; the answer only adds the stage that runs it.
`answers-unlike.yml` turns it on, so the rendered wiring is exercised on every
run.

## Deferred by design

Gaps against the reference cluster this repository deliberately does not close.
Each is a live decision, not a backlog entry — if you reopen one, change this
list in the same MR.

- **Deploy-job `rules:` are written out per job.** The reference cluster factors
  the shared preamble into a hidden `.skip-schedule-web` fragment; the generated
  pipeline repeats it. Repetition is what makes each job's trigger set readable
  where the job is, and a `!reference` that silently changes every deploy's
  trigger set is the more expensive mistake in a repository an operator forks
  and edits.
- **The template's `task lint` is a subset of the reference cluster's.** The
  gates it omits — Prometheus/Alertmanager config lint, the cluster-literal
  check, the flux/kubectl/busybox pin checks, the CI-policy and tailnet-policy
  checks — each need a tool or a policy file a freshly generated cluster does
  not have yet. They are adopted per cluster, not shipped empty.
- **Not every library script is vendored.** `template/scripts/` carries what a
  generated cluster's tasks and CI jobs actually invoke; the library's
  role-development and matrix-generation tooling stays out. `scripts/README.md`
  in the render is the human inventory, and the render's own
  `scripts/vendored-manifest.yml` is the machine-checked one — the copies that
  do ship are gated in the generated repository, by it, not from here.
- **Agent guidance ships; agent settings do not.** A generated cluster gets
  `AGENTS.md`, `CLAUDE.md`, the `.claude/` skill and the `.cursor/` rule — all
  prose — but no `.claude/settings.json`. Permissions and hooks are per operator
  and per machine, and a shipped one is a permission grant nobody reviewed.
- **The `{{ {}['unimplemented'] | mandatory('...') }}` else-branches are
  unreachable.** Every backend selector is an enum whose `choices` list only the
  implemented values, and `test_unimplemented_backend_choices_fail_at_copy_time`
  holds that list to what the render can actually produce, so an unimplemented
  backend is refused before a single file is written. The idiom stays as
  defence-in-depth for the case a choice is added without its branch — accepting
  that if it ever did fire, the operator gets a Jinja `FilterArgumentError`
  traceback carrying the message rather than copier's clean validation error.
- **Chart and image pins ship at the version the template was last released
  with**, not at the latest upstream. Currency is the version-bump cycle's job
  ([VERSIONING.md](VERSIONING.md)): the generated cluster's own
  `version-bump-bot` picks up from wherever it starts, so a pin that is one
  release behind at generation costs one bot run, not a re-release here.
