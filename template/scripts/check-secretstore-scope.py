#!/usr/bin/env python3
"""Assert every ClusterSecretStore is namespace-scoped and covers its consumers.

Reads the rendered corpus on stdin; exits 0 clean, 1 finding, 2 error. Contract: weisssrv-lib docs/SCRIPTS.md.
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
        NAME_LABEL,
        SELECTOR_KEYS,
        condition_admits,
        load_corpus,
        selector_matches,
    )
except ImportError:
    print(
        "ERROR: gate_common.py must be vendored beside this gate "
        "(see scripts/vendorable-paths.yml)", file=sys.stderr,
    )
    raise SystemExit(2) from None

CLUSTER_STORE_KIND = "ClusterSecretStore"


def _unmodelled_selector_keys(selector: object) -> set:
    """Selector keys outside the labelSelector schema.

    The CRD prunes them, so a `matchLabel:` typo reaches the apiserver as the
    EMPTY selector — which admits every namespace.
    """
    if not isinstance(selector, dict):
        return set()
    return set(selector) - SELECTOR_KEYS


def namespace_labels(name: str, labels: object = None) -> dict:
    """A Namespace's labels as the apiserver stores them, its name label included."""
    merged = dict(labels) if isinstance(labels, dict) else {}
    merged[NAME_LABEL] = name
    return merged


def _condition_is_universal(
    condition: object,
    bad_patterns: list[tuple[str, re.error]] | None = None,
) -> bool:
    """True when one condition admits every namespace, so the store is unscoped.

    Its selector and regexes are probed with two namespaces no cluster would
    carry; an uncompilable regex is collected, not swallowed."""
    if not isinstance(condition, dict):
        return False
    probes = [f"probe-{uuid.uuid4().hex}" for _ in range(2)]
    selector = condition.get("namespaceSelector")
    if selector is not None and all(
        selector_matches(selector, namespace_labels(probe)) for probe in probes
    ):
        return True
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
    argparse.ArgumentParser(
        description="Every ClusterSecretStore is namespace-scoped and covers its consumers."
    ).parse_args(argv)

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
            ns_labels[name] = namespace_labels(name, meta.get("labels"))
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
    # Stores whose conditions carry a selector this gate does not model; their
    # consumer admissions say nothing, so they are reported once and skipped.
    unmodelled_stores: set[str] = set()

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
            selector = condition.get("namespaceSelector") if isinstance(condition, dict) else None
            unmodelled = _unmodelled_selector_keys(selector)
            if unmodelled:
                unmodelled_stores.add(name)
                violations.append(
                    f"  ClusterSecretStore {name}: spec.conditions[{index}].namespaceSelector "
                    f"carries unmodelled key(s) {', '.join(sorted(unmodelled))} — the CRD prunes "
                    f"them, so the apiserver keeps the EMPTY selector, which admits every "
                    f"namespace. Spell the terms as matchLabels / matchExpressions."
                )
                continue
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
        unmodelled = set().union(*(
            [_unmodelled_selector_keys(sel) for sel in selectors] or [set()]
        ))
        if unmodelled:
            violations.append(
                f"  ClusterExternalSecret {meta.get('name', '?')}: a namespace selector "
                f"carries unmodelled key(s) {', '.join(sorted(unmodelled))} — the CRD prunes "
                f"them, so the apiserver keeps the EMPTY selector and the fan-out reaches "
                f"EVERY namespace. Spell the terms as matchLabels / matchExpressions."
            )
            continue
        targets = {
            ns for ns, labels in ns_labels.items()
            if any(selector_matches(sel, labels) for sel in selectors)
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
            unknown.add(store)
            continue
        conditions = stores[store] or []
        if not conditions:
            continue  # already reported as unscoped above
        if store in unmodelled_stores:
            continue  # already reported as unmodelled above
        labels = ns_labels.get(namespace) or namespace_labels(namespace)
        store_bad = []
        admitted = any(
            condition_admits(c, namespace, labels, store_bad) for c in conditions
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
            f"the store's manifest to the linted tree."
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
            f"{len(docs)} document(s), so there is nothing to check. "
            "Check that the `kustomize build` paths feeding stdin cover the stage "
            "that defines the stores.",
            file=sys.stderr,
        )
        return 2

    print(
        f"ClusterSecretStore scoping OK ({len(stores)} cluster stores, "
        f"{len(consumers)} namespace consumers checked)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
