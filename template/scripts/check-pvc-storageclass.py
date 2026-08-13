#!/usr/bin/env python3
"""Assert every claim pins a storageClassName.

A cluster on pre-provisioned PersistentVolumes (zvol or NFS, `Retain`, bound by
`storageClassName: ""`) needs the field written out. Omitting it is not neutral:
the DefaultStorageClass admission plugin rewrites an unset `storageClassName` to
whatever class is marked default, at create time, with no diff in git — so the
claim binds a dynamically provisioned volume that no backup path covers.
StatefulSet `volumeClaimTemplates` are immutable, so that is not editable
afterwards; the PVC has to be deleted and recreated.

Disabling the packaged provisioner removes the class to fall through to, but is
one inventory edit away from returning. This makes the omission itself fail in
CI.

Input: the rendered manifest corpus on stdin (what `task flux:lint` accumulates
from `kustomize build | envsubst`).

The gate refuses to be vacuous, in both the shapes its siblings guard. An EMPTY
corpus is an operator error (exit 2), not a pass, and so is a corpus that HAS
documents but declares no claim at all: that is what a render loop which never
reached the storage-declaring stages produces. This gate takes no arguments, so
a mis-piped invocation has no other symptom — and it reads the same accumulated
corpus as check-scrape-netpol.py and check-secretstore-scope.py, which hold the
same contract.

Exit 0 clean, 1 on a finding, 2 on an operator error.

Usage:
  cat rendered-corpus.yaml | python3 scripts/check-pvc-storageclass.py
"""
from __future__ import annotations

import sys

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml")

# Chart values shapes that create a PVC the corpus never renders (the chart
# does, server-side). A persistence block that declares a size is provisioning
# storage, so it must also say WHICH class — `storageClass: ""` for a static
# bind, the chart-specific `"-"` sentinel where the template's `with` guard
# would otherwise drop an empty string (loki), or an existingClaim. A class
# key set to null, or an existing-volume key that is not a non-empty string,
# pins nothing: chart templates treat both as unset.
_CLASS_PIN_KEYS = ("storageClass", "storageClassName")
_VOLUME_PIN_KEYS = ("existingClaim", "existingVolume")


def _claim_violations(docs: list[dict]) -> tuple[list[str], int]:
    """-> (violations, claims inspected). The count feeds the vacuity guard."""
    out: list[str] = []
    seen = 0
    for d in docs:
        kind = d.get("kind")
        meta = d.get("metadata") or {}
        where = f"{meta.get('namespace', '')}/{kind}/{meta.get('name', '?')}"
        claims: list[tuple[str, dict]] = []
        if kind == "PersistentVolumeClaim":
            claims.append((where, d.get("spec") or {}))
        elif kind == "StatefulSet":
            templates = ((d.get("spec") or {}).get("volumeClaimTemplates") or [])
            for t in templates:
                if not isinstance(t, dict):
                    continue
                tname = (t.get("metadata") or {}).get("name", "?")
                claims.append((f"{where} volumeClaimTemplate {tname!r}", t.get("spec") or {}))
        for label, spec in claims:
            seen += 1
            # `storageClassName: null` deserializes as unset, so the default
            # StorageClass captures it exactly like a missing key; only the
            # explicit "" (bind a static PV) counts as pinned.
            if not isinstance(spec, dict) or spec.get("storageClassName") is None:
                out.append(
                    f"  {label}: no storageClassName — the default StorageClass "
                    f'would capture this claim (use "" to bind a static PV)'
                )
    return out, seen


def _values_violations(node, doc_label: str, path: str = "values") -> tuple[list[str], int]:
    """Find HelmRelease persistence blocks that size a volume but name no class.

    -> (violations, blocks inspected). A block that is `enabled: false`
    provisions nothing, so it is neither a violation nor a subject.
    """
    out: list[str] = []
    seen = 0
    if isinstance(node, dict):
        if "size" in node and node.get("enabled") is not False:
            seen += 1
            class_pinned = any(
                k in node and node[k] is not None for k in _CLASS_PIN_KEYS
            )
            volume_pinned = any(
                isinstance(node.get(k), str) and node[k].strip()
                for k in _VOLUME_PIN_KEYS
            )
            if not (class_pinned or volume_pinned):
                out.append(
                    f"  {doc_label}: {path} declares size={node['size']!r} but no "
                    f"storageClass — the chart's PVC would take the default class"
                )
        for k, v in node.items():
            child, child_seen = _values_violations(v, doc_label, f"{path}.{k}")
            out.extend(child)
            seen += child_seen
    elif isinstance(node, list):
        for i, v in enumerate(node):
            child, child_seen = _values_violations(v, doc_label, f"{path}[{i}]")
            out.extend(child)
            seen += child_seen
    return out, seen


def violations(docs: list[dict]) -> tuple[list[str], int]:
    """-> (violations, claims inspected) across manifests and HelmRelease values."""
    out, seen = _claim_violations(docs)
    for d in docs:
        if d.get("kind") != "HelmRelease":
            continue
        meta = d.get("metadata") or {}
        label = f"{meta.get('namespace', '')}/HelmRelease/{meta.get('name', '?')}"
        child, child_seen = _values_violations((d.get("spec") or {}).get("values") or {}, label)
        out.extend(child)
        seen += child_seen
    return out, seen


def main() -> int:
    docs: list[dict] = []
    try:
        for raw in yaml.safe_load_all(sys.stdin):
            if isinstance(raw, dict):
                if raw.get("kind") == "List" and isinstance(raw.get("items"), list):
                    docs.extend(i for i in raw["items"] if isinstance(i, dict))
                else:
                    docs.append(raw)
            elif isinstance(raw, list):
                docs.extend(i for i in raw if isinstance(i, dict))
    except yaml.YAMLError as exc:
        print(f"ERROR: failed to parse YAML input: {exc}", file=sys.stderr)
        return 2

    if not docs:
        print(
            "ERROR: empty corpus — no manifests on stdin. A gate that passes on nothing "
            "is not a gate; check the pipe and the `kustomize build` paths feeding it.",
            file=sys.stderr,
        )
        return 2

    found, seen = violations(docs)
    if found:
        print(
            "Claims without an explicit storageClassName — a missing field is "
            "rewritten to the cluster-default StorageClass at admission, which is "
            "how a PVC silently lands on an unbacked-up disk:",
            file=sys.stderr,
        )
        print("\n".join(found), file=sys.stderr)
        return 1

    if not seen:
        # A non-empty corpus declaring no claim is the wiring failure an empty
        # one cannot be: the render loop produced documents but never reached
        # the stages that declare storage. Same arm as check-secretstore-scope.py's
        # store-less corpus.
        print(
            f"ERROR: inspected 0 claims in {len(docs)} document(s) — a gate that "
            "checks nothing is not a gate. Check that the `kustomize build` paths "
            "feeding stdin cover the stages that declare PersistentVolumeClaims, "
            "volumeClaimTemplates or chart persistence blocks.",
            file=sys.stderr,
        )
        return 2

    print(
        f"storageClassName policy OK — {seen} claim(s) across {len(docs)} document(s) "
        "(every claim pins its class)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
