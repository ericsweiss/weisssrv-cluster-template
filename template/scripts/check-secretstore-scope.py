#!/usr/bin/env python3
"""Assert every ClusterSecretStore is namespace-scoped and covers its consumers.

Reads the rendered corpus on stdin, stores outside the tree via
--external-store; exits 0 clean, 1 finding, 2 error. Contract: docs/SCRIPTS.md.
"""
from __future__ import annotations

import argparse
import re
import sys
import uuid
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    from gate_common import (  # noqa: E402  (resolved from this script's own directory)
        load_corpus,
        selects_all_pods,
    )
except ImportError:
    print(
        "ERROR: gate_common.py must be vendored beside this gate "
        "(see scripts/vendorable-paths.yml)", file=sys.stderr,
    )
    raise SystemExit(2) from None

CLUSTER_STORE_KIND = "ClusterSecretStore"
_EXPRESSION_KEYS = {"key", "operator", "values"}
_SET_OPERATORS = {"In", "NotIn"}
_EXISTENCE_OPERATORS = {"Exists", "DoesNotExist"}


def _selector_matches(selector: object, labels: dict) -> bool:
    """Kubernetes labelSelector semantics (matchLabels + matchExpressions, ANDed).

    A term the apiserver would reject never matches: crediting it would admit a
    namespace ESO itself refuses, leaving the Secret stale while the gate passes.
    """
    if not isinstance(selector, dict):
        return False
    match_labels = selector.get("matchLabels")
    if match_labels is not None and not isinstance(match_labels, dict):
        return False
    for key, value in (match_labels or {}).items():
        if labels.get(key) != value:
            return False
    exprs = selector.get("matchExpressions")
    if exprs is not None and not isinstance(exprs, list):
        return False
    for expr in exprs or []:
        if not isinstance(expr, dict) or set(expr) - _EXPRESSION_KEYS:
            return False
        key = expr.get("key")
        op = expr.get("operator")
        values = expr.get("values")
        present = key in labels
        if op in _SET_OPERATORS:
            if not isinstance(values, list) or not values:
                return False
            if op == "In" and labels.get(key) not in values:
                return False
            if op == "NotIn" and labels.get(key) in values:
                return False
        elif op in _EXISTENCE_OPERATORS:
            if op == "Exists" and not present:
                return False
            if op == "DoesNotExist" and present:
                return False
        else:
            return False
    return True


def _condition_admits(
    condition: object,
    namespace: str,
    labels: dict,
    bad_patterns: list[tuple[str, re.error]] | None = None,
) -> bool:
    if not isinstance(condition, dict):
        return False
    if namespace in (condition.get("namespaces") or []):
        return True
    for pattern in condition.get("namespaceRegexes") or []:
        try:
            if re.search(str(pattern), namespace):
                return True
        except re.error as exc:
            if bad_patterns is not None:
                bad_patterns.append((str(pattern), exc))
            continue
    selector = condition.get("namespaceSelector")
    if selector is not None and _selector_matches(selector, labels):
        return True
    return False


def _condition_is_universal(
    condition: object,
    bad_patterns: list[tuple[str, re.error]] | None = None,
) -> bool:
    """True when one condition admits every namespace, so the store is unscoped.

    An empty `namespaceSelector` matches everything; a catch-all regex is found
    by probing two names no cluster would carry, an uncompilable one collected."""
    if not isinstance(condition, dict):
        return False
    selector = condition.get("namespaceSelector")
    if selector is not None and selects_all_pods(selector):
        return True
    probes = [f"probe-{uuid.uuid4().hex}" for _ in range(2)]
    for pattern in condition.get("namespaceRegexes") or []:
        try:
            if all(re.search(str(pattern), probe) for probe in probes):
                return True
        except re.error as exc:
            if bad_patterns is not None:
                bad_patterns.append((str(pattern), exc))
            continue
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Every ClusterSecretStore is namespace-scoped and covers its consumers.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--external-store",
        action="append",
        default=[],
        metavar="NAME",
        help="ClusterSecretStore defined outside this corpus; referencing it is not a violation",
    )
    args = parser.parse_args(argv)
    external = set(args.external_store)

    docs = load_corpus()

    ns_labels: dict[str, dict] = {}
    stores: dict[str, list] = {}
    # (store, namespace, describing the consumer) tuples to validate.
    consumers: list[tuple[str, str, str]] = []
    cluster_external_secrets: list[dict] = []

    for doc in docs:
        kind = doc.get("kind")
        meta = doc.get("metadata") or {}
        name = meta.get("name", "?")
        spec = doc.get("spec") or {}

        if kind == "Namespace":
            ns_labels[name] = meta.get("labels") or {}
        elif kind == CLUSTER_STORE_KIND:
            conditions = spec.get("conditions")
            stores[name] = conditions if isinstance(conditions, list) else []
        elif kind == "ExternalSecret":
            ref = spec.get("secretStoreRef") or {}
            if ref.get("kind") == CLUSTER_STORE_KIND:
                ns = meta.get("namespace") or "default"
                consumers.append((ref.get("name", "?"), ns, f"ExternalSecret {ns}/{name}"))
        elif kind == "ClusterExternalSecret":
            cluster_external_secrets.append(doc)

    violations: list[str] = []
    # (store, pattern, error) for every namespaceRegexes entry that will not
    # compile: an operator error, not a scoping finding.
    bad_patterns: list[tuple[str, str, re.error]] = []

    for name, conditions in sorted(stores.items()):
        if not conditions:
            violations.append(
                f"  ClusterSecretStore {name}: no spec.conditions — referenceable "
                f"from every namespace, so any ExternalSecret in the cluster can "
                f"read the whole backing vault. Add conditions scoping it to the "
                f"namespaces that legitimately consume it."
            )
            continue
        for index, condition in enumerate(conditions):
            store_bad: list[tuple[str, re.error]] = []
            universal = _condition_is_universal(condition, store_bad)
            bad_patterns += [(name, pattern, exc) for pattern, exc in store_bad]
            if universal:
                violations.append(
                    f"  ClusterSecretStore {name}: spec.conditions[{index}] admits "
                    f"every namespace ({condition!r}), which is exactly as wide as no "
                    f"conditions at all — any ExternalSecret in the cluster can read "
                    f"the whole backing vault. Name the namespaces, or anchor the "
                    f"regex to the ones that legitimately consume it."
                )
                break

    # A ClusterExternalSecret creates ExternalSecrets in every namespace its
    # selectors match, so those namespaces need the same admission.
    for ces in cluster_external_secrets:
        meta = ces.get("metadata") or {}
        spec = ces.get("spec") or {}
        ref = ((spec.get("externalSecretSpec") or {}).get("secretStoreRef")) or {}
        if ref.get("kind") != CLUSTER_STORE_KIND:
            continue
        selectors = spec.get("namespaceSelectors")
        if selectors is None:
            # Absent, not empty: `namespaceSelector: {}` is a selector with no
            # terms, which matches EVERY namespace - the widest fan-out there is.
            single = spec.get("namespaceSelector")
            selectors = [] if single is None else [single]
        targets = {
            ns for ns, labels in ns_labels.items()
            if any(_selector_matches(sel, labels) for sel in selectors)
        }
        # ESO unions the label selectors with the literal `spec.namespaces` list,
        # so both must be counted as consumers.
        targets.update(str(ns) for ns in spec.get("namespaces") or [])
        for ns in sorted(targets):
            consumers.append(
                (
                    ref.get("name", "?"),
                    ns,
                    f"ClusterExternalSecret {meta.get('name', '?')} -> {ns}",
                )
            )

    unknown: set[str] = set()
    for store, namespace, description in consumers:
        if store not in stores:
            if store not in external:
                unknown.add(store)
            continue
        conditions = stores[store] or []
        if not conditions:
            continue  # already reported as unscoped above
        labels = ns_labels.get(namespace, {})
        store_bad = []
        admitted = any(
            _condition_admits(c, namespace, labels, store_bad) for c in conditions
        )
        bad_patterns += [(store, pattern, exc) for pattern, exc in store_bad]
        if not admitted:
            violations.append(
                f"  {description}: namespace {namespace!r} is not admitted by "
                f"ClusterSecretStore {store}'s spec.conditions — ESO will refuse "
                f"the fetch and the Secret will go stale. Add the namespace to the "
                f"store's conditions (or point the app at a scoped store)."
            )

    for store in sorted(unknown):
        violations.append(
            f"  ClusterSecretStore {store}: referenced but not defined in this corpus, "
            f"so its scope cannot be checked — and at runtime an ExternalSecret pointing "
            f"at a store that does not resolve never syncs, leaving a stale Secret. Add "
            f"the store's manifest to the linted tree, or declare it with "
            f"--external-store {store} if it is genuinely managed elsewhere."
        )

    if bad_patterns:
        for store, pattern, exc in sorted(set((s, p, str(e)) for s, p, e in bad_patterns)):
            print(
                f"ERROR: ClusterSecretStore {store} declares a namespaceRegexes "
                f"entry that does not compile: {pattern!r} ({exc})",
                file=sys.stderr,
            )
        return 2

    if violations:
        print(
            "ClusterSecretStore scoping invariant violated:", file=sys.stderr
        )
        print("\n".join(sorted(set(violations))), file=sys.stderr)
        return 1

    if not stores and not consumers:
        # Documents rendered but neither a store nor a consumer found: the render
        # never reached the stage defining the stores.
        print(
            f"ERROR: inspected 0 ClusterSecretStores and 0 namespace consumers in "
            f"{len(docs)} document(s) — a gate that checks nothing is not a gate. "
            "Check that the `kustomize build` paths feeding stdin cover the stage "
            "that defines the stores.",
            file=sys.stderr,
        )
        return 2

    referenced = {store for store, _ns, _description in consumers}
    unused = sorted(name for name in external if name not in referenced)
    if unused:
        print(
            f"(--external-store declared but not referenced by this corpus: "
            f"{', '.join(unused)})"
        )
    external_note = f", {len(external)} declared external" if external else ""
    print(
        f"ClusterSecretStore scoping OK ({len(stores)} cluster stores, "
        f"{len(consumers)} namespace consumers checked{external_note})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
