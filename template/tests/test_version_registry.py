"""How a tracked pin reaches the fleet, held against how the manifests read it.

A pin the registry says Flux rolls out that no manifest reads never rolls out
at all, and a digest pin shared by several manifests must name all of them.
"""

from __future__ import annotations

import re

import pytest
from conftest import REPO, load_script

K8S = REPO / "kubernetes"
_registry = load_script("version-registry.py")
CONFIG = _registry.CONFIG
FLUX_DEPLOY = _registry._FLUX

# Pins an image-build job bakes into an image instead of substituting, so no
# `${...}` placeholder reads them. Each entry needs a rationale comment.
BUILD_INPUTS: frozenset = frozenset()

PLACEHOLDER_RE = re.compile(r"(?<!\$)\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

needs_k8s = pytest.mark.skipif(not K8S.is_dir(), reason="no kubernetes/ tree")


def placeholder_name(var_name: str) -> str:
    """The ConfigMap key, and so the `${...}` spelling, of a registry var.

    generate-versions-configmap.py flattens a nested pin to `<parent>_<child>`.
    """
    return var_name.replace(".", "_")


def _substituted_names() -> set[str]:
    names: set[str] = set()
    for path in K8S.rglob("*.yaml"):
        names |= set(PLACEHOLDER_RE.findall(path.read_text(encoding="utf-8")))
    return names


def rollout_problems(services, substituted: set[str]) -> list[str]:
    """Pins whose rollout path and whose manifest wiring disagree."""
    problems: list[str] = []
    for svc in services:
        var_name = svc.get("var_name") or ""
        key = placeholder_name(var_name)
        flux = svc.get("deploy_command") == FLUX_DEPLOY
        if key in substituted and not flux:
            problems.append(f"{var_name}: substituted by Flux but not routed through it")
        if flux and key not in substituted and var_name not in BUILD_INPUTS:
            problems.append(f"{var_name}: rolls out through Flux but no manifest reads it")
    return problems


@needs_k8s
def test_rollout_mapping_matches_how_the_pin_reaches_the_fleet():
    substituted = _substituted_names()
    assert substituted, "found no ${...} placeholders — the substitution syntax moved"
    services = CONFIG["services"]
    assert any(placeholder_name(svc.get("var_name") or "") in substituted for svc in services), (
        "no registry entry is substituted into a manifest — this gate is examining nothing"
    )
    problems = rollout_problems(services, substituted)
    assert not problems, (
        "version-registry.py's deploy_command and the manifests disagree:\n  "
        + "\n  ".join(problems)
    )


def test_the_rollout_reader_catches_both_directions():
    """Mutation case: an Ansible rollout for a substituted pin, and a Flux rollout
    nothing reads."""
    services = [
        {"var_name": "a_version", "deploy_command": "task infra:deploy"},
        {"var_name": "b_version", "deploy_command": FLUX_DEPLOY},
        {"var_name": "helm_chart_versions.c", "deploy_command": FLUX_DEPLOY},
    ]
    problems = rollout_problems(services, {"a_version", "helm_chart_versions_c"})
    assert len(problems) == 2
    assert problems[0].startswith("a_version:")
    assert problems[1].startswith("b_version:")
    assert rollout_problems(services[2:], {"helm_chart_versions_c"}) == []


def shared_pin_problems(services, root) -> list[str]:
    """Entries whose `version_file` list is not exactly the manifests pinning the ref."""
    problems: list[str] = []
    for svc in services:
        version_file, ref = svc.get("version_file"), svc.get("image_ref")
        if not version_file or not ref:
            continue
        listed = {version_file} if isinstance(version_file, str) else set(version_file)
        if not all(path.startswith("kubernetes/") for path in listed):
            continue
        pattern = re.compile(rf"image:\s*{re.escape(ref)}:[^\s@]+@sha256:")
        carrying = {
            str(path.relative_to(root))
            for path in (root / "kubernetes").rglob("*.yaml")
            if pattern.search(path.read_text(encoding="utf-8"))
        }
        if carrying != listed:
            problems.append(
                f"{svc.get('name')}: version_file lists {sorted(listed)} but "
                f"{sorted(carrying)} pin {ref}"
            )
    return problems


@needs_k8s
def test_version_file_lists_name_every_manifest_carrying_the_pin():
    """A digest pin shared by several manifests must list all of them: bumped in
    two of three, the third silently keeps the old image."""
    problems = shared_pin_problems(CONFIG["services"], REPO)
    assert not problems, "\n  ".join(problems)


def test_the_shared_pin_reader_catches_a_manifest_left_off_the_list(tmp_path):
    """Mutation case: the gate is a tripwire until an entry carries both fields,
    so the reader is proven against a fixture tree."""
    manifests = tmp_path / "kubernetes" / "apps"
    manifests.mkdir(parents=True)
    for name in ("one.yaml", "two.yaml"):
        (manifests / name).write_text("      image: example.io/app:1.2.3@sha256:deadbeef\n")
    svc = {
        "name": "App",
        "image_ref": "example.io/app",
        "version_file": ["kubernetes/apps/one.yaml"],
    }
    assert shared_pin_problems([svc], tmp_path)
    svc["version_file"] = ["kubernetes/apps/one.yaml", "kubernetes/apps/two.yaml"]
    assert shared_pin_problems([svc], tmp_path) == []
