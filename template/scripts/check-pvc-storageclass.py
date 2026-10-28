#!/usr/bin/env python3
"""Assert every claim pins a storageClassName and no volumeName bind is prunable.

An unset class is rewritten at admission; a pruned volumeName claim releases
its PV for good. Both, and the static-bind resize trap: docs/SCRIPTS.md.
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    from gate_common import (  # noqa: E402  (resolved from this script's own directory)
        doc_key,
        load_corpus,
    )
except ImportError:
    print(
        "ERROR: gate_common.py must be vendored beside this gate "
        "(see scripts/vendorable-paths.yml)", file=sys.stderr,
    )
    raise SystemExit(2) from None

# Chart values keys that pin a PVC the corpus never renders: a class name
# ("" for a static bind, "-" where a chart's `with` guard drops an empty
# string) or an existing claim. Null or non-string pins nothing.
_CLASS_PIN_KEYS = ("storageClass", "storageClassName")
_VOLUME_PIN_KEYS = ("existingClaim", "existingVolume")

_PRUNE_ANNOTATION = "kustomize.toolkit.fluxcd.io/prune"


def _prune_violation(doc: dict, where: str) -> str | None:
    """A claim bound by spec.volumeName must opt out of Flux pruning."""
    volume = (doc.get("spec") or {}).get("volumeName")
    if not (isinstance(volume, str) and volume):
        return None
    annotations = (doc.get("metadata") or {}).get("annotations") or {}
    if isinstance(annotations, dict) and annotations.get(_PRUNE_ANNOTATION) == "disabled":
        return None
    # Pruning deletes the claim and releases its PV; a Released volume never
    # rebinds until someone hand-edits its claimRef.
    return (
        f"  {where}: binds volumeName {volume!r} without "
        f'{_PRUNE_ANNOTATION}: "disabled" — a Flux prune deletes the claim and '
        f"leaves the PersistentVolume Released, which never rebinds without a "
        f"hand edit of its claimRef"
    )


def _cnpg_claim_spec(block: dict) -> dict:
    """A CNPG storage block as the claim spec CNPG renders from it: `storageClass`
    wins, else the pvcTemplate's own storageClassName."""
    if block.get("storageClass") is not None:
        return {"storageClassName": block["storageClass"]}
    template = block.get("pvcTemplate") or {}
    return {"storageClassName": template.get("storageClassName")}


def _claim_violations(docs: list[dict]) -> tuple[list[str], int]:
    """-> (violations, claims inspected). The count feeds the vacuity guard."""
    out: list[str] = []
    seen = 0
    for d in docs:
        kind = d.get("kind")
        where = doc_key(d)
        claims: list[tuple[str, dict]] = []
        if kind == "PersistentVolumeClaim":
            claims.append((where, d.get("spec") or {}))
            prune = _prune_violation(d, where)
            if prune:
                out.append(prune)
        elif kind == "Cluster" and str(d.get("apiVersion", "")).startswith("postgresql.cnpg.io/"):
            spec = d.get("spec") or {}
            for field in ("storage", "walStorage"):
                if isinstance(spec.get(field), dict):
                    claims.append((f"{where} spec.{field}", _cnpg_claim_spec(spec[field])))
            for tablespace in spec.get("tablespaces") or []:
                if isinstance(tablespace, dict) and isinstance(tablespace.get("storage"), dict):
                    label = f"{where} tablespace {tablespace.get('name', '?')!r}"
                    claims.append((label, _cnpg_claim_spec(tablespace["storage"])))
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
                    f"would capture this claim (name the provisioner's class, "
                    f'or "" to bind a static PV)'
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
        label = doc_key(d)
        child, child_seen = _values_violations((d.get("spec") or {}).get("values") or {}, label)
        out.extend(child)
        seen += child_seen
    return out, seen


def main() -> int:
    docs = load_corpus()

    found, seen = violations(docs)
    if found:
        print(
            "Claim policy violations — a missing storageClassName is rewritten "
            "to the cluster-default StorageClass at admission (how a PVC silently "
            "lands on an unbacked-up disk), and a prunable volumeName bind "
            "strands its PersistentVolume:",
            file=sys.stderr,
        )
        print("\n".join(found), file=sys.stderr)
        return 1

    if not seen:
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
        "(every claim pins its class, every volumeName bind is unprunable)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
