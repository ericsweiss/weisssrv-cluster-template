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
| `flux-render.sh` | `flux-env.sh` — extracts postBuild vars from one ConfigMap |
| `flux-env.sh` | `task flux:lint` + the CI flux-lint job (`flux_render_script`) — merges the cluster's **two** substitution ConfigMaps behind one interface (see below) |
| `kubeconform-skipped.py` | the CI flux-lint job — tracks kinds no schema validated |
| `check-hpa-vpa-invariant.py` | `task flux:lint` + the CI flux-lint job (`extra_validation`) — no HPA and a CPU-controlling VPA on one workload |
| `check-scrape-netpol.py` | `task flux:lint` + the CI flux-lint job (`extra_validation`) — every scraped namespace admits Prometheus through its NetworkPolicies |
| `check-secretstore-scope.py` | `task flux:lint` + the CI flux-lint job (`extra_validation`) — every ClusterSecretStore declares `conditions` and admits its consumers |
| `check-pvc-storageclass.py` | `task flux:lint` + the CI flux-lint job (`extra_validation`) — every claim pins a `storageClassName` |
| `check-netpol-except-parity.py` | `task lint:netpol-parity`, the CI netpol-parity job — public-egress except-lists match a canonical reserved-CIDR set |
| `validate-helm-values.py` | `task flux:lint` + the CI flux-lint job (`extra_validation`) — `helm template` over the value-heavy releases |
| `check-deploy-coverage.sh` | the CI deploy-coverage job — every changed playbook/inventory path reaches a deploy job |
| `generate-versions-configmap.py` | `task flux:sync-versions` and its CI drift gate |
| `generate-hosts-env.py` | `task hosts:sync` and its CI drift gate |
| `check-versions.py` | `task maintenance:check-versions`, the CI version-bump bot |
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

The copies here are **byte-identical** to the library's at the pinned ref, and
nothing above is forked. That is what makes the refresh a mechanical `cp` and a
diff review; the library's own gate compares them, so a local edit that has to
survive belongs upstream in the library, not here.

Note on `flux-env.sh`: this cluster substitutes from **two** ConfigMaps
(`cluster-versions` for pins, `cluster-config` for domains, VIPs and CIDRs)
while the library's render helper takes one file per call. `flux-env.sh` merges
them behind the same `export-versions` / `k8s-version` interface, so the local
lint and the CI job see identical variables — which is why `task flux:lint` and
the CI flux-lint job's `flux_render_script` input both point at THIS file, never
at `flux-render.sh` directly. Extra ConfigMaps can also come from
`$FLUX_EXTRA_CONFIGMAPS`; `merged-configmap` emits the union as one document for
the tools that accept a single ConfigMap path.

## Site data

| File | Read by | Notes |
|---|---|---|
| `hosts.env` | the Taskfile (`dotenv:`), the verification scripts | **Generated.** `task hosts:sync` after every inventory edit; CI fails when it is stale |
| `hosts-env-map.yml` | `generate-hosts-env.py` | Which inventory group becomes which variable |
| `version-registry.py` | `check-versions.py` | Which upstreams to watch. Ships the platform set; add an entry per app you adopt |
| `autoscaling-policy.yaml` | `check-hpa-vpa-invariant.py`, `validate-helm-values.py` | Chart-native HPA targets and the CPU-limit allowlist |
| `helm-values-releases.yaml` | `validate-helm-values.py` | Which HelmReleases are worth a `helm template` round-trip |
| `deploy-coverage.conf` | `check-deploy-coverage.sh` | Playbooks deliberately not wired to a deploy job, each with a rationale |
| `netpol-except.yaml` | `check-netpol-except-parity.py` | Egress rules that deliberately carry no peers, each with a rationale |

`hosts.env` ships empty: the host roster is the one thing the template cannot
guess. Until you fill in `ansible/inventories/prod/hosts.yml` and run `task
hosts:sync`, any task that iterates over a host list stops with an explicit
"empty — run task hosts:sync" message rather than doing nothing quietly.
