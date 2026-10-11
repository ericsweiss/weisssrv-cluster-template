#!/usr/bin/env python3
"""Assert a pod's sized emptyDirs fit inside its ephemeral-storage budget.

A pod whose budget is below the emptyDirs it mounts is evicted before the
volumes it sized ever fill. Contract: weisssrv-lib docs/SCRIPTS.md.
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


def _declared(container: dict, section: str) -> object:
    """The container's `resources.<section>.ephemeral-storage`, or None."""
    resources = container.get("resources")
    block = resources.get(section) if isinstance(resources, dict) else None
    return block.get(RESOURCE) if isinstance(block, dict) else None


def _containers(spec: dict, section: str) -> list[dict]:
    """The mapping entries of one container list."""
    return [c for c in spec.get(section) or [] if isinstance(c, dict)]


def _sized_mounts(container: dict, sized: dict) -> list[str]:
    """The sized emptyDirs this container mounts, in mount order, each once."""
    out: list[str] = []
    for mount in container.get("volumeMounts") or []:
        if not isinstance(mount, dict):
            continue
        name = mount.get("name")
        if isinstance(name, str) and name in sized and name not in out:
            out.append(name)
    return out


def format_bytes(count: float) -> str:
    """A byte count as the largest binary suffix that divides it evenly."""
    for suffix, scale in (("Gi", 2**30), ("Mi", 2**20), ("Ki", 2**10)):
        if count >= scale and count % scale == 0:
            return "%d%s" % (count // scale, suffix)
    if float(count).is_integer():
        return "%d bytes" % count
    return "%g bytes" % count


def pod_limit(spec: dict) -> tuple[float, list[tuple[str, object]]]:
    """-> (the pod's ephemeral-storage limit in bytes, the unreadable quantities).

    The kubelet takes the higher of the app containers plus every sidecar, and
    each plain init container beside the sidecars declared before it.
    """
    app = 0.0
    sidecars = 0.0
    init_floor = 0.0
    unreadable: list[tuple[str, object]] = []
    for container in _containers(spec, "containers"):
        parsed = _limit(container, unreadable)
        if parsed is not None:
            app += parsed
    for container in _containers(spec, "initContainers"):
        parsed = _limit(container, unreadable)
        if parsed is None:
            continue
        if container.get("restartPolicy") == "Always":
            sidecars += parsed
        else:
            init_floor = max(init_floor, sidecars + parsed)
    return max(app + sidecars, init_floor), unreadable


def _limit(container: dict, unreadable: list[tuple[str, object]]) -> float | None:
    """One container's ephemeral-storage limit; None when absent or unreadable."""
    raw = _declared(container, "limits")
    if raw is None:
        return None
    parsed = parse_quantity(raw)
    if parsed is None:
        unreadable.append((container.get("name", "?"), raw))
    return parsed


def _pod_violations(label: str, spec: dict) -> list[str]:
    """Everything wrong with one pod spec's sized emptyDirs."""
    sized = sized_volumes(spec)
    if not sized:
        return []

    # A volume is charged once however many containers mount it, but each
    # mounting container owes the pair the scheduler and the kubelet read.
    out: list[str] = []
    mounted: dict[str, object] = {}
    for section in ("initContainers", "containers"):
        for container in _containers(spec, section):
            names = _sized_mounts(container, sized)
            if not names:
                continue
            mounted.update({name: sized[name] for name in names})
            for block in ("requests", "limits"):
                if _declared(container, block) is None:
                    out.append(
                        "  %s container %r: mounts a sized emptyDir but declares "
                        "no %s in resources.%s"
                        % (label, container.get("name", "?"), RESOURCE, block)
                    )
    if out or not mounted:
        return out

    total = 0.0
    for volume, size in mounted.items():
        parsed = parse_quantity(size)
        if parsed is None:
            out.append(
                "  %s volume %r: sizeLimit %r is not a readable quantity"
                % (label, volume, size)
            )
        else:
            total += parsed
    limit, unreadable = pod_limit(spec)
    for name, raw in unreadable:
        out.append(
            "  %s container %r: limits.%s %r is not a readable quantity"
            % (label, name, RESOURCE, raw)
        )
    if out:
        return out

    if limit < total:
        out.append(
            "  %s: mounts %s of sized emptyDir (%s) against a pod %s limit of %s "
            "— the kubelet evicts the pod before the volume fills"
            % (
                label, format_bytes(total),
                ", ".join("%s=%s" % (v, s) for v, s in mounted.items()),
                RESOURCE, format_bytes(limit),
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
            out.extend(_pod_violations(label, spec))
    return out, seen


def main() -> int:
    try:
        docs = load_corpus()
        found, seen = violations(docs)
    except OperatorError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2

    if found:
        print(
            "Sized emptyDirs the pod's ephemeral-storage budget does not cover. "
            "The kubelet evicts on the pod's limit — its containers' limits added "
            "up — not on the volume's sizeLimit, so the pod dies before the "
            "volume is full:",
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
