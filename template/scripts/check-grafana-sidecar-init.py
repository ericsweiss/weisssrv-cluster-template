#!/usr/bin/env python3
"""Assert every k8s-sidecar container runs the METHOD its position requires: LIST
in an init container, so it exits; WATCH long-running, where LIST crash-loops it.
Exit 0 clean, 1 a container in the wrong mode, 2 the gate could not inspect it.
"""
from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

REPO = Path(__file__).resolve().parent.parent

MANIFEST = Path("kubernetes/infrastructure/observability/kube-prometheus-stack/release.yaml")
VERSIONS_CONFIGMAP = Path("kubernetes/infrastructure/sources/versions-configmap.yaml")
CLUSTER_CONFIG = Path("kubernetes/infrastructure/sources/cluster-config.yaml")
CHART = "kube-prometheus-stack"
CHART_REPO = "https://prometheus-community.github.io/helm-charts"
# The chart names every k8s-sidecar container <release>-sc-<kind>, and the init
# variant <release>-init-sc-<kind>.
SIDECAR_MARKER = "-sc-"
# k8s-sidecar reads its mode from METHOD. Only LIST terminates, so the required
# mode is the opposite of the container's position in the pod spec.
METHOD_ENV = "METHOD"
REQUIRED_METHOD = {"initContainers": "LIST", "containers": "WATCH"}
POSITION = {"initContainers": "init", "containers": "long-running"}
RENDER_TIMEOUT_SECONDS = 300

# A stalled chart repo must fail with a message, not hang until the job timeout.
_VALIDATOR = "validate-helm-values.py"


class GateError(RuntimeError):
    """The gate could not inspect its subject: exit 2, never a finding."""


# The reused validator reports by `raise SystemExit("ERROR: ...")`, which exits
# 1 on its own; a parse or read failure likewise reads as a finding uncaught.
_CANNOT_INSPECT = (SystemExit, yaml.YAMLError, OSError, ValueError)


def _one_line(exc: BaseException) -> str:
    """One line of an exception, so the gate never prints a traceback."""
    first = str(exc).strip().splitlines()
    return first[0] if first else type(exc).__name__


def _inspect(step: str, call, *args, **kwargs):
    """Run one inspection step; a failure inside it is exit 2, not a finding."""
    try:
        return call(*args, **kwargs)
    except GateError:
        raise
    except _CANNOT_INSPECT as exc:
        raise GateError(f"{step}: {_one_line(exc)}") from exc


def _yaml_file(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _documents(text: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]


def _validator():
    """Reuse the sibling gate's substitution and HelmRelease extraction."""
    src = Path(__file__).resolve().parent / _VALIDATOR
    if not src.is_file():
        raise GateError(f"{_VALIDATOR} must sit next to this script")
    spec = importlib.util.spec_from_file_location("validate_helm_values", str(src))
    module = importlib.util.module_from_spec(spec)
    _inspect(f"loading {_VALIDATOR}", spec.loader.exec_module, module)
    return module


def substitutions(root: Path, validator) -> dict:
    """Both Flux substitution ConfigMaps, which the manifest's ${vars} resolve from."""
    versions = _inspect(f"reading {VERSIONS_CONFIGMAP}", validator.load_versions,
                        str(root), str(root / VERSIONS_CONFIGMAP))
    config_path = root / CLUSTER_CONFIG
    if not config_path.is_file():
        raise GateError(f"cluster identity ConfigMap not found: {CLUSTER_CONFIG}")
    config = _inspect(f"parsing {CLUSTER_CONFIG}", _yaml_file, config_path).get("data") or {}
    if not config:
        raise GateError(f"no cluster identity keys in {CLUSTER_CONFIG}")
    return {**versions, **config}


def render(root: Path, validator) -> list[dict]:
    """The chart as Flux installs it: pinned version, substituted values."""
    manifest = root / MANIFEST
    if not manifest.is_file():
        raise GateError(f"HelmRelease not found: {MANIFEST}")
    source = _inspect(f"reading {MANIFEST}", manifest.read_text, encoding="utf-8")
    text, missing = validator.substitute(source, substitutions(root, validator))
    if missing:
        raise GateError(f"{MANIFEST} references unknown ConfigMap key(s): {missing}")
    spec = _inspect(f"parsing {MANIFEST}", validator.extract_helmrelease_from_text,
                    text, str(manifest)).get("spec", {})
    version = str(spec.get("chart", {}).get("spec", {}).get("version", ""))
    if not version:
        raise GateError(f"could not determine the chart version pinned in {MANIFEST}")
    with tempfile.NamedTemporaryFile("w", suffix=".yaml") as values_file:
        _inspect(f"serialising the values in {MANIFEST}", yaml.safe_dump,
                 spec.get("values", {}), values_file, sort_keys=False)
        values_file.flush()
        cmd = [
            "helm", "template", CHART, CHART,
            "--repo", CHART_REPO,
            "--version", version,
            "--namespace", spec.get("targetNamespace") or "default",
            "-f", values_file.name,
            "--skip-tests",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=RENDER_TIMEOUT_SECONDS, check=False)
        except FileNotFoundError as exc:
            raise GateError("helm is not on PATH, so the chart cannot be rendered") from exc
        except subprocess.TimeoutExpired as exc:
            raise GateError(f"`helm template {CHART}@{version}` timed out") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["no output"]
        raise GateError(f"`helm template {CHART}@{version}` failed: {tail[0]}")
    return _inspect(f"parsing the rendered {CHART}@{version}", _documents, proc.stdout)


def sidecars(docs: list[dict]) -> list[tuple[str, str, str, str | None]]:
    """(workload, container, pod-spec field, METHOD) for every k8s-sidecar container."""
    found = []
    for doc in docs:
        pod = ((doc.get("spec") or {}).get("template") or {}).get("spec") or {}
        workload = str(doc.get("metadata", {}).get("name", "?"))
        for field in REQUIRED_METHOD:
            for container in pod.get(field) or []:
                name = str(container.get("name", ""))
                if SIDECAR_MARKER not in name:
                    continue
                env = {e.get("name"): e.get("value") for e in container.get("env") or []}
                found.append((workload, name, field, env.get(METHOD_ENV)))
    return found


def check(root: Path = REPO) -> list[str]:
    containers = sidecars(render(root, _validator()))
    # A gate that inspects nothing is not a gate: the values enable a sidecar in
    # both positions, so a render missing either means the knob or the chart moved.
    for field in REQUIRED_METHOD:
        if not any(place == field for _, _, place, _ in containers):
            raise GateError(
                f"{MANIFEST} renders no `*{SIDECAR_MARKER}*` {POSITION[field]} "
                f"container, so this gate checked nothing in {field}. Either the "
                "chart renamed it or its sidecar knob is off; update this gate "
                "with whichever it is."
            )
    return [
        f"{workload}: {POSITION[field]} container {name} runs with {METHOD_ENV}="
        f"{method or '<unset>'}, not {REQUIRED_METHOD[field]}"
        for workload, name, field, method in containers
        if method != REQUIRED_METHOD[field]
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Grafana's k8s-sidecar containers must run the METHOD their "
                    "position requires: LIST in init, WATCH long-running.",
    )
    parser.add_argument("--repo-root", default=REPO, type=Path)
    args = parser.parse_args(argv)
    try:
        problems = check(args.repo_root)
    except GateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except (Exception, SystemExit) as exc:
        # Nothing escapes as a traceback: exit 1 would read as a finding.
        print(f"ERROR: {type(exc).__name__}: {_one_line(exc)}", file=sys.stderr)
        return 2
    if problems:
        print("ERROR: a k8s-sidecar container runs the wrong mode. An init container "
              "that watches never exits, so the pod stays in PodInitializing and the "
              "Helm upgrade times out; a long-running one that lists exits at once "
              "and crash-loops:")
        for problem in problems:
            print(f"  - {problem}")
        print(f"  Set the matching sidecar's watchMethod in {MANIFEST}.")
        return 1
    print(f"Every rendered k8s-sidecar container runs the {METHOD_ENV} its position "
          "requires.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
