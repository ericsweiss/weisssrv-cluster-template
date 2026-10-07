#!/usr/bin/env python3
"""The workloads in one namespace that consume a named Secret.

Reads `kubectl get deployment,statefulset,daemonset -o json` on stdin and prints
one TSV row per consumer: `<kind>/<name>`, `kustomize` or `other`, its selector.
"""
from __future__ import annotations

import json
import sys

# kustomize-controller stamps this on everything it applies. A restart
# annotation on such a workload is drift and gets reverted on the next reconcile.
KUSTOMIZE_LABEL = "kustomize.toolkit.fluxcd.io/name"


def _secret_names(pod_spec: dict) -> set:
    """Every Secret name the pod template references, by any mechanism."""
    names = set()
    for volume in pod_spec.get("volumes") or []:
        secret = (volume or {}).get("secret") or {}
        if secret.get("secretName"):
            names.add(secret["secretName"])
        projected = (volume or {}).get("projected") or {}
        for source in projected.get("sources") or []:
            inner = (source or {}).get("secret") or {}
            if inner.get("name"):
                names.add(inner["name"])
    for pull in pod_spec.get("imagePullSecrets") or []:
        if (pull or {}).get("name"):
            names.add(pull["name"])
    container_keys = ("containers", "initContainers", "ephemeralContainers")
    for key in container_keys:
        for container in pod_spec.get(key) or []:
            for source in (container or {}).get("envFrom") or []:
                ref = (source or {}).get("secretRef") or {}
                if ref.get("name"):
                    names.add(ref["name"])
            for entry in (container or {}).get("env") or []:
                ref = ((entry or {}).get("valueFrom") or {}).get("secretKeyRef") or {}
                if ref.get("name"):
                    names.add(ref["name"])
    return names


def _selector(workload: dict) -> str:
    """The workload's matchLabels as a `k=v,k=v` selector, or "" when absent."""
    labels = ((workload.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))


def consumers(items: list, secret: str) -> list:
    """`(object, manager, selector)` per workload referencing `secret`."""
    found = []
    for item in items:
        pod_spec = (((item.get("spec") or {}).get("template") or {}).get("spec")) or {}
        if secret not in _secret_names(pod_spec):
            continue
        meta = item.get("metadata") or {}
        kind = (item.get("kind") or "").lower()
        obj = f"{kind}/{meta.get('name', '')}"
        manager = "kustomize" if (meta.get("labels") or {}).get(KUSTOMIZE_LABEL) else "other"
        found.append((obj, manager, _selector(item)))
    return found


def main(argv: list) -> int:
    if len(argv) != 1:
        print("usage: flux-secret-consumers.py <secret-name> < workloads.json", file=sys.stderr)
        return 2
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"ERROR: stdin is not the JSON kubectl emits: {exc}", file=sys.stderr)
        return 2
    items = payload.get("items") if isinstance(payload, dict) else None
    if items is None:
        print("ERROR: stdin carries no .items — pass `kubectl get ... -o json`", file=sys.stderr)
        return 2
    rows = consumers(items, argv[0])
    unscopable = [obj for obj, _manager, selector in rows if not selector]
    if unscopable:
        print(
            "ERROR: no matchLabels on " + ", ".join(unscopable) + " — a pod delete "
            "cannot be scoped to it; restart it by hand",
            file=sys.stderr,
        )
        return 2
    for obj, manager, selector in rows:
        print(f"{obj}\t{manager}\t{selector}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
