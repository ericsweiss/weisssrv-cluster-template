# scripts/

Two kinds of file live here: **vendored** copies of generic tooling from
weisssrv-lib, and **site data** those tools read.

Nothing in this directory is cluster-specific logic. If you find yourself
editing a vendored script, the change belongs upstream in the library — a local
edit is silently reverted the next time the copy is refreshed.

## Vendored from weisssrv-lib

Copied at the ref this repository was generated from. Refresh one by copying it
out of a `task lib:sync` checkout (`.weisssrv-lib/scripts/<name>`) and reviewing
the diff.

| Script | Used by |
|---|---|
| `flux-render.sh` | `task flux:lint`, the CI flux-lint job — extracts postBuild vars from a ConfigMap |
| `kubeconform-skipped.py` | the CI flux-lint job — tracks kinds no schema validated |
| `generate-versions-configmap.py` | `task flux:sync-versions` and its CI drift gate |
| `generate-hosts-env.py` | `task hosts:sync` and its CI drift gate |
| `check-versions.py`, `version-check-ci.py` | `task maintenance:check-versions`, the CI version report, the bump bot |
| `extract-prometheus-config.py`, `lint-prometheus-config.sh` | `task lint:prometheus-config` |
| `check-doc-links.py` | `task lint:doc-links`, the CI docs-link-check job |
| `check-taskfile.sh` | `task lint:taskfile-smoke` — catches dangling script references |
| `resolve-tool.sh` | the Taskfile, to find `ansible-lint` outside `PATH` |
| `find-reachable-host.sh`, `shell-lib.sh` | the Taskfile, to pick a live SSH entry point |
| `semantic-release.py` | the CI release job |
| `version-bump-mr.py` | the CI version-bump bot |

Everything a task or a CI job runs is here rather than pulled from a checkout at
run time: a CI job has only this repository, and a gate that depends on cloning
another repository fails for a reason that has nothing to do with the change
under review. `task lib:sync` clones the library at the pinned ref into
`.weisssrv-lib/` (gitignored) so a refresh is a copy plus a reviewed diff.

`flux-env.sh` is the one script written here rather than vendored: this cluster
substitutes from **two** ConfigMaps (`cluster-versions` for pins,
`cluster-config` for domains, VIPs and CIDRs) while the library's render helper
takes one file per call. It merges them behind the same
`export-versions` / `k8s-version` interface, so the local lint and the CI job
see identical variables — which is why the CI flux-lint job must point its
`flux_render_script` input at THIS file. Extra ConfigMaps can also come from
`$FLUX_EXTRA_CONFIGMAPS`.

## Site data

| File | Read by | Notes |
|---|---|---|
| `hosts.env` | the Taskfile (`dotenv:`), the verification scripts | **Generated.** `task hosts:sync` after every inventory edit; CI fails when it is stale |
| `hosts-env-map.yml` | `generate-hosts-env.py` | Which inventory group becomes which variable |
| `version-registry.py` | `check-versions.py` | Which upstreams to watch. Ships the platform set; add an entry per app you adopt |

`hosts.env` ships empty: the host roster is the one thing the template cannot
guess. Until you fill in `ansible/inventories/prod/hosts.yml` and run `task
hosts:sync`, any task that iterates over a host list stops with an explicit
"empty — run task hosts:sync" message rather than doing nothing quietly.
