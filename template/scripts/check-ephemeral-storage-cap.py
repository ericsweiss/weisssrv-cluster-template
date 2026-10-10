#!/usr/bin/env python3
"""Assert a sized emptyDir fits inside its container's ephemeral-storage limit.

A container whose limit is below the emptyDirs it mounts is evicted before the
volume it sized ever fills. Contract: weisssrv-lib docs/SCRIPTS.md.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    from gate_common import (  # noqa: E402  (resolved from this script's own directory)
        OperatorError,
        doc_key,
        load_corpus,
    )
except ImportError:
    print(
        "ERROR: gate_common.py must be vendored beside this gate "
        "(see scripts/vendorable-paths.yml)", file=sys.stderr,
    )
    raise SystemExit(2) from None

RESOURCE = "ephemeral-storage"

# Workload kinds carrying a pod template, and where the template sits.
TEMPLATE_PATHS = {
    "CronJob": ("spec", "jobTemplate", "spec", "template"),
    "DaemonSet": ("spec", "template"),
    "Deployment": ("spec", "template"),
    "Job": ("spec", "template"),
    "ReplicaSet": ("spec", "template"),
    "ReplicationController": ("spec", "template"),
    "StatefulSet": ("spec", "template"),
}

# Kubernetes quantity suffixes. The binary ones are what a manifest writes;
# the decimal ones are accepted so an exotic but valid pin still compares.
SUFFIXES = {
    "": 1, "m": 0.001,
    "k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "P": 10**15, "E": 10**18,
    "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60,
}
QUANTITY_RE = re.compile(r"^([+-]?[0-9.]+(?:[eE][+-]?[0-9]+)?)([a-zA-Z]*)$")


def parse_quantity(value: object) -> float | None:
    """A Kubernetes quantity in bytes, or None when it cannot be read."""
    match = QUANTITY_RE.match(str(value).strip())
    if not match:
        return None
    number, suffix = match.groups()
    if suffix not in SUFFIXES:
        return None
    try:
        return float(number) * SUFFIXES[suffix]
    except ValueError:
        return None


def pod_specs(doc: dict) -> list[tuple[str, dict]]:
    """-> [(label, pod spec)] for the pod this document declares, if any."""
    kind = doc.get("kind")
    where = doc_key(doc)
    if kind == "Pod":
        spec = doc.get("spec")
        return [(where, spec)] if isinstance(spec, dict) else []
    path = TEMPLATE_PATHS.get(kind)
    if not path:
        return []
    node: object = doc
    for key in path:
        if not isinstance(node, dict):
            return []
        node = node.get(key)
    if not isinstance(node, dict):
        return []
    spec = node.get("spec")
    return [(where, spec)] if isinstance(spec, dict) else []


def sized_volumes(spec: dict) -> dict[str, object]:
    """Volume name -> its emptyDir `sizeLimit`, for the disk-backed volumes that set one.

    A `medium: Memory` emptyDir is tmpfs, charged to the container's memory limit,
    so it needs no ephemeral-storage pair.
    """
    found: dict[str, object] = {}
    for volume in spec.get("volumes") or []:
        if not isinstance(volume, dict):
            continue
        empty_dir = volume.get("emptyDir")
        name = volume.get("name")
        if isinstance(empty_dir, dict) and empty_dir.get("medium") == "Memory":
            continue
        if isinstance(empty_dir, dict) and empty_dir.get("sizeLimit") is not None:
            if isinstance(name, str):
                found[name] = empty_dir["sizeLimit"]
    return found


def _mounted(container: dict, sized: dict) -> list[tuple[str, object]]:
    """The sized emptyDirs this container mounts, in mount order."""
    out = []
    for mount in container.get("volumeMounts") or []:
        if isinstance(mount, dict) and mount.get("name") in sized:
            out.append((mount["name"], sized[mount["name"]]))
    return out


def _container_violations(label: str, container: dict, sized: dict) -> list[str]:
    name = container.get("name", "?")
    mounts = _mounted(container, sized)
    if not mounts:
        return []
    resources = container.get("resources")
    resources = resources if isinstance(resources, dict) else {}
    out: list[str] = []
    for section in ("requests", "limits"):
        block = resources.get(section)
        if not isinstance(block, dict) or block.get(RESOURCE) is None:
            out.append(
                "  %s container %r: mounts a sized emptyDir but declares no "
                "%s in resources.%s" % (label, name, RESOURCE, section)
            )
    if out:
        return out

    # Every emptyDir the container mounts draws on the same budget, so the sum
    # is what the kubelet weighs the limit against.
    total = 0.0
    for volume, size in mounts:
        parsed = parse_quantity(size)
        if parsed is None:
            out.append(
                "  %s volume %r: sizeLimit %r is not a readable quantity"
                % (label, volume, size)
            )
        else:
            total += parsed
    limit = parse_quantity(resources["limits"][RESOURCE])
    if limit is None:
        out.append(
            "  %s container %r: limits.%s %r is not a readable quantity"
            % (label, name, RESOURCE, resources["limits"][RESOURCE])
        )
    if out:
        return out
    if limit < total:
        out.append(
            "  %s container %r: limits.%s is %s but it mounts %s of sized "
            "emptyDir — the kubelet evicts the pod before the volume fills"
            % (
                label, name, RESOURCE, resources["limits"][RESOURCE],
                ", ".join("%s=%s" % (v, s) for v, s in mounts),
            )
        )
    return out


def violations(docs: list[dict]) -> tuple[list[str], int]:
    """-> (violations, pod specs inspected). The count feeds the vacuity guard."""
    out: list[str] = []
    seen = 0
    for doc in docs:
        for label, spec in pod_specs(doc):
            seen += 1
            sized = sized_volumes(spec)
            if not sized:
                continue
            for section in ("initContainers", "containers"):
                for container in spec.get(section) or []:
                    if isinstance(container, dict):
                        out.extend(_container_violations(label, container, sized))
    return out, seen


def main() -> int:
    docs = load_corpus()
    try:
        found, seen = violations(docs)
    except OperatorError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2

    if found:
        print(
            "Sized emptyDirs their container's ephemeral-storage budget does "
            "not cover. The kubelet evicts on the container's limit, not on the "
            "volume's sizeLimit, so the pod dies before the volume is full:",
            file=sys.stderr,
        )
        print("\n".join(found), file=sys.stderr)
        return 1

    if not seen:
        print(
            "ERROR: inspected 0 pod specs in %d document(s) — a gate that checks "
            "nothing is not a gate. Check that the `kustomize build` paths "
            "feeding stdin cover the stages that declare workloads." % len(docs),
            file=sys.stderr,
        )
        return 2

    print(
        "ephemeral-storage policy OK — %d pod spec(s) across %d document(s)"
        % (seen, len(docs))
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
