"""Shared helpers for the suites under tests/.

load_script() resolves a hyphenated, unimportable gate, k8s_documents() is the
one manifest walk, and require_tool() handles a missing binary.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import tempfile
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"

# Where `task lib:sync` puts the weisssrv-lib checkout (Taskfile.yml's LIB_DIR),
# and the gate whose presence identifies one.
LOCAL_CHECKOUT = ".weisssrv-lib"
GATE_RELPATH = "scripts/check-vendored-copies.py"


def _lib_root() -> Path:
    """The weisssrv-lib checkout the vendored-copy gates read."""
    candidates = []
    explicit = os.environ.get("WEISSSRV_LIB_PATH")
    if explicit:
        candidates.append(Path(explicit))
    candidates += [REPO / LOCAL_CHECKOUT, REPO.parent / "weisssrv-lib"]
    for candidate in candidates:
        if (candidate / GATE_RELPATH).is_file():
            return candidate
    raise AssertionError(
        f"no weisssrv-lib checkout with {GATE_RELPATH} found — run `task lib:sync` "
        f"(it clones one into {LOCAL_CHECKOUT}/ at the pinned ref) or set "
        "$WEISSSRV_LIB_PATH. This gate never skips: an ungated vendored copy is "
        "exactly the drift it exists to catch."
    )


def load_script(name: str | Path):
    """Import a hyphenated script under a module name Python accepts.

    A bare basename resolves under scripts/; a path is taken as given, for the
    gates that ship inside a manifest rather than in scripts/.
    """
    path = Path(name)
    path = path if path.is_absolute() else SCRIPTS / path
    spec = importlib.util.spec_from_file_location(
        path.name.replace("-", "_").removesuffix(".py"), path
    )
    assert spec and spec.loader, f"{path} is not importable"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def require_tool(name: str, gate_name: str, install_hint: str = "") -> None:
    """Stop a binary-driven gate that has no binary instead of skipping in CI.

    Skipping suits a workstation without the tool; under $CI the job never
    installed it, so the gate would certify a check it never ran.
    """
    if shutil.which(name):
        return
    if os.environ.get("CI"):
        pytest.fail(
            f"{name} is not on PATH, so the {gate_name} gate cannot run. "
            + (install_hint or "Install it in the job.")
        )
    pytest.skip(f"{name} is not on PATH")


def reported(path: Path) -> Path:
    return path.relative_to(REPO) if path.is_relative_to(REPO) else path


# Both YAML spellings reach the cluster, so both are walked: a `.yml` manifest
# dropped from the corpus takes every invariant declared in it with it.
K8S_SUFFIXES = ("*.yaml", "*.yml")


def k8s_documents(
    root: Path, *, suffix: str | None = None
) -> tuple[list[tuple[Path, dict]], list[str]]:
    """(path, document) for every mapping document under `root`, plus the files
    that would not parse, because a dropped file shrinks the caller's corpus
    silently. `suffix` narrows the walk to one glob."""
    found: list[tuple[Path, dict]] = []
    unreadable: list[str] = []
    globs = (suffix,) if suffix else K8S_SUFFIXES
    for path in sorted({p for glob in globs for p in root.rglob(glob)}):
        if not path.is_file():
            continue
        try:
            docs = list(yaml.safe_load_all(path.read_text()))
        except (OSError, yaml.YAMLError) as exc:
            unreadable.append(f"{reported(path)}: {exc}")
            continue
        found += [(path, doc) for doc in docs if isinstance(doc, dict)]
    return found, unreadable


def assert_all_parsed(unreadable: list[str], kind: str) -> None:
    """The shared every-manifest-parsed assertion, named for what went unchecked."""
    assert not unreadable, (
        f"unparseable manifests — any {kind} they declare went unchecked:\n  "
        + "\n  ".join(unreadable)
    )


# cluster-config key -> (group_vars file, inventory variable) it mirrors. Shared
# because both the mirror gate and the restatement gate key off the same pairs.
CONFIGMAP_INVENTORY_MIRROR = {
    "cluster_internal_domain": ("all.yml", "internal_domain"),
    "cluster_external_domain": ("all.yml", "external_domain"),
    "cluster_node_label_domain": ("all.yml", "internal_domain"),
    "cluster_pod_cidr": ("k3s.yml", "k3s_cluster_cidr"),
    "cluster_service_cidr": ("k3s.yml", "k3s_service_cidr"),
    # kube-vip and MetalLB answer ARP for these from the inventory values; the
    # manifests reach the same addresses through the ConfigMap.
    "cluster_api_vip": ("all.yml", "k3s_api_vip"),
    "cluster_k3s_api_vip": ("all.yml", "k3s_api_vip"),
    "cluster_metallb_public_vip": ("all.yml", "metallb_public_vip"),
    "cluster_metallb_internal_vip": ("all.yml", "metallb_internal_vip"),
}

OBSERVABILITY = REPO / "kubernetes" / "infrastructure" / "observability"
APPS = REPO / "kubernetes" / "apps"


def release_alert_rules() -> tuple[list[tuple[str, dict]], list[str]]:
    """The alerts inside the kube-prometheus-stack HelmRelease's values.

    The release path is absolute: the extractor's default is relative to the
    working directory, so a caller running from elsewhere would extract nothing.
    """
    extract = load_script("extract-prometheus-config.py")
    release = REPO / extract.DEFAULT_RELEASE
    with tempfile.TemporaryDirectory() as scratch:
        rules_file = Path(scratch) / "rules.yaml"
        if extract.extract_rules(rules_file, release) != 0:
            return [], [f"{reported(release)}: no rule groups extracted"]
        doc = yaml.safe_load(rules_file.read_text()) or {}
    return [
        (rule["alert"], rule)
        for group in doc.get("groups") or []
        for rule in group.get("rules") or []
        if rule.get("alert")
    ], []


def prometheusrule_alert_rules() -> tuple[list[tuple[str, dict]], list[str]]:
    """The alerts any PrometheusRule CR declares, platform or app."""
    found: list[tuple[str, dict]] = []
    unreadable: list[str] = []
    trees = [tree for tree in (OBSERVABILITY, APPS) if tree.is_dir()]
    for path in sorted(path for tree in trees for path in tree.rglob("*.yaml")):
        try:
            docs = list(yaml.safe_load_all(path.read_text()))
        except (OSError, yaml.YAMLError) as exc:
            unreadable.append(f"{reported(path)}: {exc}")
            continue
        for doc in docs:
            if not isinstance(doc, dict) or doc.get("kind") != "PrometheusRule":
                continue
            for group in (doc.get("spec") or {}).get("groups") or []:
                for rule in group.get("rules") or []:
                    if rule.get("alert"):
                        found.append((rule["alert"], rule))
    return found, unreadable


def loki_alert_rules(loki: Path | None = None) -> tuple[list[tuple[str, dict]], list[str]]:
    """The alerts the Loki ruler evaluates. These are bare `groups:` files the
    loki-sc-rules sidecar loads, not PrometheusRule CRs."""
    found: list[tuple[str, dict]] = []
    unreadable: list[str] = []
    loki = loki or OBSERVABILITY / "loki"
    kustomization = loki / "kustomization.yaml"
    if not kustomization.is_file():
        return found, unreadable
    try:
        config = yaml.safe_load(kustomization.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        return found, [f"{reported(kustomization)}: {exc}"]
    names = [
        name
        for entry in config.get("configMapGenerator") or []
        for name in entry.get("files") or []
    ]
    for name in names:
        # A `files:` entry may be written `key=path`; the path is what ships.
        path = loki / name.rsplit("=", 1)[-1]
        if not path.is_file():
            unreadable.append(
                f"{reported(path)}: named by {reported(kustomization)}'s "
                "configMapGenerator but not shipped, so its alerts went unchecked"
            )
            continue
        try:
            doc = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError) as exc:
            unreadable.append(f"{reported(path)}: {exc}")
            continue
        for group in doc.get("groups") or []:
            for rule in group.get("rules") or []:
                if rule.get("alert"):
                    found.append((rule["alert"], rule))
    return found, unreadable


def alert_rules() -> tuple[list[tuple[str, dict]], list[str]]:
    """(alertname, rule) across the whole shipped rule corpus, plus the rule
    files that would not parse. A dropped file silently shrinks the corpus."""
    found: list[tuple[str, dict]] = []
    unreadable: list[str] = []
    for walker in (release_alert_rules, prometheusrule_alert_rules, loki_alert_rules):
        walked, failed = walker()
        found += walked
        unreadable += failed
    return found, unreadable


VENDORED_MANIFEST = SCRIPTS / "vendored-manifest.yml"


def vendored_consumer_paths() -> list[str]:
    """Every consumer path scripts/vendored-manifest.yml registers.

    Both forms: a bare string is its own consumer path, a mapping names `lib`
    and `consumer`. `forked:` entries count — they are library copies too.
    """
    manifest = yaml.safe_load(VENDORED_MANIFEST.read_text()) or {}
    found = []
    for section in ("vendored", "forked"):
        for entry in manifest.get(section) or []:
            found.append(entry["consumer"] if isinstance(entry, dict) else entry)
    return found
