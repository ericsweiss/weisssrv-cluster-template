"""Layout invariants this cluster keeps, in PyYAML alone: site values stay out
of the manifests and agree with the inventory, the Proxmox HA triad is one node,
and every playbook is classified for deploy coverage. See docs/ci-pipeline.md."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from conftest import REPO as REPO_ROOT
from conftest import (
    CONFIGMAP_INVENTORY_MIRROR,
    assert_all_parsed,
    k8s_documents,
    load_script,
)

K8S_ROOT = REPO_ROOT / "kubernetes"
SOURCES_DIR = K8S_ROOT / "infrastructure" / "sources"
CLUSTERS_DIR = K8S_ROOT / "clusters"
ANSWERS_FILE = REPO_ROOT / ".copier-answers.yml"
INVENTORY_HOSTS = REPO_ROOT / "ansible" / "inventories" / "prod" / "hosts.yml"
TASKFILE = REPO_ROOT / "Taskfile.yml"
TASKFILES_DIR = REPO_ROOT / "taskfiles"
# The ansible tasks run with `dir: ./ansible`, so a playbook path is relative to
# that, not to the repository root.
ANSIBLE_DIR = REPO_ROOT / "ansible"
PLAYBOOK_REF_RE = re.compile(r"playbooks/[A-Za-z0-9_./-]+\.ya?ml")
PLAYBOOKS_DIR = ANSIBLE_DIR / "playbooks"
CI_FILE = REPO_ROOT / ".gitlab-ci.yml"
COVERAGE_CONF = REPO_ROOT / "scripts" / "deploy-coverage.conf"

# The postBuild sources every stage after `sources` substitutes from.
SUBSTITUTE_SOURCES = ("cluster-versions", "cluster-config")
# infrastructure-sources creates both ConfigMaps, so it has nothing to
# substitute from.
SUBSTITUTE_EXEMPT = {"infrastructure-sources"}

# `flux bootstrap` writes clusters/*/flux-system/ and replaces it wholesale on
# every Flux bump: its Kustomization carries no postBuild block and its
# GitRepository url is necessarily the literal git host.
BOOTSTRAP_DIR = "flux-system"

# Flux substitutes ${var}; $${var} is the escape that reaches the cluster as a
# literal ${var} (shell snippets embedded in manifests use it).
PLACEHOLDER_RE = re.compile(r"(?<!\$)\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?$")
DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$")

needs_k8s = pytest.mark.skipif(
    not K8S_ROOT.is_dir(), reason="no kubernetes/ tree in this repository"
)
needs_inventory = pytest.mark.skipif(
    not INVENTORY_HOSTS.is_file(), reason="no ansible/inventories/prod/hosts.yml"
)
needs_taskfile = pytest.mark.skipif(not TASKFILE.is_file(), reason="no Taskfile.yml")
needs_deploy_coverage = pytest.mark.skipif(
    not (PLAYBOOKS_DIR.is_dir() and CI_FILE.is_file() and COVERAGE_CONF.is_file()),
    reason="no playbooks tree, .gitlab-ci.yml or deploy-coverage.conf",
)


SCANNED_SUFFIXES = (".yaml", ".yml", ".json", ".toml", ".tpl", ".py")


def _yaml_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.yaml") if p.is_file())


def _scannable_files(root: Path) -> list[Path]:
    """Every file a site literal can hide in, not just the YAML manifests.

    Dashboards are JSON, configMapGenerator payloads are .py/.toml/.tpl, and a
    hard-coded domain reaches the cluster from any of them.
    """
    return sorted(
        p for p in root.rglob("*") if p.is_file() and p.suffix in SCANNED_SUFFIXES
    )


_TRAILING_COMMENT_RE = re.compile(r"\s+#.*$")


def _code_lines(path: Path):
    """(lineno, line) for every line, with comments dropped whole-line and trailing.

    kustomize drops both before Flux ever sees the manifest, so a
    `${placeholder}` or an address written in documentation is not a value.
    """
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        yield lineno, _TRAILING_COMMENT_RE.sub("", line)


def _load_configmaps() -> dict[str, tuple[Path, dict[str, str]]]:
    """Map ConfigMap name -> (file, data) for the postBuild sources."""
    found: dict[str, tuple[Path, dict[str, str]]] = {}
    if not SOURCES_DIR.is_dir():
        return found
    for path in _yaml_files(SOURCES_DIR):
        for doc in yaml.safe_load_all(path.read_text()):
            if not isinstance(doc, dict) or doc.get("kind") != "ConfigMap":
                continue
            name = (doc.get("metadata") or {}).get("name")
            data = doc.get("data") or {}
            if name and data:
                found[name] = (path, {k: str(v) for k, v in data.items()})
    return found


CONFIGMAPS = _load_configmaps()
SUBSTITUTION_KEYS = {k for _, data in CONFIGMAPS.values() for k in data}


@needs_k8s
def test_cluster_config_configmap_exists():
    assert "cluster-config" in CONFIGMAPS, (
        "kubernetes/infrastructure/sources/ must define a `cluster-config` ConfigMap — "
        "it is the single source of the domains, VIPs and CIDRs the manifests substitute."
    )


@needs_k8s
def test_cluster_config_carries_the_expected_keys():
    _, data = CONFIGMAPS["cluster-config"]
    required = {
        "cluster_name",
        "cluster_internal_domain",
        "cluster_external_domain",
        "cluster_lan_cidr",
        "cluster_k3s_api_vip",
        "cluster_metallb_public_vip",
        "cluster_metallb_internal_vip",
    }
    assert required <= set(data), f"cluster-config is missing keys: {sorted(required - set(data))}"


@needs_k8s
def test_substitution_keys_are_unique_across_configmaps():
    """Flux merges substituteFrom sources in list order — a duplicate resolves
    to whichever source is listed last, which is invisible in review."""
    seen: dict[str, Path] = {}
    clashes = []
    for path, data in CONFIGMAPS.values():
        for key in data:
            if key in seen:
                clashes.append(f"{key} in {seen[key]} and {path}")
            seen[key] = path
    assert not clashes, "duplicate substitution keys: " + "; ".join(clashes)


@needs_k8s
def test_every_placeholder_resolves():
    unresolved: list[str] = []
    # Dashboard JSON too: strict substitution takes the whole observability
    # stage down over one unknown key there. `${DS_...}` is Grafana's own
    # datasource-input syntax, which Flux leaves alone.
    files = list(_yaml_files(K8S_ROOT)) + sorted(K8S_ROOT.rglob("*.json"))
    for path in files:
        for lineno, line in _code_lines(path):
            for name in PLACEHOLDER_RE.findall(line):
                if name.startswith("DS_") or name in SUBSTITUTION_KEYS:
                    continue
                rel = path.relative_to(REPO_ROOT)
                unresolved.append(f"{rel}:{lineno} ${{{name}}}")
    assert not unresolved, (
        "placeholders with no ConfigMap key (kustomize-controller runs with "
        "StrictPostBuildSubstitutions, so each one fails its stage's reconcile; "
        "escape a deliberate literal as $${...}):\n  " + "\n  ".join(unresolved)
    )


def _substitute_problems(clusters_dir: Path, root: Path) -> tuple[list[str], int]:
    """Stages whose substituteFrom is not exactly both sources at optional:false."""
    expected = set(SUBSTITUTE_SOURCES)
    problems: list[str] = []
    examined = 0
    for path in _yaml_files(clusters_dir):
        if BOOTSTRAP_DIR in path.parts:
            continue
        for doc in yaml.safe_load_all(path.read_text()):
            if not isinstance(doc, dict) or doc.get("kind") != "Kustomization":
                continue
            spec = doc.get("spec") or {}
            if not spec.get("path"):
                continue
            name = (doc.get("metadata") or {}).get("name", "?")
            if name in SUBSTITUTE_EXEMPT:
                continue
            examined += 1
            entries = [
                entry
                for entry in (spec.get("postBuild") or {}).get("substituteFrom") or []
                if isinstance(entry, dict)
            ]
            rel = path.relative_to(root)
            refs = {entry.get("name") for entry in entries if entry.get("kind") == "ConfigMap"}
            if refs != expected:
                problems.append(f"{rel} ({name}): substitutes from {sorted(refs)}")
                continue
            lax = sorted(e.get("name") for e in entries if e.get("optional") is not False)
            if lax:
                problems.append(f"{rel} ({name}): {lax} must be optional: false")
    return problems, examined


@needs_k8s
def test_flux_kustomizations_carry_both_substitute_sources():
    if not CLUSTERS_DIR.is_dir():
        pytest.skip("no kubernetes/clusters/ tree")
    missing_sources = sorted(set(SUBSTITUTE_SOURCES) - set(CONFIGMAPS))
    assert not missing_sources, (
        f"kubernetes/infrastructure/sources/ declares {missing_sources} nowhere — "
        "every substituted manifest would reconcile with unresolved placeholders"
    )
    problems, examined = _substitute_problems(CLUSTERS_DIR, REPO_ROOT)
    assert examined, (
        "no Flux Kustomization with a spec.path outside clusters/*/flux-system/ — "
        "this gate is examining nothing"
    )
    assert not problems, (
        "Flux Kustomizations with an incomplete substituteFrom — each must list "
        "exactly both sources at optional: false, or under "
        "StrictPostBuildSubstitutions its manifests' placeholders fail the "
        "stage's reconcile:\n  " + "\n  ".join(problems)
    )


@needs_k8s
def test_the_substitute_exemption_still_names_a_real_kustomization():
    if not CLUSTERS_DIR.is_dir():
        pytest.skip("no kubernetes/clusters/ tree")
    names = {
        (doc.get("metadata") or {}).get("name")
        for _path, doc in _docs(CLUSTERS_DIR)
        if doc.get("kind") == "Kustomization"
    }
    stale = sorted(SUBSTITUTE_EXEMPT - names)
    assert not stale, f"stale substituteFrom exemptions: {stale}"


def _write_stage(path: Path, name: str, entries: str) -> None:
    path.write_text(
        "---\n"
        "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
        "kind: Kustomization\n"
        f"metadata:\n  name: {name}\n"
        "spec:\n  path: ./kubernetes/x\n  postBuild:\n    substituteFrom:\n"
        + entries
    )


def test_substitute_gate_rejects_an_optional_source(tmp_path):
    _write_stage(
        tmp_path / "stage.yaml",
        "apps",
        "      - kind: ConfigMap\n        name: cluster-versions\n        optional: false\n"
        "      - kind: ConfigMap\n        name: cluster-config\n        optional: true\n",
    )
    problems, examined = _substitute_problems(tmp_path, tmp_path)
    assert examined == 1
    assert problems == ["stage.yaml (apps): ['cluster-config'] must be optional: false"]


def test_substitute_gate_rejects_an_extra_source(tmp_path):
    _write_stage(
        tmp_path / "stage.yaml",
        "apps",
        "      - kind: ConfigMap\n        name: cluster-versions\n        optional: false\n"
        "      - kind: ConfigMap\n        name: cluster-config\n        optional: false\n"
        "      - kind: ConfigMap\n        name: extra\n        optional: false\n",
    )
    problems, _examined = _substitute_problems(tmp_path, tmp_path)
    assert problems == [
        "stage.yaml (apps): substitutes from "
        "['cluster-config', 'cluster-versions', 'extra']"
    ]


def test_substitute_gate_skips_the_exempt_stage(tmp_path):
    (tmp_path / "sources.yaml").write_text(
        "---\n"
        "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
        "kind: Kustomization\n"
        "metadata:\n  name: infrastructure-sources\n"
        "spec:\n  path: ./kubernetes/infrastructure/sources\n"
    )
    assert _substitute_problems(tmp_path, tmp_path) == ([], 0)


@needs_k8s
def test_no_flux_kustomization_path_nests_inside_another():
    """Two Kustomizations whose spec.path nest each own the inner objects, so
    each prune deletes what the other reconciled — a reconcile loop that reads
    as intermittent drift."""
    if not CLUSTERS_DIR.is_dir():
        pytest.skip("no kubernetes/clusters/ tree")
    paths: dict[str, str] = {}
    for path, doc in _docs(CLUSTERS_DIR):
        if doc.get("kind") != "Kustomization":
            continue
        spec_path = (doc.get("spec") or {}).get("path")
        if spec_path:
            name = (doc.get("metadata") or {}).get("name", path.name)
            paths[str(spec_path).rstrip("/").removeprefix("./")] = name
    assert paths, "no Flux Kustomization paths found — this gate is examining nothing"
    nested = [
        f"{paths[inner]} ({inner}) nests inside {paths[outer]} ({outer})"
        for inner in paths
        for outer in paths
        if inner != outer and inner.startswith(outer + "/")
    ]
    assert not nested, "nested Flux Kustomization paths:\n  " + "\n  ".join(nested)


FLUX_KUSTOMIZE_API = "kustomize.toolkit.fluxcd.io/"


def _path_less_kustomizations(root: Path) -> tuple[list[str], int]:
    """(Flux Kustomizations carrying no spec.path, documents examined).

    spec.path is optional and defaults to the repository root, so a stage that
    loses it reconciles the whole repo and escapes every path-walking gate.
    """
    offenders: list[str] = []
    examined = 0
    for path, doc in _docs(root):
        if BOOTSTRAP_DIR in path.parts or doc.get("kind") != "Kustomization":
            continue
        if not str(doc.get("apiVersion") or "").startswith(FLUX_KUSTOMIZE_API):
            continue
        examined += 1
        if not (doc.get("spec") or {}).get("path"):
            name = (doc.get("metadata") or {}).get("name", "?")
            offenders.append(f"{path.relative_to(root.parent)} ({name})")
    return offenders, examined


@needs_k8s
def test_every_flux_kustomization_declares_a_path():
    if not CLUSTERS_DIR.is_dir():
        pytest.skip("no kubernetes/clusters/ tree")
    offenders, examined = _path_less_kustomizations(CLUSTERS_DIR)
    assert examined, (
        "no Flux Kustomization outside clusters/*/flux-system/ — this gate is "
        "examining nothing"
    )
    assert not offenders, (
        "Flux Kustomizations with no spec.path — each reconciles the whole "
        "repository and is skipped by every build, kubeconform and corpus "
        "gate:\n  " + "\n  ".join(offenders)
    )


def test_a_kustomization_that_lost_its_path_is_reported(tmp_path):
    """Mutation case: a path-less stage, and the two documents the gate leaves
    alone (a plain kustomize Kustomization, a bootstrap one)."""
    root = tmp_path / "clusters"
    boot = root / "c" / BOOTSTRAP_DIR
    boot.mkdir(parents=True)
    stage = (
        f"apiVersion: {FLUX_KUSTOMIZE_API}v1\nkind: Kustomization\n"
        "metadata:\n  name: apps\nspec:\n  prune: true\n"
    )
    (root / "c" / "apps.yaml").write_text(stage)
    (root / "c" / "kustomization.yaml").write_text(
        "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources: []\n"
    )
    (boot / "gotk-sync.yaml").write_text(stage)
    offenders, examined = _path_less_kustomizations(root)
    assert examined == 1
    assert len(offenders) == 1 and "(apps)" in offenders[0]
    (root / "c" / "apps.yaml").write_text(stage + "  path: ./kubernetes/apps\n")
    assert _path_less_kustomizations(root) == ([], 1)


def _literal_offenders(
    root: Path, ordered: list[tuple[str, str]], skip: Path | None = None
) -> tuple[list[str], int]:
    """(offenders, files scanned) for site values spelled out under `root`.

    clusters/*/flux-system/ falls out: `flux bootstrap` writes the git host into
    gotk-sync.yaml and replaces those files wholesale.
    """
    offenders: list[str] = []
    scanned = 0
    for path in _scannable_files(root):
        if path == skip or BOOTSTRAP_DIR in path.parts:
            continue
        scanned += 1
        for lineno, line in _code_lines(path):
            for value, key in ordered:
                if re.search(rf"(?<![\w.-]){re.escape(value)}(?![\w.-])", line):
                    rel = path.relative_to(root.parent)
                    offenders.append(f"{rel}:{lineno} {value!r} — use ${{{key}}}")
                    break
    return offenders, scanned


@needs_k8s
def test_no_site_addresses_are_hard_coded_in_manifests():
    """Domains, IPs and CIDRs belong in cluster-config, referenced as ${...}.
    Scoped to values that came from the copier answers, so a well-known constant
    in the ConfigMap may still be spelled out in a NetworkPolicy."""
    if "cluster-config" not in CONFIGMAPS:
        pytest.skip("no cluster-config ConfigMap")
    if not ANSWERS_FILE.is_file():
        pytest.skip("no .copier-answers.yml to source the site values from")
    config_file, data = CONFIGMAPS["cluster-config"]
    answers = yaml.safe_load(ANSWERS_FILE.read_text()) or {}
    site_values = {
        str(value)
        for key, value in answers.items()
        if not key.startswith("_")
        and isinstance(value, str)
        and (IPV4_RE.match(value) or DOMAIN_RE.match(value))
    }
    literals = {
        value: key for key, value in data.items() if value in site_values
    }
    assert literals, "no site value from the answers reaches cluster-config — this gate is examining nothing"
    # Longest value first, on a word boundary: a short domain inside a longer
    # one would otherwise be reported against the wrong key.
    ordered = sorted(literals.items(), key=lambda kv: -len(kv[0]))
    offenders, scanned = _literal_offenders(K8S_ROOT, ordered, skip=config_file)
    assert scanned, "no manifests scanned — this gate is examining nothing"
    assert not offenders, "site values hard-coded in manifests:\n  " + "\n  ".join(offenders)


def test_a_literal_under_the_bootstrap_directory_is_not_reported(tmp_path):
    """Mutation case: the same GitRepository url is reported one directory up, so
    the exclusion covers only the files flux bootstrap writes."""
    ordered = [("git.nonesuch.invalid", "cluster_git_host")]
    body = "spec:\n  url: https://git.nonesuch.invalid/ns/repo.git\n"
    root = tmp_path / "kubernetes"
    boot = root / "clusters" / "c" / BOOTSTRAP_DIR
    boot.mkdir(parents=True)
    (boot / "gotk-sync.yaml").write_text(body)
    assert _literal_offenders(root, ordered) == ([], 0)
    (boot.parent / "gotk-sync.yaml").write_text(body)
    offenders, scanned = _literal_offenders(root, ordered)
    assert scanned == 1
    assert len(offenders) == 1 and "cluster_git_host" in offenders[0]


REQUIRE_CLUSTER_ROOT_RE = re.compile(r"^\s*require_cluster_root:\s*(true|false)\b", re.M)


def _require_cluster_root(ci_text: str) -> list[str]:
    """The require_cluster_root values flux-lint is given, as written.

    Read as text: the pipeline file carries GitLab `!reference` tags.
    """
    return REQUIRE_CLUSTER_ROOT_RE.findall(ci_text)


@needs_k8s
def test_flux_lint_requires_the_cluster_root_once_bootstrapped():
    """`require_cluster_root: false` is right only before `flux bootstrap`. Once
    flux-system/ is committed, false lets a cluster that has lost that tree lint
    green, and check-flux-version-pin.py skips its component-set arm too."""
    gotk = sorted(CLUSTERS_DIR.glob(f"*/{BOOTSTRAP_DIR}/gotk-components.yaml"))
    if not gotk:
        pytest.skip("pre-bootstrap: no clusters/*/flux-system/gotk-components.yaml yet")
    if not CI_FILE.is_file():
        pytest.skip("no .gitlab-ci.yml in this repository")
    values = _require_cluster_root(CI_FILE.read_text(encoding="utf-8"))
    assert values, "flux-lint sets no require_cluster_root — this gate is examining nothing"
    assert "false" not in values, (
        f"{gotk[0].relative_to(REPO_ROOT)} exists: set require_cluster_root: true in "
        ".gitlab-ci.yml now that flux-system exists (README bring-up step)"
    )


def test_a_false_require_cluster_root_is_reported():
    """Mutation case: the reader, not just the shipped pipeline."""
    assert _require_cluster_root("    inputs:\n      require_cluster_root: false\n") == ["false"]
    assert _require_cluster_root("      require_cluster_root: true\n") == ["true"]
    assert _require_cluster_root("# require_cluster_root: false\n") == []


GROUP_VARS = REPO_ROOT / "ansible" / "inventories" / "prod" / "group_vars"
needs_group_vars = pytest.mark.skipif(
    not GROUP_VARS.is_dir(), reason="no ansible/inventories/prod/group_vars"
)


def _mirror_report(mirror: dict) -> tuple[list[str], list[str], int]:
    """(pairs one side no longer declares, disagreements, pairs compared).

    A pair whose key or variable is gone is reported, not dropped: a rename would
    otherwise remove it from the comparison and leave the gate green.
    """
    data = CONFIGMAPS["cluster-config"][1]
    loaded: dict[str, dict] = {}
    undeclared: list[str] = []
    mismatches: list[str] = []
    compared = 0
    for cm_key, (group_file, var_name) in sorted(mirror.items()):
        path = GROUP_VARS / group_file
        label = f"{cm_key} / group_vars/{group_file}:{var_name}"
        if not path.is_file() or cm_key not in data:
            undeclared.append(f"{label} (cluster-config key or group_vars file absent)")
            continue
        if group_file not in loaded:
            loaded[group_file] = yaml.safe_load(path.read_text()) or {}
        if var_name not in loaded[group_file]:
            undeclared.append(f"{label} (inventory variable absent)")
            continue
        compared += 1
        if str(loaded[group_file][var_name]) != str(data[cm_key]):
            mismatches.append(
                f"{cm_key}={data[cm_key]!r} but group_vars/{group_file}:{var_name}="
                f"{loaded[group_file][var_name]!r}"
            )
    return undeclared, mismatches, compared


@needs_k8s
@needs_group_vars
def test_cluster_config_agrees_with_the_ansible_inventory():
    """cluster-config and the inventory agree. Nothing reconciles them at deploy
    time, so a domain changed in one place alone surfaces as a certificate or
    node selector that matches nothing."""
    if "cluster-config" not in CONFIGMAPS:
        pytest.skip("no cluster-config ConfigMap")
    undeclared, mismatches, compared = _mirror_report(CONFIGMAP_INVENTORY_MIRROR)
    assert not undeclared, (
        "CONFIGMAP_INVENTORY_MIRROR names pairs one side no longer declares — fix "
        "the rename or drop the pair:\n  " + "\n  ".join(undeclared)
    )
    assert compared, (
        "no cluster-config key was compared against the inventory — this gate is "
        "examining nothing"
    )
    assert not mismatches, (
        "cluster-config and the Ansible inventory disagree:\n  " + "\n  ".join(mismatches)
    )


@needs_k8s
@needs_group_vars
@pytest.mark.parametrize(
    "cm_key,pair",
    [
        pytest.param("cluster_internal_domain", ("all.yml", "no_such_var"), id="variable"),
        pytest.param("cluster_internal_domain", ("no-such.yml", "internal_domain"), id="file"),
        pytest.param("cluster_no_such_key", ("all.yml", "internal_domain"), id="configmap-key"),
    ],
)
def test_a_mirror_pair_one_side_lost_is_reported(cm_key, pair):
    """Mutation case: a renamed key or variable must red the gate, not vanish."""
    if "cluster-config" not in CONFIGMAPS:
        pytest.skip("no cluster-config ConfigMap")
    mirror = dict(CONFIGMAP_INVENTORY_MIRROR)
    mirror[cm_key] = pair
    undeclared, _mismatches, _compared = _mirror_report(mirror)
    assert undeclared


_SVC_DNS_PORT_RE = re.compile(
    r"([a-z0-9][a-z0-9-]*)\.([a-z0-9][a-z0-9-]*)\.svc\.cluster\.local(?::(\d+))?"
)
# The namespace External Secrets Operator runs in; the egress allow that
# reaches the backend has to live here, not in the backend's namespace.
CONTROLLER_NAMESPACE = "external-secrets"


def _docs(root: Path):
    for path in _yaml_files(root):
        for doc in yaml.safe_load_all(path.read_text()):
            if isinstance(doc, dict):
                yield path, doc


def _policy_types(spec: dict) -> set[str]:
    """policyTypes, applying the API default. An explicit non-empty list wins;
    otherwise the direction is inferred from the rules present, so an omitted or
    empty field is not read as "no direction"."""
    declared = spec.get("policyTypes")
    if isinstance(declared, list) and declared:
        return {str(t) for t in declared}
    types = {"Ingress"}
    if spec.get("egress"):
        types.add("Egress")
    return types


def _namespaces_denying_egress() -> set[str]:
    """Namespaces carrying a namespace-wide egress deny (no egress rules)."""
    denied = set()
    for _path, doc in _docs(K8S_ROOT):
        if doc.get("kind") != "NetworkPolicy":
            continue
        spec = doc.get("spec") or {}
        namespace = (doc.get("metadata") or {}).get("namespace")
        if not namespace or spec.get("podSelector"):
            continue
        if "Egress" in _policy_types(spec) and not spec.get("egress"):
            denied.add(namespace)
    return denied


def _store_backends(root: Path) -> set[tuple[str, str, int]]:
    """(namespace, service, port) for every in-cluster backend a store names."""
    targets: set[tuple[str, str, int]] = set()
    for path, doc in _docs(root):
        if doc.get("kind") not in {"ClusterSecretStore", "SecretStore"}:
            continue
        for match in _SVC_DNS_PORT_RE.finditer(path.read_text()):
            service, namespace, port = match.group(1), match.group(2), match.group(3)
            targets.add((namespace, service, int(port) if port else 80))
    return targets


def _egress_reaches(policy: dict, namespace: str, port: int) -> bool:
    """One egress rule that names `port` AND selects the backend, rather than
    any rule at all: a DNS-only allow must not satisfy this."""
    own = (policy.get("metadata") or {}).get("namespace")
    for rule in (policy.get("spec") or {}).get("egress") or []:
        ports = {p.get("port") for p in (rule.get("ports") or [])}
        if ports and port not in ports:
            continue
        for peer in rule.get("to") or []:
            if "ipBlock" in peer:
                continue
            selector = peer.get("namespaceSelector")
            if namespace != own:
                if selector is None:
                    continue
                named = (selector.get("matchLabels") or {}).get("kubernetes.io/metadata.name")
                # An empty selector is every namespace, and matchExpressions are
                # not comparable here, so only a named mismatch is rejected.
                if named is not None and named != namespace:
                    continue
                return True
            if "podSelector" in peer or selector is not None:
                return True
    return False


def _unreachable_backends(root: Path, controller_ns: str, backends) -> list[str]:
    """Backends the controller namespace has no egress allow for."""
    policies = [
        doc
        for _path, doc in _docs(root)
        if doc.get("kind") == "NetworkPolicy"
        and (doc.get("metadata") or {}).get("namespace") == controller_ns
    ]
    unreachable = []
    for namespace, service, port in sorted(backends):
        if not any(_egress_reaches(policy, namespace, port) for policy in policies):
            unreachable.append(f"{controller_ns} -> {namespace}/{service}:{port}")
    return unreachable


@needs_k8s
def test_the_secret_store_backend_is_reachable_from_external_secrets():
    """The controller namespace needs an egress allow naming the backend and its
    port. A default-deny with no such allow lints clean and only surfaces as every
    ExternalSecret failing to sync, so both preconditions are asserted, not skipped."""
    backends = _store_backends(K8S_ROOT)
    assert backends, (
        "no ClusterSecretStore or SecretStore names an in-cluster "
        "<service>.<namespace>.svc.cluster.local backend — the store URL shape "
        "moved and this gate is inspecting nothing"
    )
    assert CONTROLLER_NAMESPACE in _namespaces_denying_egress(), (
        f"{CONTROLLER_NAMESPACE} runs no namespace-wide egress deny — "
        "netpol-baseline is mandatory in every namespace, so this gate is "
        "inspecting nothing"
    )
    unreachable = _unreachable_backends(K8S_ROOT, CONTROLLER_NAMESPACE, backends)
    assert not unreachable, (
        "the external-secrets namespace denies egress and has no allow naming "
        "these backends, so no ExternalSecret can ever sync:\n  " + "\n  ".join(unreachable)
    )


def test_a_dns_only_egress_allow_does_not_count_as_reaching_the_backend():
    """Mutation case: the old shape accepted any egress rule in the namespace."""
    dns_only = {
        "kind": "NetworkPolicy",
        "metadata": {"namespace": CONTROLLER_NAMESPACE},
        "spec": {
            "egress": [
                {
                    "to": [{"namespaceSelector": {}}],
                    "ports": [{"protocol": "UDP", "port": 53}],
                }
            ]
        },
    }
    assert not _egress_reaches(dns_only, CONTROLLER_NAMESPACE, 8080)
    connect = {
        "kind": "NetworkPolicy",
        "metadata": {"namespace": CONTROLLER_NAMESPACE},
        "spec": {
            "egress": [
                {
                    "to": [{"podSelector": {"matchLabels": {"app": "onepassword-connect"}}}],
                    "ports": [{"protocol": "TCP", "port": 8080}],
                }
            ]
        },
    }
    assert _egress_reaches(connect, CONTROLLER_NAMESPACE, 8080)

    def cross(ns_name: str) -> dict:
        return {
            "kind": "NetworkPolicy",
            "metadata": {"namespace": CONTROLLER_NAMESPACE},
            "spec": {
                "egress": [
                    {
                        "to": [
                            {
                                "namespaceSelector": {
                                    "matchLabels": {"kubernetes.io/metadata.name": ns_name}
                                }
                            }
                        ],
                        "ports": [{"protocol": "TCP", "port": 8200}],
                    }
                ]
            },
        }

    assert not _egress_reaches(cross("other-ns"), "vault", 8200)
    assert _egress_reaches(cross("vault"), "vault", 8200)


def test_the_egress_deny_scan_applies_the_api_policy_type_default():
    """Mutation case for _policy_types: a policy with egress rules and no
    policyTypes restricts egress, and an omitted, empty or non-list field takes
    the API's inferred default rather than reading as no direction."""
    assert _policy_types({"egress": [{}]}) == {"Ingress", "Egress"}
    assert _policy_types({}) == {"Ingress"}
    assert _policy_types({"policyTypes": []}) == {"Ingress"}
    assert _policy_types({"policyTypes": "Egress"}) == {"Ingress"}
    assert _policy_types({"policyTypes": ["Egress"]}) == {"Egress"}


def test_a_placeholder_backend_host_reads_as_no_backend(tmp_path):
    """Mutation case: a store URL written as a ${...} placeholder matches nothing,
    which is why the reachability gate asserts its backends instead of skipping."""
    (tmp_path / "store.yaml").write_text(
        "kind: ClusterSecretStore\n"
        "metadata:\n"
        "  name: secrets\n"
        "spec:\n"
        "  provider:\n"
        "    webhook:\n"
        "      url: https://${cluster_secret_backend_host}/v1/items\n"
    )
    assert not _store_backends(tmp_path)


# --------------------------------------------------------------------------
# Ansible inventory
# --------------------------------------------------------------------------


def _inventory_hosts() -> dict[str, dict]:
    """host name -> merged vars, for every host in the YAML inventory. Merged
    rather than collected per group, so a host in two groups is one machine and
    not a duplicate of itself."""
    hosts: dict[str, dict] = {}

    def walk(node) -> None:
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if key == "hosts" and isinstance(value, dict):
                for name, host_vars in value.items():
                    hosts.setdefault(name, {}).update(host_vars or {})
            elif key != "vars":
                walk(value)

    walk(yaml.safe_load(INVENTORY_HOSTS.read_text()) or {})
    return hosts


@needs_inventory
def test_inventory_declares_hosts():
    """The HA-triad arm below is vacuously true on an empty inventory. Duplicate
    vmids and addresses, a host on a VIP and a host outside cluster_lan_cidr are
    `task lint:cluster-invariants`."""
    assert _inventory_hosts(), f"{INVENTORY_HOSTS} declares no hosts"


# Proxmox HA: the guest's host, the affinity rule's home and the replication
# source are one node. They are declared in three separate structures.
_HA_RESOURCE_RE = re.compile(r"^(?:ct|vm):(\d+)$")
_HA_KEYS = ("proxmox_ha_rules", "proxmox_ha_replication_jobs")
_HA_GROUP_FILES = ("proxmox.yml", "all.yml")


def _ha_vars() -> dict:
    """The two HA lists, from whichever group_vars file declares them."""
    merged: dict = {}
    for name in _HA_GROUP_FILES:
        path = GROUP_VARS / name
        if not path.is_file():
            continue
        data = yaml.safe_load(path.read_text()) or {}
        merged.update({key: data[key] for key in _HA_KEYS if key in data})
    return merged


def _ha_home_node(rule: dict) -> str | None:
    """The single node a node-affinity rule gives priority 2."""
    homes = [
        node.split(":", 1)[0]
        for node in rule.get("nodes") or []
        if isinstance(node, str) and node.split(":", 1)[-1] == "2"
    ]
    return homes[0] if len(homes) == 1 else None


def ha_triad_problems(hosts: dict[str, dict], all_vars: dict) -> list[str]:
    """proxmox_host == affinity home == replication source_node, per guest."""
    rules = all_vars.get("proxmox_ha_rules") or []
    jobs = all_vars.get("proxmox_ha_replication_jobs") or []
    if not rules or not jobs:
        raise AssertionError(
            "one of proxmox_ha_rules / proxmox_ha_replication_jobs is empty and the "
            "other is not — an HA guest with no replica, or a replica nothing fails "
            "over to"
        )

    by_vmid = {
        str(host_vars["vmid"]): (name, host_vars)
        for name, host_vars in hosts.items()
        if host_vars.get("vmid") is not None
    }
    problems: list[str] = []
    checked = 0

    for rule in rules:
        name = rule.get("name", "<unnamed>")
        resources = rule.get("resources") or []
        match = _HA_RESOURCE_RE.match(str(resources[0])) if resources else None
        if not match:
            problems.append(f"{name}: resources[0] is not ct:<id> / vm:<id>")
            continue
        vmid = match.group(1)
        if vmid not in by_vmid:
            problems.append(f"{name}: vmid {vmid} is in no inventory host")
            continue
        host_name, host_vars = by_vmid[vmid]

        home = _ha_home_node(rule)
        if home is None:
            problems.append(f"{name}: needs exactly one node at priority 2")
            continue
        if host_vars.get("proxmox_host") != home:
            problems.append(
                f"{name}: home is {home} but {host_name}'s proxmox_host is "
                f"{host_vars.get('proxmox_host')}"
            )

        for job in jobs:
            if str(job.get("id", "")).split("-", 1)[0] != vmid:
                continue
            checked += 1
            if job.get("source_node") != home:
                problems.append(
                    f"replication {job.get('id')}: source_node "
                    f"{job.get('source_node')} is not {name}'s home {home}"
                )
            if job.get("target_node") == job.get("source_node"):
                problems.append(f"replication {job.get('id')}: target_node equals source_node")

    if not checked:
        raise AssertionError("no replication job matched an HA rule — nothing examined")
    return problems


@needs_inventory
@needs_group_vars
def test_the_ha_triad_is_one_unit():
    """A guest whose affinity home is not the node holding its replica fails over
    to a node with no copy of its disks, so the recovery is a restore."""
    all_vars = _ha_vars()
    if not any(all_vars.get(key) for key in _HA_KEYS):
        pytest.skip("no proxmox_ha_rules or proxmox_ha_replication_jobs declared yet")
    problems = ha_triad_problems(_inventory_hosts(), all_vars)
    assert not problems, (
        "HA affinity / replication / proxmox_host disagree:\n  " + "\n  ".join(problems)
    )


# One coherent triad, so the mutation cases below hold on a cluster that declares
# no HA rules yet.
_HA_HOSTS = {"dns-01": {"vmid": 101, "proxmox_host": "pve-01"}}
_HA_VARS = {
    "proxmox_ha_rules": [
        {"name": "dns-01-home", "resources": ["ct:101"], "nodes": ["pve-01:2", "pve-02:1"]}
    ],
    "proxmox_ha_replication_jobs": [
        {"id": "101-0", "source_node": "pve-01", "target_node": "pve-02"}
    ],
}


def test_the_ha_triad_reader_passes_a_coherent_triad():
    assert ha_triad_problems(_HA_HOSTS, _HA_VARS) == []


def test_a_moved_ha_home_is_caught():
    """Mutation case: the affinity home moved off the node the guest runs on."""
    rule = {**_HA_VARS["proxmox_ha_rules"][0], "nodes": ["pve-01:1", "pve-02:2"]}
    assert ha_triad_problems(_HA_HOSTS, {**_HA_VARS, "proxmox_ha_rules": [rule]})


def test_a_replication_job_targeting_its_own_source_is_caught():
    job = {**_HA_VARS["proxmox_ha_replication_jobs"][0], "target_node": "pve-01"}
    assert ha_triad_problems(_HA_HOSTS, {**_HA_VARS, "proxmox_ha_replication_jobs": [job]})


def test_a_one_sided_ha_declaration_is_vacuous_not_green():
    """A renamed key must fail loudly rather than turn the gate into a pass."""
    for key in _HA_KEYS:
        with pytest.raises(AssertionError):
            ha_triad_problems(_HA_HOSTS, {**_HA_VARS, key: []})


@needs_taskfile
def test_every_taskfile_playbook_exists():
    """Every playbook the Taskfile names exists. Nothing else checks it:
    ansible-lint walks the playbooks/ tree, not the tasks, and
    check-taskfile.sh resolves only script and dotenv references."""
    # The whole tree: the root file plus the namespace files it includes.
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [TASKFILE, *sorted(TASKFILES_DIR.glob("*.yml"))]
        if path.is_file()
    )
    refs = sorted(set(PLAYBOOK_REF_RE.findall(text)))
    assert refs, "the Taskfile tree names no playbooks — this gate is examining nothing"
    missing = [ref for ref in refs if not (ANSIBLE_DIR / ref).is_file()]
    assert not missing, (
        "the Taskfile tree names playbooks that do not exist under ansible/:\n  "
        + "\n  ".join(missing)
    )


def _deploy_job_playbooks() -> set[str]:
    """Playbook paths named verbatim in a deploy job's `changes:` list.

    Same rule as check-deploy-coverage.sh: a wildcard confers no coverage, so a
    single `ansible/playbooks/**` cannot mask a missing trigger.
    """
    ci_yaml = load_script("ci_yaml.py")
    ci = ci_yaml.load_ci(CI_FILE, loader=ci_yaml.NullTagCILoader) or {}
    prefix = "ansible/playbooks/"
    mapped: set[str] = set()
    for name, job in ci.items():
        if not isinstance(job, dict) or not name.startswith("deploy-"):
            continue
        if job.get("stage") != "deploy":
            continue
        for rule in job.get("rules") or []:
            if not isinstance(rule, dict):
                continue
            changes = rule.get("changes") or []
            if isinstance(changes, dict):
                changes = changes.get("paths") or []
            if not isinstance(changes, list):
                continue
            for change in changes:
                if not isinstance(change, str) or not change.startswith(prefix):
                    continue
                rel = change[len(prefix) :]
                if "*" not in rel and rel.endswith((".yml", ".yaml")):
                    mapped.add(rel)
    return mapped


def _acknowledged_playbooks() -> set[str]:
    """The `[playbooks]` entries of deploy-coverage.conf, comments stripped."""
    entries: set[str] = set()
    section = None
    for raw in COVERAGE_CONF.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section == "playbooks":
            entries.add(line.split("#", 1)[0].strip())
    return entries


@needs_deploy_coverage
def test_every_playbook_is_deploy_classified():
    """Every playbook is wired to a deploy job or acknowledged in
    scripts/deploy-coverage.conf. check-deploy-coverage.sh inspects only the
    paths one MR touched, so it cannot see an unclassified playbook."""
    playbooks = sorted(
        str(p.relative_to(PLAYBOOKS_DIR))
        for p in PLAYBOOKS_DIR.rglob("*")
        if p.is_file() and p.suffix in {".yml", ".yaml"}
    )
    assert playbooks, "no playbooks found — this gate is examining nothing"
    classified = _deploy_job_playbooks() | _acknowledged_playbooks()
    unclassified = [p for p in playbooks if p not in classified]
    assert not unclassified, (
        "playbooks reachable by neither a deploy job's changes: list nor "
        "scripts/deploy-coverage.conf [playbooks]:\n  " + "\n  ".join(unclassified)
    )


@needs_deploy_coverage
def test_deploy_coverage_conf_lists_no_missing_playbook():
    """The mirror direction: an entry left behind by a rename makes the conf
    read as coverage for a file that no longer exists."""
    stale = [p for p in sorted(_acknowledged_playbooks()) if not (PLAYBOOKS_DIR / p).is_file()]
    assert not stale, (
        "scripts/deploy-coverage.conf [playbooks] names files that do not "
        "exist under ansible/playbooks/:\n  " + "\n  ".join(stale)
    )


# --------------------------------------------------------------------------
# The scanners themselves
# --------------------------------------------------------------------------


def test_code_lines_drops_trailing_comments(tmp_path):
    """A documented address in a trailing comment is not a hard-coded value —
    kustomize drops it before Flux ever sees the manifest."""
    path = tmp_path / "sample.yaml"
    path.write_text("# 10.0.0.1\nhost: 10.0.0.2  # 10.0.0.3\n")
    assert [line for _, line in _code_lines(path)] == ["host: 10.0.0.2"]


def test_scannable_files_reaches_more_than_yaml(tmp_path):
    for name in ("a.yaml", "b.json", "c.py", "d.png"):
        (tmp_path / name).write_text("x")
    found = {p.name for p in _scannable_files(tmp_path)}
    assert found == {"a.yaml", "b.json", "c.py"}


# === The sources stage ================================================


@needs_k8s
def test_sources_stage_carries_no_placeholder():
    """`sources` is the one stage with no substituteFrom, so a ${...} there
    ships to the cluster as a literal string."""
    assert SOURCES_DIR.is_dir(), f"{SOURCES_DIR} is missing"
    for path in sorted(SOURCES_DIR.glob("*.yaml")):
        body = "\n".join(line for _, line in _code_lines(path))
        assert "${" not in body.replace("$${", "__FLUX_ESCAPED__"), (
            f"{path.name}: this stage has no substituteFrom, so the placeholder "
            "reaches the cluster verbatim"
        )


# Manifests that are deliberately not reconciled: copy-and-edit templates and
# the break-glass NodePort. Each says so in its own header.
UNREFERENCED_ALLOWED = {
    "kubernetes/apps/vm-ingress/certificate.yaml",
    "kubernetes/apps/vm-ingress/example-route.yaml",
    "kubernetes/infrastructure/observability/loki/nodeport.yaml",
}


def _kustomization_dirs() -> list[Path]:
    return sorted(p.parent for p in K8S_ROOT.rglob("kustomization.yaml"))


def _referenced_names(doc: dict) -> set[str]:
    """Every file name the kustomization names, in any field that takes one."""
    names: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, str):
            names.add(node)
        elif isinstance(node, list):
            for child in node:
                walk(child)
        elif isinstance(node, dict):
            for key, child in node.items():
                if key in {"resources", "components", "patchesStrategicMerge", "files", "envs"}:
                    walk(child)
                elif key == "path":
                    walk(child)
                elif key in {"patches", "configMapGenerator", "secretGenerator"}:
                    walk(child)

    walk(doc)
    return names


@needs_k8s
def test_every_kustomization_lists_the_manifests_beside_it():
    """Kustomize does not auto-discover: an unlisted manifest is read by every
    gate here and applied by nothing, so the alert or policy it declares is
    simply absent from the cluster."""
    directories = _kustomization_dirs()
    assert directories, "no kustomization.yaml found under kubernetes/"
    unlisted: list[str] = []
    for directory in directories:
        doc = yaml.safe_load((directory / "kustomization.yaml").read_text()) or {}
        referenced = _referenced_names(doc)
        for path in sorted(directory.glob("*.yaml")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if path.name == "kustomization.yaml" or rel in UNREFERENCED_ALLOWED:
                continue
            if path.name not in referenced:
                unlisted.append(rel)
    assert not unlisted, (
        "manifests no kustomization.yaml references, so kustomize never applies "
        "them:\n  " + "\n  ".join(unlisted)
    )


def test_the_unlisted_manifest_collector_names_what_is_missing(tmp_path):
    """Mutation case: a directory whose kustomization forgets one file."""
    doc = yaml.safe_load("resources:\n  - listed.yaml\n")
    referenced = _referenced_names(doc)
    (tmp_path / "listed.yaml").write_text("{}\n")
    (tmp_path / "forgotten.yaml").write_text("{}\n")
    on_disk = {p.name for p in tmp_path.glob("*.yaml")}
    assert sorted(on_disk - referenced) == ["forgotten.yaml"]


# === Apiserver egress =================================================

APISERVER_PORT = "6443"
# The substitution key cluster-config owns for this rule. A cluster that
# narrows it to the servers themselves spells their /32s instead.
APISERVER_EGRESS_KEY = "${cluster_apiserver_egress_cidr}"

# Files the egress scan could not parse. A policy inside one is never compared
# against the sanctioned peer set.
UNREADABLE: list[str] = []


def _apiserver_egress_rules(root: Path):
    """(relative path, policy name, sorted ipBlock CIDRs) per egress rule on 6443."""
    UNREADABLE.clear()
    for path in sorted(root.rglob("*.yaml")):
        try:
            docs = list(yaml.safe_load_all(path.read_text()))
        except (OSError, yaml.YAMLError) as exc:
            UNREADABLE.append(f"{path.relative_to(root)}: {exc}")
            continue
        for doc in docs:
            if not isinstance(doc, dict) or doc.get("kind") != "NetworkPolicy":
                continue
            for rule in (doc.get("spec") or {}).get("egress") or []:
                if not any(
                    str(p.get("port")) == APISERVER_PORT for p in (rule.get("ports") or [])
                ):
                    continue
                yield (
                    str(path.relative_to(root)),
                    (doc.get("metadata") or {}).get("name"),
                    {
                        peer["ipBlock"]["cidr"]
                        for peer in (rule.get("to") or [])
                        if "ipBlock" in peer
                    },
                )


K3S_SERVER_GROUP = "k3s_servers"


def _group_hosts(group: str) -> dict | None:
    """The `hosts` mapping of one inventory group, or None when it is absent.

    `_inventory_hosts()` flattens the groups away, and group membership is what
    sanctions an apiserver peer, not a host name that reads like a server.
    """

    def walk(node):
        if not isinstance(node, dict):
            return None
        if isinstance(node.get(group), dict):
            return node[group].get("hosts") or {}
        for key, value in node.items():
            if key != "vars":
                found = walk(value)
                if found is not None:
                    return found
        return None

    return walk(yaml.safe_load(INVENTORY_HOSTS.read_text()) or {})


def _sanctioned_apiserver_peers() -> set[str]:
    """The placeholder, the API VIP, and every k3s server address."""
    allowed = {APISERVER_EGRESS_KEY}
    if "cluster-config" in CONFIGMAPS:
        vip = CONFIGMAPS["cluster-config"][1].get("cluster_api_vip")
        if vip:
            allowed.add(f"{vip}/32")
    servers = _group_hosts(K3S_SERVER_GROUP)
    assert servers, (
        f"the inventory declares no {K3S_SERVER_GROUP} group with hosts, so this "
        "gate would silently shrink its allow set"
    )
    for host_vars in servers.values():
        address = (host_vars or {}).get("ansible_host")
        if address:
            allowed.add(f"{address}/32")
    return allowed


def _unsanctioned_apiserver_peers(rules, allowed: set[str]) -> list[str]:
    return [
        f"{path}:{name} allows {APISERVER_PORT} to {sorted(cidrs - allowed)}"
        for path, name, cidrs in rules
        if cidrs - allowed
    ]


@needs_k8s
def test_every_networkpolicy_manifest_parsed():
    """A file that will not parse drops its NetworkPolicies from the egress
    scan, so a rule opening 6443 wider than the sanctioned set reads as a pass."""
    list(_apiserver_egress_rules(K8S_ROOT))
    assert not UNREADABLE, (
        "unparseable manifests — any NetworkPolicy they declare went unchecked:\n  "
        + "\n  ".join(UNREADABLE)
    )


@needs_k8s
@needs_inventory
def test_every_apiserver_egress_names_a_sanctioned_peer():
    """A rule opening 6443 to anything but the apiserver reaches the control
    plane from a namespace that has no business there."""
    rules = list(_apiserver_egress_rules(K8S_ROOT))
    assert rules, f"no egress rule on {APISERVER_PORT} found under kubernetes/"
    offenders = _unsanctioned_apiserver_peers(rules, _sanctioned_apiserver_peers())
    assert not offenders, (
        "\n  ".join(offenders)
        + f"\nUse {APISERVER_EGRESS_KEY}, the API VIP, or the k3s server addresses."
    )


def test_an_arbitrary_apiserver_peer_is_reported():
    """Mutation case: the collector, not just the shipped corpus."""
    synthetic = [("kubernetes/apps/x/networkpolicy.yaml", "allow-egress-x", {"0.0.0.0/0"})]
    assert _unsanctioned_apiserver_peers(synthetic, {APISERVER_EGRESS_KEY})


@needs_inventory
def test_the_server_group_is_looked_up_by_name():
    """A renamed group returns None rather than an empty set, so the allow set
    cannot shrink in silence."""
    assert _group_hosts(K3S_SERVER_GROUP), f"no {K3S_SERVER_GROUP} group in the inventory"
    assert _group_hosts("k3s_servers_renamed") is None


# === Destructive-upgrade guards =======================================

# Releases whose chart owns CRDs: a helm uninstall of one cascade-deletes every
# CR it defines. Each names the shape that keeps them, so a values key the chart
# never reads fails instead of passing on the string alone.
CRD_KEEPERS = {
    "infrastructure/crds/release.yaml": "values",
    "infrastructure/controllers/cert-manager/release.yaml": "keep",
    "infrastructure/controllers/metallb/release.yaml": "postRenderer",
    "infrastructure/controllers/external-secrets/release.yaml": "values",
}
# Every other HelmRelease, each with the reason an uninstall takes no CR with it.
# A release in neither dict fails the walk below, so a chart that starts shipping
# CRDs in templates/ cannot reach the cluster unnoticed.
NO_CRDS = {
    "infrastructure/controllers/external-dns/release.yaml": (
        "the chart ships no CustomResourceDefinition; records are driven by "
        "annotations on Services and Ingresses"
    ),
    "infrastructure/controllers/kured/release.yaml": (
        "the chart ships no CustomResourceDefinition; reboot state lives in node "
        "annotations and a lock ConfigMap"
    ),
    "infrastructure/controllers/nvidia-device-plugin/release.yaml": (
        "the chart ships no CustomResourceDefinition; the plugin advertises its "
        "devices through the kubelet device plugin API"
    ),
    "infrastructure/controllers/onepassword-connect/release.yaml": (
        "the chart ships no CustomResourceDefinition here; the operator that owns "
        "OnePasswordItem is not installed, ESO reads Connect instead"
    ),
    "infrastructure/controllers/reloader/release.yaml": (
        "the chart ships no CustomResourceDefinition; it watches ConfigMaps and "
        "Secrets and annotates workloads"
    ),
    "infrastructure/controllers/tailscale-operator/release.yaml": (
        "the chart's CRDs ship in its crds/ directory, which a helm uninstall "
        "never deletes"
    ),
    "infrastructure/controllers/traefik/release.yaml": (
        "the chart's CRDs ship in its crds/ directory, which a helm uninstall "
        "never deletes; crds: CreateReplace keeps their schema current"
    ),
    "infrastructure/controllers/vpa/release.yaml": (
        "the chart's CRDs ship in its crds/ directory, which a helm uninstall "
        "never deletes; crds: CreateReplace keeps their schema current"
    ),
    "infrastructure/observability/alloy/release.yaml": (
        "the chart ships no CustomResourceDefinition; its collectors are "
        "configured through values and ConfigMaps"
    ),
    "infrastructure/observability/exporters/blackbox-exporter.yaml": (
        "the chart ships no CustomResourceDefinition; its probes are values and "
        "a ServiceMonitor from the CRD stage"
    ),
    "infrastructure/observability/kube-prometheus-stack/release.yaml": (
        "the monitoring.coreos.com CRDs belong to infrastructure/crds; this "
        "release sets crds.enabled false and crds: Skip"
    ),
    "infrastructure/observability/loki/release.yaml": (
        "the chart ships no CustomResourceDefinition; ruler rules arrive as "
        "ConfigMaps the sidecar loads"
    ),
    "apps/authentik/release.yaml": (
        "the chart ships no CustomResourceDefinition; all authentik state lives "
        "in its database and in terraform/authentik"
    ),
    "apps/gitlab-runner/release.yaml": (
        "the chart ships no CustomResourceDefinition; runners register over the "
        "API and hold no cluster-scoped state"
    ),
    "apps/gitlab-runner-privileged/release.yaml": (
        "the chart ships no CustomResourceDefinition; runners register over the "
        "API and hold no cluster-scoped state"
    ),
}
# cert-manager keeps its CRDs through an uninstall via crds.keep, so it does not
# need the retry strategy the others use to avoid one.
CRD_RETRY_RELEASES = sorted(
    set(CRD_KEEPERS) - {"infrastructure/controllers/cert-manager/release.yaml"}
)
EXTERNAL_DNS_RELEASE = "infrastructure/controllers/external-dns/release.yaml"
# external-dns >= 0.22 defaults to external-dns.kubernetes.io/; the annotation
# the manifests carry is the alpha one, and the mismatch deletes every record.
ANNOTATION_PREFIX = "external-dns.alpha.kubernetes.io/"
ANNOTATION_PREFIX_ARG = f"--annotation-prefix={ANNOTATION_PREFIX}"
EXTERNAL_DNS_ANNOTATION_RE = re.compile(r"^external-dns(\.alpha)?\.kubernetes\.io/")
KEEP_POLICY = ("helm.sh/resource-policy", "keep")


def _helm_release(relpath: str) -> dict:
    path = K8S_ROOT / relpath
    assert path.is_file(), f"{relpath} is missing — this guard checked nothing"
    for doc in yaml.safe_load_all(path.read_text()):
        if isinstance(doc, dict) and doc.get("kind") == "HelmRelease":
            return doc
    raise AssertionError(f"{relpath} holds no HelmRelease")


def _values_keep_crds(doc: dict) -> bool:
    """`crds.annotations` — the values key both charts actually read."""
    key, value = KEEP_POLICY
    values = (doc.get("spec") or {}).get("values") or {}
    annotations = (values.get("crds") or {}).get("annotations") or {}
    return annotations.get(key) == value


def _post_renderer_keeps_crds(doc: dict) -> bool:
    """A kustomize patch whose target is the CRDs, not some other kind."""
    key, value = KEEP_POLICY
    for renderer in (doc.get("spec") or {}).get("postRenderers") or []:
        for entry in (renderer.get("kustomize") or {}).get("patches") or []:
            if (entry.get("target") or {}).get("kind") != "CustomResourceDefinition":
                continue
            try:
                patch = yaml.safe_load(entry.get("patch") or "")
            except yaml.YAMLError:
                continue
            if not isinstance(patch, dict):
                continue
            if ((patch.get("metadata") or {}).get("annotations") or {}).get(key) == value:
                return True
    return False


def _keep_flag_keeps_crds(doc: dict) -> bool:
    """`crds.keep` — the boolean cert-manager's chart turns into the annotation."""
    values = (doc.get("spec") or {}).get("values") or {}
    return (values.get("crds") or {}).get("keep") is True


KEEP_SHAPES = {
    "values": _values_keep_crds,
    "postRenderer": _post_renderer_keeps_crds,
    "keep": _keep_flag_keeps_crds,
}


def _keeps_crds(doc: dict, shape: str) -> bool:
    return KEEP_SHAPES[shape](doc)


def _install_strategy(doc: dict) -> str | None:
    """`spec.install.strategy.name` — the only path the HelmRelease CRD defines."""
    strategy = ((doc.get("spec") or {}).get("install") or {}).get("strategy")
    return strategy.get("name") if isinstance(strategy, dict) else None


@needs_k8s
@pytest.mark.parametrize("relpath,shape", sorted(CRD_KEEPERS.items()))
def test_crd_installing_releases_keep_their_crds(relpath, shape):
    assert _keeps_crds(_helm_release(relpath), shape), (
        f"{relpath} no longer sets {KEEP_POLICY[0]}: {KEEP_POLICY[1]} on the CRDs "
        f"its chart owns, at the {shape} path the chart reads — the next uninstall "
        "or chart-version change takes every CR with them"
    )


@needs_k8s
@pytest.mark.parametrize("relpath", CRD_RETRY_RELEASES)
def test_crd_installing_releases_retry_instead_of_uninstalling(relpath):
    """Install remediation remediates by uninstalling, which is the cascade
    above, so every CRD-owning release retries in place instead."""
    assert _install_strategy(_helm_release(relpath)) == "RetryOnFailure", (
        f"{relpath} install remediation is back to uninstalling on failure, which "
        "deletes the chart's CRDs and every CR"
    )


@needs_k8s
def test_external_dns_pins_the_annotation_prefix():
    values = (_helm_release(EXTERNAL_DNS_RELEASE).get("spec") or {}).get("values") or {}
    assert ANNOTATION_PREFIX_ARG in (values.get("extraArgs") or []), (
        f"{EXTERNAL_DNS_RELEASE} dropped {ANNOTATION_PREFIX_ARG}: external-dns "
        "stops seeing the annotations the manifests carry and prunes the records "
        "it owns"
    )


def _annotation_keys(node):
    """Every annotation key in a document, including nested pod templates."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "annotations" and isinstance(value, dict):
                yield from value
            else:
                yield from _annotation_keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from _annotation_keys(item)


def _external_dns_annotations(root: Path) -> list[tuple[str, str]]:
    """(relative path, annotation key) per external-dns annotation under `root`."""
    return sorted(
        (str(path.relative_to(root.parent)), key)
        for path, doc in _docs(root)
        for key in _annotation_keys(doc)
        if EXTERNAL_DNS_ANNOTATION_RE.match(key)
    )


def _off_prefix(found: list[tuple[str, str]], prefix: str) -> list[str]:
    return [f"{rel}: {key}" for rel, key in found if not key.startswith(prefix)]


@needs_k8s
def test_external_dns_annotations_match_the_pinned_prefix():
    """The flag and the annotations are one invariant: a key spelled with the
    other prefix is invisible to external-dns, so the record is never published
    and nothing reports it."""
    found = _external_dns_annotations(K8S_ROOT)
    assert found, (
        "no external-dns annotation under kubernetes/ — this gate is examining nothing"
    )
    offenders = _off_prefix(found, ANNOTATION_PREFIX)
    assert not offenders, (
        f"external-dns annotations outside {ANNOTATION_PREFIX}, the prefix "
        f"{EXTERNAL_DNS_RELEASE} pins: external-dns never sees them and publishes "
        "no record for them:\n  " + "\n  ".join(offenders)
    )


def test_an_annotation_outside_the_pinned_prefix_is_reported(tmp_path):
    """Mutation case: the 0.22 default spelling, and a nested pod-template
    annotation the walk still reaches."""
    root = tmp_path / "kubernetes"
    root.mkdir()
    (root / "route.yaml").write_text(
        "kind: IngressRoute\nmetadata:\n  annotations:\n"
        "    external-dns.kubernetes.io/hostname: app.example.com\n"
        "    kubernetes.io/ingress.class: traefik\n"
    )
    (root / "deployment.yaml").write_text(
        "kind: Deployment\nspec:\n  template:\n    metadata:\n      annotations:\n"
        f"        {ANNOTATION_PREFIX}target: example.com\n"
    )
    found = _external_dns_annotations(root)
    assert len(found) == 2
    assert _off_prefix(found, ANNOTATION_PREFIX) == [
        "kubernetes/route.yaml: external-dns.kubernetes.io/hostname"
    ]


def _helm_release_paths() -> tuple[list[str], list[str]]:
    """Every HelmRelease path under kubernetes/, relative to that tree."""
    documents, unreadable = k8s_documents(K8S_ROOT)
    return sorted(
        str(path.relative_to(K8S_ROOT))
        for path, doc in documents
        if doc.get("kind") == "HelmRelease"
    ), unreadable


def _unclassified(paths: list[str]) -> list[str]:
    return sorted(set(paths) - set(CRD_KEEPERS) - set(NO_CRDS))


@needs_k8s
def test_every_helmrelease_is_classified_for_crd_ownership():
    """A release in neither dict is one nothing holds to the keep policy: a
    chart shipping CRDs in templates/ loses every CR on a failed install."""
    paths, unreadable = _helm_release_paths()
    assert_all_parsed(unreadable, "HelmRelease")
    assert paths, "no HelmRelease found under kubernetes/ — this guard checked nothing"
    missing = _unclassified(paths)
    assert not missing, (
        "HelmReleases in neither CRD_KEEPERS nor NO_CRDS:\n  "
        + "\n  ".join(missing)
        + "\n\nAdd each to CRD_KEEPERS with the shape that keeps its CRDs, or to "
        "NO_CRDS with the reason an uninstall takes no CR with it."
    )


def test_every_no_crds_entry_states_its_reason():
    for relpath, reason in NO_CRDS.items():
        assert len(reason.split()) >= 8, f"{relpath} needs a stated reason"


def test_an_unclassified_helmrelease_is_reported():
    """Mutation case: the inversion, not just the shipped corpus. An entry
    naming a release a modules-off render does not ship is not an error."""
    listed = sorted(CRD_KEEPERS)[:1]
    assert _unclassified(listed) == []
    assert _unclassified([*listed, "apps/demo/release.yaml"]) == ["apps/demo/release.yaml"]


def test_a_release_without_the_keep_policy_is_reported():
    """Mutation case: the detector, not just the shipped corpus. Each shape is
    anchored to the path its chart reads, so a misplaced annotation fails."""
    key, value = KEEP_POLICY
    annotations = {key: value}
    assert _keeps_crds({"spec": {"values": {"crds": {"annotations": annotations}}}}, "values")
    assert not _keeps_crds({"spec": {"values": {"crds": {"annotations": {}}}}}, "values")
    # A values key the chart never reads leaves the string in the document.
    assert not _keeps_crds({"spec": {"values": {"crd": {"annotations": annotations}}}}, "values")
    assert not _keeps_crds({"spec": {"values": {"crds": {"annotation": annotations}}}}, "values")

    patch = f"metadata:\n  annotations:\n    {key}: {value}\n"

    def renderer(target_kind: str, body: str) -> dict:
        entry = {"target": {"kind": target_kind}, "patch": body}
        return {"spec": {"postRenderers": [{"kustomize": {"patches": [entry]}}]}}

    assert _keeps_crds(renderer("CustomResourceDefinition", patch), "postRenderer")
    assert not _keeps_crds(renderer("CustomResourceDefinition", "metadata:\n"), "postRenderer")
    assert not _keeps_crds(renderer("Deployment", patch), "postRenderer")

    assert _keeps_crds({"spec": {"values": {"crds": {"keep": True}}}}, "keep")
    assert not _keeps_crds({"spec": {"values": {"crds": {"keep": False}}}}, "keep")
    assert not _keeps_crds({"spec": {"values": {"crd": {"keep": True}}}}, "keep")


def test_an_install_strategy_outside_the_crd_path_is_not_read_as_configured():
    """Mutation case: a bare string is not the mapping the HelmRelease CRD
    defines, so Kubernetes prunes it and the gate must report it missing."""
    assert _install_strategy({"spec": {"install": {"strategy": {"name": "RetryOnFailure"}}}}) == (
        "RetryOnFailure"
    )
    assert _install_strategy({"spec": {"install": {"strategy": "RetryOnFailure"}}}) is None
    assert _install_strategy({}) is None


AUTHENTIK_RELEASE = "apps/authentik/release.yaml"
# Every OIDC client's issuer and redirect URIs hang off this origin, so it is the
# external host even on an internal-only cluster; authentik 2026.11 requires it.
AUTHENTIK_BASE_URL = ("AUTHENTIK_WEB__BASE_URL", "https://auth.${cluster_external_domain}")


def _env_list(doc: dict, section: str) -> list[dict]:
    values = (doc.get("spec") or {}).get("values") or {}
    return (values.get(section) or {}).get("env") or []


def _wrong_issuer_origin(env: list[dict]) -> str | None:
    name, value = AUTHENTIK_BASE_URL
    for entry in env:
        if isinstance(entry, dict) and entry.get("name") == name:
            if entry.get("value") == value:
                return None
            return f"{name} is {entry.get('value')!r}"
    return f"{name} is absent"


@needs_k8s
@pytest.mark.parametrize("section", ["server", "worker"])
def test_authentik_pins_the_external_issuer_origin(section):
    offender = _wrong_issuer_origin(_env_list(_helm_release(AUTHENTIK_RELEASE), section))
    assert offender is None, (
        f"{AUTHENTIK_RELEASE} {section}.env: {offender} — expected "
        f"{AUTHENTIK_BASE_URL[1]}, the origin every OIDC client is registered "
        "against"
    )


def test_an_env_list_missing_the_external_origin_is_reported():
    """Mutation case: the collector, not just the shipped corpus."""
    name, value = AUTHENTIK_BASE_URL
    assert _wrong_issuer_origin([{"name": name, "value": value}]) is None
    assert _wrong_issuer_origin([{"name": "AUTHENTIK_SECRET_KEY"}]) is not None
    internal = "https://auth.${cluster_internal_domain}"
    assert _wrong_issuer_origin([{"name": name, "value": internal}]) is not None
    assert _env_list({"spec": {"values": {"server": {"env": [{"name": name}]}}}}, "server")
    assert _env_list({"spec": {"values": {"worker": {}}}}, "worker") == []


KPS_RELEASE = "infrastructure/observability/kube-prometheus-stack/release.yaml"
CONFIGS_DIR = K8S_ROOT / "infrastructure" / "configs"

# Helm's own defaults scope discovery to objects carrying the release label, so
# restoring any of these drops every app-side monitoring object.
WIDE_DISCOVERY = {
    "serviceMonitorSelectorNilUsesHelmValues": False,
    "serviceMonitorNamespaceSelector": {},
    "podMonitorSelectorNilUsesHelmValues": False,
    "podMonitorNamespaceSelector": {},
    "ruleSelectorNilUsesHelmValues": False,
    "ruleNamespaceSelector": {},
}


def _prometheus_spec(doc: dict) -> dict:
    values = (doc.get("spec") or {}).get("values") or {}
    return (values.get("prometheus") or {}).get("prometheusSpec") or {}


def _discovery_problems(spec: dict) -> list[str]:
    """An absent key counts, so a renamed or moved values path fails loudly
    rather than reading as the chart default."""
    problems = []
    for key, want in WIDE_DISCOVERY.items():
        if key not in spec:
            problems.append(f"{key} is absent, so the chart default applies")
        elif spec[key] != want:
            problems.append(f"{key} is {spec[key]!r}, want {want!r}")
    return problems


@needs_k8s
def test_prometheus_discovers_monitoring_objects_in_every_namespace():
    problems = _discovery_problems(_prometheus_spec(_helm_release(KPS_RELEASE)))
    assert not problems, (
        f"{KPS_RELEASE} prometheusSpec: {'; '.join(problems)} — app-side "
        "ServiceMonitors and PrometheusRules outside the release namespace stop "
        "being discovered, so their panels go blank and their alerts never fire"
    )


def test_a_narrowed_discovery_selector_is_reported():
    """Mutation case: the collector, not just the shipped values."""
    assert _discovery_problems(dict(WIDE_DISCOVERY)) == []
    assert _discovery_problems({**WIDE_DISCOVERY, "ruleSelectorNilUsesHelmValues": True})
    pruned = {k: v for k, v in WIDE_DISCOVERY.items() if k != "ruleNamespaceSelector"}
    assert _discovery_problems(pruned)
    assert _discovery_problems({})


def _cluster_issuer_names(root: Path) -> set[str]:
    return {
        (doc.get("metadata") or {}).get("name")
        for path in _yaml_files(root)
        for doc in yaml.safe_load_all(path.read_text())
        if isinstance(doc, dict) and doc.get("kind") == "ClusterIssuer"
    }


def _issuer_problem(issuer: str, names: set[str]) -> str | None:
    if not names:
        return "no ClusterIssuer in kubernetes/infrastructure/configs/"
    if issuer not in names:
        return (
            f"cluster_issuer {issuer!r} names no ClusterIssuer this stage ships "
            f"({sorted(names)}) — every Certificate would stay unissued"
        )
    return None


@needs_k8s
def test_cluster_issuer_names_an_issuer_the_configs_stage_ships():
    """cluster_issuer is free text that every issuerRef.name reads. A name no
    ClusterIssuer carries substitutes fine and issues nothing."""
    if "cluster-config" not in CONFIGMAPS:
        pytest.skip("no cluster-config ConfigMap")
    issuer = CONFIGMAPS["cluster-config"][1]["cluster_issuer"]
    problem = _issuer_problem(issuer, _cluster_issuer_names(CONFIGS_DIR))
    assert problem is None, problem


def test_an_unknown_cluster_issuer_is_reported(tmp_path):
    """Mutation case: the collector, not just the shipped config."""
    (tmp_path / "issuers.yaml").write_text(
        "kind: ClusterIssuer\nmetadata:\n  name: letsencrypt-prod\n"
        "---\nkind: ClusterIssuer\nmetadata:\n  name: letsencrypt-staging\n"
    )
    names = _cluster_issuer_names(tmp_path)
    assert names == {"letsencrypt-prod", "letsencrypt-staging"}
    assert _issuer_problem("letsencrypt-prod", names) is None
    assert _issuer_problem("letsencrypt-stagign", names) is not None
    assert _issuer_problem("letsencrypt-prod", set()) is not None


# A reference Traefik cannot resolve fails CLOSED: the whole router is disabled
# and 404s. References live in IngressRoute CRs and in the Traefik chart's own
# ingressRoute values, so both shapes are collected.
def _traefik_objects(root: Path) -> tuple[dict, list[str], int]:
    defined = {"Middleware": set(), "ServersTransport": set()}
    refs = {"Middleware": set(), "ServersTransport": set()}
    unreadable = []
    parsed = 0
    for path in _yaml_files(root):
        try:
            docs = list(yaml.safe_load_all(path.read_text()))
        except (OSError, yaml.YAMLError) as exc:
            unreadable.append(f"{path.name}: {exc}")
            continue
        parsed += 1
        where = str(path.relative_to(root))
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            meta = doc.get("metadata") or {}
            home = meta.get("namespace")
            kind = doc.get("kind")
            if kind in defined:
                defined[kind].add((home, meta.get("name")))
            elif kind == "IngressRoute":
                for route in (doc.get("spec") or {}).get("routes") or []:
                    for entry in route.get("middlewares") or []:
                        refs["Middleware"].add(
                            (entry.get("namespace") or home, entry.get("name"), where)
                        )
                    for service in route.get("services") or []:
                        name = service.get("serversTransport")
                        if name:
                            refs["ServersTransport"].add((home, name, where))
            elif kind == "HelmRelease":
                values = (doc.get("spec") or {}).get("values") or {}
                for block in (values.get("ingressRoute") or {}).values():
                    if not isinstance(block, dict):
                        continue
                    for entry in block.get("middlewares") or []:
                        refs["Middleware"].add(
                            (entry.get("namespace") or home, entry.get("name"), where)
                        )
    return {"defined": defined, "refs": refs}, unreadable, parsed


def _dangling(collected: dict) -> list[str]:
    problems = []
    for kind, refs in collected["refs"].items():
        known = collected["defined"][kind]
        for namespace, name, where in sorted(refs):
            if (namespace, name) not in known:
                problems.append(f"{where} references {kind} {namespace}/{name}")
    return problems


@needs_k8s
def test_every_traefik_middleware_reference_resolves():
    collected, unreadable, parsed = _traefik_objects(K8S_ROOT)
    assert not unreadable, f"unparsed manifests shrink this check: {unreadable}"
    assert parsed, "parsed no manifests"
    assert collected["refs"]["Middleware"], "found no middleware references"
    assert collected["defined"]["Middleware"], "found no Middleware definitions"
    problems = _dangling(collected)
    assert not problems, (
        "Traefik cannot resolve these, so it disables the whole router and the "
        f"front door 404s: {problems}"
    )


def test_a_dangling_traefik_reference_is_reported(tmp_path):
    """Mutation case: the collector, not just the shipped manifests."""
    (tmp_path / "defined.yaml").write_text(
        "kind: Middleware\nmetadata:\n  name: hsts-header\n  namespace: traefik\n"
        "---\nkind: ServersTransport\nmetadata:\n  name: vm-tls\n  namespace: vm\n"
    )
    (tmp_path / "routes.yaml").write_text(
        "kind: IngressRoute\nmetadata:\n  name: app\n  namespace: vm\n"
        "spec:\n  routes:\n    - middlewares:\n"
        "        - name: hsts-header\n          namespace: traefik\n"
        "        - name: typo-header\n          namespace: traefik\n"
        "      services:\n        - name: backend\n          serversTransport: vm-tls\n"
    )
    (tmp_path / "release.yaml").write_text(
        "kind: HelmRelease\nmetadata:\n  name: traefik\n  namespace: traefik\n"
        "spec:\n  values:\n    ingressRoute:\n      dashboard:\n        middlewares:\n"
        "          - name: gone-strict\n            namespace: traefik\n"
    )
    collected, unreadable, parsed = _traefik_objects(tmp_path)
    assert not unreadable and parsed == 3
    problems = _dangling(collected)
    assert any("typo-header" in problem for problem in problems)
    assert any("gone-strict" in problem for problem in problems)
    assert not any("hsts-header" in problem for problem in problems)
    assert not any("vm-tls" in problem for problem in problems)


def test_an_unparseable_manifest_is_reported(tmp_path):
    (tmp_path / "broken.yaml").write_text("kind: Middleware\n  name: [oops\n")
    _, unreadable, parsed = _traefik_objects(tmp_path)
    assert unreadable and parsed == 0
