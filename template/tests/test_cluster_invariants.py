"""Invariants this cluster's own layout has to keep.

Two families, both PyYAML-only so they run in the `python-tests` CI job without
kustomize or Ansible:

* **Site values stay out of the Kubernetes manifests.** Every domain, VIP and
  CIDR lives in ONE place — the cluster-config ConfigMap — and reaches the
  manifests through Flux postBuild substitution. These fail the moment a
  manifest hard-codes one of those values, or references a placeholder no
  ConfigMap defines. `flux-lint` covers the same ground POST-kustomize.
* **The Ansible inventory's addresses are internally consistent.** `hosts.yml`
  ships as a skeleton you are told to re-address, and two guests on one address
  — or one vmid, which `pct` and `qm` share a namespace for — fails phases later
  than the edit that caused it, naming neither.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
K8S_ROOT = REPO_ROOT / "kubernetes"
SOURCES_DIR = K8S_ROOT / "infrastructure" / "sources"
CLUSTERS_DIR = K8S_ROOT / "clusters"
ANSWERS_FILE = REPO_ROOT / ".copier-answers.yml"
INVENTORY_HOSTS = REPO_ROOT / "ansible" / "inventories" / "prod" / "hosts.yml"

# The postBuild sources every stage after `sources` substitutes from.
SUBSTITUTE_SOURCES = ("cluster-versions", "cluster-config")

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


def _yaml_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.yaml") if p.is_file())


def _code_lines(path: Path):
    """(lineno, line) for every line that is not a whole-line comment.

    kustomize drops comments before Flux ever sees the manifest, so a
    `${placeholder}` written in documentation is not a substitution.
    """
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        yield lineno, line


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
    for path in _yaml_files(K8S_ROOT):
        for lineno, line in _code_lines(path):
            for name in PLACEHOLDER_RE.findall(line):
                if name not in SUBSTITUTION_KEYS:
                    rel = path.relative_to(REPO_ROOT)
                    unresolved.append(f"{rel}:{lineno} ${{{name}}}")
    assert not unresolved, (
        "placeholders with no ConfigMap key (envsubst and Flux both render these "
        "as EMPTY, so they fail silently in the cluster):\n  " + "\n  ".join(unresolved)
    )


@needs_k8s
def test_flux_kustomizations_carry_both_substitute_sources():
    if not CLUSTERS_DIR.is_dir():
        pytest.skip("no kubernetes/clusters/ tree")
    expected = set(SUBSTITUTE_SOURCES) & set(CONFIGMAPS)
    missing: list[str] = []
    for path in _yaml_files(CLUSTERS_DIR):
        for doc in yaml.safe_load_all(path.read_text()):
            if not isinstance(doc, dict) or doc.get("kind") != "Kustomization":
                continue
            spec = doc.get("spec") or {}
            if not spec.get("path"):
                continue
            refs = {
                entry.get("name")
                for entry in (spec.get("postBuild") or {}).get("substituteFrom") or []
                if isinstance(entry, dict)
            }
            absent = expected - refs
            if absent:
                name = (doc.get("metadata") or {}).get("name", "?")
                missing.append(f"{path.relative_to(REPO_ROOT)} ({name}): {sorted(absent)}")
    assert not missing, (
        "Flux Kustomizations that do not read every postBuild source — their "
        "manifests' placeholders would render empty:\n  " + "\n  ".join(missing)
    )


@needs_k8s
def test_no_site_addresses_are_hard_coded_in_manifests():
    """Domains, IPs and CIDRs belong in cluster-config, referenced as ${...}.

    Scoped to the values that came from the copier ANSWERS: a well-known
    constant that happens to live in the ConfigMap (the CGNAT range, the pod and
    service CIDRs) is legitimately spelled out in a NetworkPolicy.
    """
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
    offenders: list[str] = []
    for path in _yaml_files(K8S_ROOT):
        if path == config_file:
            continue
        for lineno, line in _code_lines(path):
            for value, key in literals.items():
                if value in line:
                    rel = path.relative_to(REPO_ROOT)
                    offenders.append(f"{rel}:{lineno} {value!r} — use ${{{key}}}")
    assert not offenders, "site values hard-coded in manifests:\n  " + "\n  ".join(offenders)


# --------------------------------------------------------------------------
# Ansible inventory
# --------------------------------------------------------------------------


def _inventory_hosts() -> dict[str, dict]:
    """host name -> merged vars, for every host in the YAML inventory.

    Merged rather than collected per group: a host listed in two groups
    (`dns-01` is in both `dns` and `dns_primary`) is ONE machine, and comparing
    its two entries against each other would report every such host as a
    duplicate of itself.
    """
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


def _duplicates(field: str) -> list[str]:
    owners: dict[str, list[str]] = {}
    for name, host_vars in sorted(_inventory_hosts().items()):
        value = host_vars.get(field)
        if value is not None:
            owners.setdefault(str(value), []).append(name)
    return [
        f"{field} {value} is claimed by {', '.join(names)}"
        for value, names in sorted(owners.items())
        if len(names) > 1
    ]


@needs_inventory
def test_inventory_declares_hosts():
    """The three tests below are vacuously true on an empty inventory."""
    assert _inventory_hosts(), f"{INVENTORY_HOSTS} declares no hosts"


@needs_inventory
def test_every_vmid_is_unique():
    """`pct` and `qm` share ONE vmid namespace.

    Most vmids here are composed from a fixed scheme, but a resolver's is
    derived from its address (`100 + last octet`), so re-addressing one moves
    it — into the k3s server band, if the new address is in the .31+ range.
    The second `create` then fails against an id Proxmox already knows, or the
    reconcile path adopts the wrong guest, several phases after the edit.
    """
    clashes = _duplicates("vmid")
    assert not clashes, "duplicate vmids in the inventory:\n  " + "\n  ".join(clashes)


@needs_inventory
def test_every_ansible_host_is_unique():
    clashes = _duplicates("ansible_host")
    assert not clashes, (
        "two hosts configured on one address:\n  "
        + "\n  ".join(clashes)
        + "\nWhichever is provisioned second takes the address."
    )


@needs_inventory
def test_every_ansible_host_is_inside_the_lan():
    """Guests are created on the flat LAN and route through its gateway; an
    address outside it is unreachable from everything that manages it."""
    if "cluster-config" not in CONFIGMAPS:
        pytest.skip("no cluster-config ConfigMap to read cluster_lan_cidr from")
    cidr = CONFIGMAPS["cluster-config"][1].get("cluster_lan_cidr")
    if not cidr:
        pytest.skip("cluster-config declares no cluster_lan_cidr")
    network = ipaddress.ip_network(cidr, strict=False)
    outside: list[str] = []
    for name, host_vars in sorted(_inventory_hosts().items()):
        addr = host_vars.get("ansible_host")
        if addr is None:
            continue
        try:
            address = ipaddress.ip_address(str(addr))
        except ValueError:
            continue  # a name rather than an address; DNS resolves it
        if address not in network:
            outside.append(f"{name}: {addr}")
    assert not outside, (
        f"hosts addressed outside cluster_lan_cidr ({network}):\n  " + "\n  ".join(outside)
    )
